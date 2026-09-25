from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ONE, ZERO, decimal, money, price, quantity, rate
from app.models import (
    Asset,
    AssetInstrument,
    PortfolioSnapshot,
    Position,
    RiskEvent,
    StrategyVersion,
    TradeIntent,
)
from app.trading.portfolio_intelligence import PortfolioIntelligence
from app.trading.safety import SafetyService


@dataclass
class RiskAssessment:
    approved: bool
    reason_codes: list[str]
    quantity: Decimal = ZERO
    risk_amount: Decimal = ZERO
    stop_loss: Decimal = ZERO
    take_profit: Decimal = ZERO
    leverage: Decimal = ONE
    notional: Decimal = ZERO


class RiskEngine:
    """Layer 3 owns final size, protection, leverage, margin and rejection."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def assess(self, intent: TradeIntent, market_price: float, volatility: float) -> RiskAssessment:
        portfolio = self.session.scalar(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at.desc())
        )
        equity = decimal(portfolio.equity) if portfolio else decimal(self.settings.starting_equity)
        available = decimal(portfolio.available_balance) if portfolio else equity
        exposure = decimal(portfolio.exposure) if portfolio else ZERO
        daily_pnl = decimal(portfolio.daily_pnl) if portfolio else ZERO
        drawdown = decimal(portfolio.drawdown) if portfolio else ZERO
        market = price(market_price)
        volatility_rate = decimal(volatility)
        open_positions = self.session.scalar(
            select(func.count()).select_from(Position).where(Position.status.in_(["OPENING", "OPEN", "MANAGING", "CLOSING"]))
        ) or 0
        reasons: list[str] = []
        if equity <= ZERO or available <= ZERO:
            reasons.append("ZERO_OR_NEGATIVE_CAPITAL")
        if equity > ZERO and daily_pnl / equity <= -decimal(self.settings.max_daily_loss):
            reasons.append("DAILY_LOSS_LIMIT")
        if drawdown >= decimal(self.settings.max_drawdown):
            reasons.append("MAX_DRAWDOWN")
        if open_positions >= self.settings.max_open_positions:
            reasons.append("MAX_POSITIONS")
        conflicting = self.session.scalar(select(func.count()).select_from(Position).where(
            Position.symbol == intent.symbol,
            Position.strategy_version_id == intent.strategy_version_id,
            Position.status.in_(["OPENING", "OPEN", "MANAGING", "PARTIALLY_CLOSING", "CLOSING"]),
        )) or 0
        if conflicting:
            reasons.append("CONFLICTING_POSITION")
        if exposure >= decimal(self.settings.max_total_exposure):
            reasons.append("MAX_EXPOSURE")
        if market <= ZERO or not (ZERO <= volatility_rate < decimal(10)):
            reasons.append("INVALID_MARKET_INPUT")
        strategy_id = self.session.scalar(
            select(StrategyVersion.strategy_id).where(StrategyVersion.id == intent.strategy_version_id)
        )
        reasons.extend(SafetyService(self.session).blocks_new_orders(strategy_id, intent.symbol))
        if reasons:
            return self._record(intent, RiskAssessment(False, reasons))

        stop_fraction = max(decimal("0.01"), min(decimal("0.05"), volatility_rate / 10 if volatility_rate else decimal("0.02")))
        target_fraction = stop_fraction * 2
        # A strategy's declared protection (validated in research) is honoured within hard bounds;
        # Risk still owns the final values and never widens them beyond those bounds.
        declared = self.session.scalar(
            select(StrategyVersion.risk_assumptions).where(StrategyVersion.id == intent.strategy_version_id)
        ) or {}
        if declared.get("stop_loss_pct"):
            stop_fraction = max(decimal("0.005"), min(decimal("0.25"), decimal(declared["stop_loss_pct"])))
            target_fraction = (max(decimal("0.005"), min(ONE, decimal(declared["take_profit_pct"])))
                               if declared.get("take_profit_pct") else stop_fraction * 2)
        stop_loss = price(market * (ONE - stop_fraction if intent.side == "BUY" else ONE + stop_fraction))
        take_profit = price(market * (ONE + target_fraction if intent.side == "BUY" else ONE - target_fraction))
        risk_amount = money(equity * decimal(self.settings.max_risk_per_trade))
        raw_quantity = risk_amount / abs(market - stop_loss)
        remaining_exposure = max(ZERO, decimal(self.settings.max_total_exposure) - exposure)
        raw_quantity = min(raw_quantity, equity * remaining_exposure / market)
        portfolio_view: dict[str, Any] = PortfolioIntelligence(self.session).assess_candidate(
            intent.symbol, 0.0, float(equity), threshold=self.settings.correlation_threshold,
            max_cluster=self.settings.max_correlated_exposure, max_symbol=self.settings.max_symbol_concentration,
        ) if equity > ZERO else {"symbol_exposure": 1.0, "cluster_exposure": 1.0, "cluster": [intent.symbol]}
        # Size down to the remaining symbol and correlated-cluster budgets; reject if none remain.
        symbol_budget = max(ZERO, decimal(self.settings.max_symbol_concentration) - decimal(portfolio_view["symbol_exposure"]))
        cluster_budget = max(ZERO, decimal(self.settings.max_correlated_exposure) - decimal(portfolio_view["cluster_exposure"]))
        budget = min(symbol_budget, cluster_budget) if len(portfolio_view["cluster"]) > 1 else symbol_budget
        if budget <= ZERO:
            reasons.append("SYMBOL_CONCENTRATION_LIMIT" if symbol_budget <= ZERO else "CORRELATED_EXPOSURE_LIMIT")
            return self._record(intent, RiskAssessment(False, reasons), portfolio_view)
        raw_quantity = min(raw_quantity, equity * budget / market)
        leverage = min(ONE, decimal(self.settings.max_leverage))
        instrument = self.session.scalar(select(AssetInstrument).where(
            AssetInstrument.exchange == "paper", AssetInstrument.exchange_symbol == intent.symbol
        ))
        asset = self.session.scalar(select(Asset).where(Asset.symbol == intent.symbol))
        step_size = instrument.step_size if instrument else (asset.step_size if asset else None)
        final_quantity = quantity(raw_quantity, step_size)
        notional = money(final_quantity * market)
        min_quantity = decimal(instrument.min_quantity if instrument else (asset.min_quantity if asset else ZERO))
        min_notional = decimal(instrument.min_notional if instrument else (asset.min_notional if asset else ZERO))
        if final_quantity <= ZERO or final_quantity < min_quantity:
            reasons.append("INVALID_OR_BELOW_MIN_QUANTITY")
        if notional < min_notional:
            reasons.append("BELOW_MIN_NOTIONAL")
        required_margin = money(notional / leverage)
        if required_margin > available:
            reasons.append("INSUFFICIENT_MARGIN")
        assessment = RiskAssessment(
            not reasons,
            reasons,
            final_quantity,
            risk_amount,
            stop_loss,
            take_profit,
            leverage,
            notional,
        )
        return self._record(intent, assessment, portfolio_view)

    def _record(
        self, intent: TradeIntent, assessment: RiskAssessment, portfolio_view: dict | None = None,
    ) -> RiskAssessment:
        portfolio = self.session.scalar(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at.desc())
        )
        self.session.add(RiskEvent(
            trade_intent_id=intent.id,
            outcome="APPROVED" if assessment.approved else "REJECTED",
            reason_codes=assessment.reason_codes,
            metrics={
                "quantity": str(assessment.quantity),
                "risk_amount": str(assessment.risk_amount),
                "notional": str(assessment.notional),
                "leverage": str(rate(assessment.leverage)),
                "portfolio_snapshot_id": portfolio.id if portfolio else None,
                "portfolio_equity": str(portfolio.equity) if portfolio else None,
                "portfolio_exposure": str(portfolio.exposure) if portfolio else None,
                "portfolio_intelligence": portfolio_view,
            },
        ))
        intent.status = "risk_approved" if assessment.approved else "risk_rejected"
        self.session.flush()
        return assessment
