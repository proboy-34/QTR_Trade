"""REST closed-candle loop — the core autonomous paper-trading cycle (no WebSocket needed).

Every run, per configured timeframe:

1. Determine the latest fully closed candle (never the forming one; a short grace period
   covers exchange publication lag).
2. Sync closed candles from Binance REST for eligible assets and assets with open positions.
3. Monitor open paper positions candle by candle (stop, target, strategy exit, maximum
   holding period), then mark them to the real bid and check stop/target against it.
4. Scan once per closed candle (idempotent marker row).
5. For each asset, claim the candle in the `processed_candles` ledger (unique on
   exchange + symbol + timeframe + open time) and run the decision pipeline at the candle's
   close time: regime -> strategy -> evidence -> Gemini -> deterministic Risk -> paper fill.
6. Recompute the paper account.

Candle exit semantics (OHLC has no intrabar ordering, so the rule is conservative):
- open beyond the stop            -> exit at the open (gap), market slippage applies
- open beyond the target          -> exit at the target (no price improvement assumed)
- low <= stop and high >= target  -> STOP is assumed to have happened first
- only the stop touched           -> exit at the stop, market slippage applies
- only the target touched         -> exit at the target (resting limit), no slippage
- strategy exit rule on the close -> exit at the real bid (latest candle) or the close, with slippage
- maximum holding period reached  -> exit like a strategy exit

Restart safety: processed candles, open positions and each position's last evaluated candle
are all in the database; nothing is kept only in memory.
"""

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ONE, ZERO, decimal, money, price
from app.core.events import EventBus
from app.core.logging import redact
from app.core.time import TimeService
from app.global_services.features import enrich
from app.global_services.historical import (
    TIMEFRAME_DELTA,
    BinanceHistoricalProvider,
    HistoricalBackfillService,
    HistoricalCandleProvider,
)
from app.global_services.quotes import BinanceQuoteProvider, Quote, QuoteProvider, QuoteUnavailable
from app.global_services.regime import RegimeService, candles_frame
from app.global_services.scanner import MarketScanner
from app.global_services.universe import UniverseService, liquidity_state
from app.learning.post_trade import PostTradeAnalyst
from app.memory.trade_memory import TradeMemoryService
from app.models import (
    MarketCandle,
    Opportunity,
    Position,
    PositionEvent,
    ProcessedCandle,
    StrategyVersion,
)
from app.research.dsl import spec_from_version
from app.trading.loop import PaperTradingLoop
from app.trading.paper_account import PaperAccount
from app.trading.pipeline import MarketSnapshot, TradingPipeline, latest_signals
from app.trading.positions import track_extremes
from app.trading.reasoning import HIGHER_TIMEFRAME
from app.trading.safety import HALTING, SafetyService

logger = logging.getLogger("qtr.candle_cycle")
SCAN_MARKER = "__SCAN__"
OPEN = ("OPEN", "MANAGING")


def last_closed_open(timeframe: str, now: datetime, grace_seconds: int = 0) -> datetime:
    """Open time of the latest candle that has fully closed at `now` (the forming candle is excluded)."""
    interval_ms = int(TIMEFRAME_DELTA[timeframe].total_seconds() * 1000)
    now_ms = int((TimeService.ensure_utc(now) - timedelta(seconds=grace_seconds)).timestamp() * 1000)
    return datetime.fromtimestamp(((now_ms // interval_ms) * interval_ms - interval_ms) / 1000, UTC)


def resample_closed(frame: pd.DataFrame, timeframe: str, target: str, decision_time: datetime) -> pd.DataFrame:
    """Aggregate closed candles into a higher timeframe, keeping only buckets fully closed by
    `decision_time` and fully covered by source candles (never a partial, forming bucket)."""
    if frame.empty:
        return frame
    step, big = TIMEFRAME_DELTA[timeframe], TIMEFRAME_DELTA[target]
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
    data["bucket"] = data["timestamp"].dt.floor(pd.Timedelta(big))
    grouped = data.groupby("bucket").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                         close=("close", "last"), volume=("volume", "sum"), count=("close", "size"))
    grouped = grouped[(grouped["count"] == int(big / step))
                      & (grouped.index + pd.Timedelta(big) <= pd.Timestamp(decision_time))]
    return grouped.drop(columns="count").reset_index().rename(columns={"bucket": "timestamp"})


def candle_exit(side: str, stop: Decimal, target: Decimal, candle: MarketCandle) -> tuple[str, Decimal] | None:
    """Conservative OHLC exit rule (see module docstring). Returns (reason, trigger price)."""
    open_, high, low = decimal(candle.open), decimal(candle.high), decimal(candle.low)
    if side == "BUY":
        if open_ <= stop:
            return "stop_loss_gap", open_
        if open_ >= target:
            return "take_profit", target
        if low <= stop:
            return ("stop_loss_ambiguous_candle" if high >= target else "stop_loss"), stop
        if high >= target:
            return "take_profit", target
        return None
    if open_ >= stop:
        return "stop_loss_gap", open_
    if open_ <= target:
        return "take_profit", target
    if high >= stop:
        return ("stop_loss_ambiguous_candle" if low <= target else "stop_loss"), stop
    if low <= target:
        return "take_profit", target
    return None


class ClosedCandleCycle:
    def __init__(self, session_factory: Callable[[], Session], settings: Settings, event_bus: EventBus,
                 candle_provider: HistoricalCandleProvider | None = None, quote_provider: QuoteProvider | None = None,
                 clock: Callable[[], datetime] = TimeService.now, ai_provider: Any = None) -> None:
        self.session_factory, self.settings, self.event_bus = session_factory, settings, event_bus
        self.ai_provider = ai_provider
        self.exchange = settings.scanner_exchange
        self.candles = candle_provider or BinanceHistoricalProvider(settings.binance_public_base_url)
        self.quotes = quote_provider or BinanceQuoteProvider(settings.binance_public_base_url)
        self.clock = clock

    # ------------------------------------------------------------------ run
    async def run(self) -> dict[str, Any]:
        now = TimeService.ensure_utc(self.clock())
        report: dict[str, Any] = {"now": now.isoformat(), "exchange": self.exchange, "timeframes": {}}
        with self.session_factory() as session:
            report["interrupted_marked"] = self._mark_interrupted(session, now)
        for timeframe in self.settings.csv("scanner_timeframes"):
            report["timeframes"][timeframe] = await self._timeframe(timeframe, now)
        with self.session_factory() as session:
            PaperAccount(session, self.settings).snapshot()
            session.commit()
        return report

    def _mark_interrupted(self, session: Session, now: datetime) -> int:
        """A claim that never completed (process killed) is closed out, never re-run (no duplicates)."""
        stale = session.scalars(select(ProcessedCandle).where(
            ProcessedCandle.status == "CLAIMED", ProcessedCandle.processed_at < now - timedelta(minutes=15))).all()
        for row in stale:
            row.status, row.detail = "INTERRUPTED", {**(row.detail or {}), "note": "claimed but never completed; not replayed"}
        session.commit()
        return len(stale)

    async def _timeframe(self, timeframe: str, now: datetime) -> dict[str, Any]:
        last_open = last_closed_open(timeframe, now, self.settings.candle_close_grace_seconds)
        close_time = last_open + TIMEFRAME_DELTA[timeframe]
        with self.session_factory() as session:
            halted = [c.scope for c in SafetyService(session).active() if c.scope in HALTING]
            eligible = UniverseService.eligible_symbols(session, self.exchange)
            held = sorted({row for row in session.scalars(select(Position.symbol).where(
                Position.status.in_(OPEN), Position.venue.in_(["paper", "testnet"]))).all()})
        symbols = list(dict.fromkeys([*eligible, *held]))
        sync = await self._sync(symbols, timeframe, last_open)
        monitoring = await self._monitor(timeframe, last_open)
        result: dict[str, Any] = {"candle_open_time": last_open.isoformat(), "candle_close_time": close_time.isoformat(),
                                  "symbols": len(symbols), "sync": sync, "monitoring": monitoring}
        if halted:
            result["decisions"] = {"skipped": f"SAFETY_{'/'.join(halted)}_STOP (exits keep running)"}
            return result
        if not eligible:
            result["decisions"] = {"skipped": "no eligible universe yet (universe_refresh has not produced one)"}
            return result
        result["scan"] = await self._scan(eligible, timeframe, last_open)
        decisions: dict[str, int] = {}
        details: list[dict[str, Any]] = []
        for symbol in eligible:
            outcome = await self._decide(symbol, timeframe, last_open, sync["errors"].get(symbol))
            decisions[outcome["status"]] = decisions.get(outcome["status"], 0) + 1
            if outcome["status"] != "ALREADY_PROCESSED":
                details.append(outcome)
        result["decisions"] = decisions
        result["decision_details"] = details[:50]
        return result

    # ----------------------------------------------------------------- sync
    async def _sync(self, symbols: list[str], timeframe: str, last_open: datetime) -> dict[str, Any]:
        written: dict[str, int] = {}
        errors: dict[str, str] = {}
        interval = TIMEFRAME_DELTA[timeframe]
        depth = max(self.settings.research_history_candles, self.settings.universe_min_history_candles + 100)
        for symbol in symbols:
            with self.session_factory() as session:
                # Only candles up to the last closed one count (a stray future row must not stop the sync).
                latest = session.scalar(select(MarketCandle.timestamp).where(
                    MarketCandle.exchange == self.exchange, MarketCandle.symbol == symbol,
                    MarketCandle.timeframe == timeframe, MarketCandle.timestamp <= last_open,
                ).order_by(desc(MarketCandle.timestamp)))
                start = TimeService.ensure_utc(latest) + interval if latest else last_open - interval * (depth - 1)
                if start > last_open:
                    continue
                service = HistoricalBackfillService(session)
                job = service.create(self.exchange, symbol, timeframe, start, last_open, 1000)
                try:
                    await service.run(job, self.candles)
                except Exception as exc:  # recorded per symbol; that symbol cannot trade this cycle
                    session.rollback()
                    errors[symbol] = redact(f"{type(exc).__name__}: {exc}", self.settings.secret_values())[:200]
                    continue
                if job.status != "COMPLETED":
                    errors[symbol] = str(job.failure_reason or job.status)[:200]
                written[symbol] = job.rows_written
        return {"rows_written": sum(written.values()), "symbols_synced": len(written), "errors": errors}

    # ----------------------------------------------------------------- scan
    async def _scan(self, symbols: list[str], timeframe: str, last_open: datetime) -> dict[str, Any]:
        with self.session_factory() as session:
            if not self._claim(session, SCAN_MARKER, timeframe, last_open):
                return {"status": "ALREADY_SCANNED"}
            try:
                outcome = await MarketScanner(session, self.settings, self.event_bus).scan(self.exchange, timeframe, symbols)
            except Exception as exc:
                session.rollback()
                self._finish(session, SCAN_MARKER, timeframe, last_open, "FAILED", None, None, {"error": f"{type(exc).__name__}: {exc}"[:300]})
                return {"status": "FAILED", "error": f"{type(exc).__name__}"}
            self._finish(session, SCAN_MARKER, timeframe, last_open, "DONE", None, None,
                         {"scanned": outcome["scanned"], "opportunities": outcome["opportunities"]})
            return {"status": "DONE", "scanned": outcome["scanned"], "opportunities": outcome["opportunities"]}

    # -------------------------------------------------------------- ledger
    def _claim(self, session: Session, symbol: str, timeframe: str, open_time: datetime) -> bool:
        try:
            session.add(ProcessedCandle(exchange=self.exchange, symbol=symbol, timeframe=timeframe,
                                        candle_open_time=open_time, candle_close_time=open_time + TIMEFRAME_DELTA[timeframe],
                                        status="CLAIMED", processed_at=TimeService.now()))
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False

    def _finish(self, session: Session, symbol: str, timeframe: str, open_time: datetime, status: str,
                outcome: str | None, decision_id: str | None, detail: dict[str, Any]) -> None:
        row = session.scalar(select(ProcessedCandle).where(
            ProcessedCandle.exchange == self.exchange, ProcessedCandle.symbol == symbol,
            ProcessedCandle.timeframe == timeframe, ProcessedCandle.candle_open_time == open_time))
        if row:
            row.status, row.outcome, row.decision_id, row.detail = status, outcome, decision_id, detail
            row.processed_at = TimeService.now()
        session.commit()

    # -------------------------------------------------------------- decide
    async def _decide(self, symbol: str, timeframe: str, last_open: datetime, sync_error: str | None) -> dict[str, Any]:
        interval = TIMEFRAME_DELTA[timeframe]
        decision_time = last_open + interval  # the candle's close: nothing later may be used
        with self.session_factory() as session:
            rows = session.scalars(select(MarketCandle).where(
                MarketCandle.exchange == self.exchange, MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe,
                MarketCandle.timestamp <= last_open).order_by(desc(MarketCandle.timestamp)).limit(300)).all()
            if not rows or TimeService.ensure_utc(rows[0].timestamp) != last_open:
                # Not claimed: the candle is retried on the next run once the data is there.
                return {"symbol": symbol, "status": "CANDLE_NOT_AVAILABLE", "sync_error": sync_error}
            if not self._claim(session, symbol, timeframe, last_open):
                return {"symbol": symbol, "status": "ALREADY_PROCESSED"}
            try:
                frame = candles_frame(list(reversed(rows)))
                enriched = enrich(frame)
                latest = enriched.iloc[-1]
                volatility = float(latest["volatility"]) if math.isfinite(float(latest["volatility"])) else 0.0
                _, regime = RegimeService(session).update(self.exchange, symbol, timeframe, frame)
                higher = HIGHER_TIMEFRAME.get(timeframe)
                if higher and higher in TIMEFRAME_DELTA:
                    # Higher-timeframe context from closed candles only (used by the reasoner as context).
                    context_frame = resample_closed(frame, timeframe, higher, decision_time)
                    if len(context_frame) >= 30:
                        RegimeService(session).update(self.exchange, symbol, higher, context_frame)
                scanner = session.scalar(select(Opportunity).where(
                    Opportunity.source == "scanner", Opportunity.symbol == symbol, Opportunity.timeframe == timeframe,
                    Opportunity.dedup_key == f"{self.exchange}:{symbol}:{timeframe}:{last_open.isoformat()}"))
                snapshot = MarketSnapshot(
                    symbol=symbol, price=float(latest["close"]), volume=float(latest["volume"]), volatility=volatility,
                    funding=0, regime=str(latest["regime"]).upper(), direction=str(latest["trend"]).upper(),
                    liquidity=liquidity_state(session, self.exchange, symbol), observed_at=decision_time,
                    exchange=self.exchange, timeframe=timeframe, market_regime=regime.regime,
                )
                inputs = {
                    "candle": {"exchange": self.exchange, "symbol": symbol, "timeframe": timeframe,
                               "open_time": last_open.isoformat(), "close_time": decision_time.isoformat(),
                               "close": str(rows[0].close), "volume": str(rows[0].volume), "source": "binance_rest_klines"},
                    "scanner_evidence": ({"opportunity_id": scanner.id, "signals": scanner.signals, "rank_score": scanner.rank_score}
                                         if scanner else None),
                    "provider_errors": [f"candle sync: {sync_error}"] if sync_error else [],
                }
                result = await TradingPipeline(session, self.settings, self.event_bus, self.quotes,
                                               self.ai_provider).evaluate(snapshot, inputs=inputs)
            except Exception as exc:
                session.rollback()
                error = redact(f"{type(exc).__name__}: {exc}", self.settings.secret_values())[:300]
                self._finish(session, symbol, timeframe, last_open, "FAILED", None, None, {"error": error})
                logger.warning("candle decision failed", extra={"symbol": symbol, "timeframe": timeframe,
                                                                 "candle_time": last_open.isoformat(), "error": error})
                return {"symbol": symbol, "status": "FAILED", "error": error}
            outcome = result.get("outcome")
            detail = {key: result.get(key) for key in ("risk_outcome", "risk_reasons", "order_id", "position_id",
                                                        "execution_plan_id", "rejected", "errors") if result.get(key) is not None}
            detail["reasoning"] = (result.get("reasoning") or [None])[0]
            self._finish(session, symbol, timeframe, last_open, "DECIDED", outcome, result.get("decision_id"), detail)
            logger.info("candle decision", extra={"symbol": symbol, "timeframe": timeframe, "candle_time": last_open.isoformat(),
                                                  "decision_id": result.get("decision_id"), "outcome": outcome,
                                                  "risk_result": result.get("risk_outcome"), "order_id": result.get("order_id"),
                                                  "position_id": result.get("position_id"), "execution_mode": "PAPER"})
            return {"symbol": symbol, "status": "DECIDED", "outcome": outcome, "decision_id": result.get("decision_id"), **detail}

    # ------------------------------------------------------------- monitor
    async def _monitor(self, timeframe: str, last_open: datetime) -> dict[str, Any]:
        closed: list[dict[str, Any]] = []
        marked = 0
        errors: dict[str, str] = {}
        with self.session_factory() as session:
            ids = session.scalars(select(Position.id).where(Position.status.in_(OPEN), Position.venue.in_(["paper", "testnet"]))).all()
        for position_id in ids:
            with self.session_factory() as session:
                position = session.get(Position, position_id)
                if position is None or position.status not in OPEN:
                    continue
                if position.timeframe is None:
                    position.timeframe, position.market_exchange = timeframe, self.exchange
                if position.timeframe != timeframe:
                    continue
                if position.last_evaluated_candle_at is None:
                    # The candle in which the position opened is covered by quote checks, not by its OHLC
                    # (its range partly predates the entry).
                    opened = TimeService.ensure_utc(position.opened_at)
                    position.last_evaluated_candle_at = last_closed_open(timeframe, opened) + TIMEFRAME_DELTA[timeframe]
                try:
                    outcome = await self._monitor_position(session, position, last_open)
                except Exception as exc:
                    session.rollback()
                    errors[position_id] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                if outcome.get("closed"):
                    closed.append(outcome)
                else:
                    marked += 1
        return {"open_positions_checked": len(ids), "closed": closed, "marked": marked, "errors": errors}

    async def _monitor_position(self, session: Session, position: Position, last_open: datetime) -> dict[str, Any]:
        exchange = position.market_exchange or self.exchange
        timeframe = position.timeframe or "1h"
        after = TimeService.ensure_utc(position.last_evaluated_candle_at or position.opened_at)
        candles = session.scalars(select(MarketCandle).where(
            MarketCandle.exchange == exchange, MarketCandle.symbol == position.symbol, MarketCandle.timeframe == timeframe,
            MarketCandle.timestamp > after, MarketCandle.timestamp <= last_open).order_by(MarketCandle.timestamp)).all()
        loop = PaperTradingLoop(session, self.settings, self.event_bus)
        stop, target = decimal(position.stop_loss), decimal(position.take_profit)
        for index, candle in enumerate(candles):
            position.bars_held = (position.bars_held or 0) + 1
            track_extremes(position, decimal(candle.high))
            track_extremes(position, decimal(candle.low))
            hit = candle_exit(position.side, stop, target, candle)
            is_latest = index == len(candles) - 1
            if hit is None:
                reason = self._strategy_exit(session, position, candle)
                if reason is None and position.max_holding_bars and position.bars_held >= position.max_holding_bars:
                    reason = "max_holding_period"
                if reason:
                    quote = await self._quote(position.symbol) if is_latest else None
                    touch = (quote.bid if position.side == "BUY" else quote.ask) if quote else decimal(candle.close)
                    return await self._exit(session, loop, position, candle, reason, touch, market=True,
                                            quote_source="REAL_BINANCE_BID" if quote else "CANDLE_CLOSE")
                position.last_evaluated_candle_at = TimeService.ensure_utc(candle.timestamp)
                continue
            reason, trigger = hit
            return await self._exit(session, loop, position, candle, reason, trigger,
                                    market=not reason.startswith("take_profit"), quote_source="CANDLE_OHLC")
        # Between candles: mark to the real bid and enforce stop/target on it (REST monitoring).
        quote = await self._quote(position.symbol)
        if quote is not None:
            mark = quote.bid if position.side == "BUY" else quote.ask
            if (position.side == "BUY" and mark <= stop) or (position.side == "SELL" and mark >= stop):
                return await self._exit(session, loop, position, None, "stop_loss", mark, market=True, quote_source="REAL_BINANCE_BID")
            if (position.side == "BUY" and mark >= target) or (position.side == "SELL" and mark <= target):
                return await self._exit(session, loop, position, None, "take_profit", target, market=False, quote_source="REAL_BINANCE_BID")
            self._mark(position, mark)
        elif candles:
            self._mark(position, decimal(candles[-1].close))
        session.commit()
        return {"position_id": position.id, "closed": False, "bars_held": position.bars_held,
                "mark_source": "REAL_BINANCE_BID" if quote else "CANDLE_CLOSE"}

    @staticmethod
    def _mark(position: Position, mark: Decimal) -> None:
        position.current_price = price(mark)
        track_extremes(position, price(mark))
        direction = ONE if position.side == "BUY" else -ONE
        position.unrealized_pnl = money((price(mark) - decimal(position.entry_price)) * decimal(position.quantity) * direction)

    async def _quote(self, symbol: str) -> Quote | None:
        try:
            return await self.quotes.quote(symbol)
        except QuoteUnavailable:
            return None

    def _strategy_exit(self, session: Session, position: Position, candle: MarketCandle) -> str | None:
        version = session.get(StrategyVersion, position.strategy_version_id)
        spec = spec_from_version(version, version.strategy) if version else None
        if spec is None or not spec.exit or (position.timeframe or "1h") not in spec.timeframes:
            return None
        snapshot = MarketSnapshot(symbol=position.symbol, price=float(candle.close), volume=float(candle.volume), volatility=0,
                                  funding=0, regime="", direction="", observed_at=TimeService.ensure_utc(candle.timestamp)
                                  + TIMEFRAME_DELTA[position.timeframe or "1h"], exchange=position.market_exchange or self.exchange,
                                  timeframe=position.timeframe or "1h")
        signals, _ = latest_signals(session, spec, snapshot)
        return "strategy_exit" if signals and signals["exit"] else None

    async def _exit(self, session: Session, loop: PaperTradingLoop, position: Position, candle: MarketCandle | None,
                    reason: str, trigger: Decimal, *, market: bool, quote_source: str) -> dict[str, Any]:
        slip = decimal(self.settings.paper_slippage_rate) if market else ZERO
        exit_price = price(trigger * (ONE - slip if position.side == "BUY" else ONE + slip))
        slippage_cost = money(abs(trigger - exit_price) * decimal(position.quantity))
        if candle is not None:
            position.last_evaluated_candle_at = TimeService.ensure_utc(candle.timestamp)
        session.add(PositionEvent(position_id=position.id, event_type="EXIT_TRIGGERED", from_status=position.status,
                                  to_status=position.status, price=trigger, quantity=position.quantity,
                                  reason=f"{reason}; trigger source {quote_source}"
                                         + (f"; candle {TimeService.ensure_utc(candle.timestamp).isoformat()}" if candle else "")))
        await loop.close_at(position, exit_price, reason, "candle_cycle", slippage_cost)
        if position.status == "CLOSED":
            trade = TradeMemoryService(session).record(position)
            if trade is not None:
                PostTradeAnalyst(session).analyze(trade)
            session.commit()
        return {"position_id": position.id, "closed": position.status == "CLOSED", "reason": reason,
                "exit_price": str(exit_price), "trigger_source": quote_source,
                "net_pnl": str(money(decimal(position.realized_pnl) - decimal(position.fees)))}


