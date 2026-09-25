"""Structured AI research tasks.

The AI receives QTR evidence (verified events, system metrics) with reference ids and must
answer in typed claims. Its output becomes an AIArtifact; proposals become hypotheses
that still face every quantitative gate. The AI has no path to decisions or orders.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.ai.firewall import ClaimVerifier, EvidenceRegistry, MalformedAIResponse, parse_json
from app.ai.gateway import AIBudgetExceeded, AIGateway, AIUnavailable
from app.ai.providers import AIProvider, AIRequest, build_provider
from app.core.config import Settings
from app.core.events import EventBus
from app.core.lineage import fingerprint
from app.core.time import TimeService
from app.global_services.market_intelligence import knowable_at
from app.global_services.regime import load_candles
from app.integrations.fred import macro_as_of
from app.memory.market_memory import context_features
from app.memory.trade_memory import base_asset
from app.models import (
    AIArtifact,
    AICallRecord,
    MarketEvent,
    MarketRegimeRecord,
    Opportunity,
    PostTradeAnalysis,
    Strategy,
    StrategyHealthRecord,
    TradeMemory,
)
from app.research.dsl import OPERATORS, PERIOD_FEATURES, PLAIN_FEATURES, TIMEFRAMES

PROMPT_VERSION = "v1"
SYSTEM_PROMPT = """You are the research analyst of QTR, a quantitative paper-trading research platform.
You do not place, recommend, size or time trades. Your output is research material only.
Answer with a single JSON object: {"summary": string, "claims": [{"type": "FACT"|"INTERPRETATION"|"HYPOTHESIS"|"METRIC", "text": string, "refs": [string]}], "proposals": [object]}.
Rules:
- A FACT must cite one or more refs from EVIDENCE. Never state a fact that is not in EVIDENCE.
- A METRIC must repeat a numeric value exactly as given in EVIDENCE and cite its ref.
- Everything else (explanations, possibilities, opinions) is INTERPRETATION or HYPOTHESIS.
- Never invent news, prices, flows, events or sources. If evidence is insufficient, say so.
- Items marked UNVERIFIED_SOURCE must not be presented as facts."""

DSL_GUIDE = (
    "A strategy proposal must contain a 'spec' object: {\"side\": \"long\", \"entry\": [conditions], \"exit\": [conditions], "
    "\"risk\": {\"stop_loss_pct\": number, \"take_profit_pct\": number}, \"timeframes\": [timeframe], \"universe\": [symbols], "
    "\"parameters\": {name: number}, \"regimes\": [optional regime names]}. A condition is {\"left\": operand, \"operator\": op, "
    "\"right\": operand}. Operands are numbers, plain features " + ", ".join(sorted(PLAIN_FEATURES)) +
    ", or period features written name:N or name:{param} with name in " + ", ".join(sorted(PERIOD_FEATURES)) +
    ". Operators: " + ", ".join(sorted(set(OPERATORS.values()))) + ". Timeframes: " + ", ".join(TIMEFRAMES) +
    ". Also give 'statement', 'rationale', 'assumptions' (list) and 'market_conditions' (object)."
)


class AIResearchService:
    def __init__(self, session: Session, settings: Settings, event_bus: EventBus,
                 provider: AIProvider | None = None, gateway: AIGateway | None = None) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus
        self.provider = provider or build_provider(settings)
        self.gateway = gateway or AIGateway(session, settings, self.provider)

    # ------------------------------------------------------------- evidence
    def _event_evidence(self, registry: EvidenceRegistry, events: list[MarketEvent]) -> None:
        for event in events:
            kind = "SOURCE_FACT" if event.verification_status in {"SOURCE_VERIFIED", "OPERATOR_ENTERED"} else "UNVERIFIED_SOURCE"
            registry.add(f"event:{event.id}", kind, {
                "title": event.title, "type": event.event_type, "source": event.source, "provider": event.provider,
                "published_at": event.event_at, "assets": event.affected_assets, "verification": event.verification_status,
                "expected": event.expected_value, "actual": event.actual_value, "surprise": event.surprise,
            })

    def market_evidence(self, exchange: str, symbol: str, timeframe: str) -> EvidenceRegistry:
        registry = EvidenceRegistry()
        frame = load_candles(self.session, exchange, symbol, timeframe, 300)
        if not frame.empty:
            registry.add("metric:last_close", "SYSTEM_METRIC", {"symbol": symbol, "close": float(frame["close"].iloc[-1]),
                         "candle_at": str(frame["timestamp"].iloc[-1])}, float(frame["close"].iloc[-1]))
            for key, value in context_features(frame).items():
                if value is not None:
                    registry.add(f"metric:{key}", "SYSTEM_METRIC", {"feature": key, "value": value}, value)
        regime = self.session.scalar(select(MarketRegimeRecord).where(
            MarketRegimeRecord.exchange == exchange, MarketRegimeRecord.symbol == symbol, MarketRegimeRecord.timeframe == timeframe,
        ).order_by(desc(MarketRegimeRecord.candle_timestamp)))
        if regime:
            registry.add(f"regime:{regime.id}", "SYSTEM_METRIC", {"regime": regime.regime, "confidence": regime.confidence,
                         "previous": regime.previous_regime, "candle_at": regime.candle_timestamp}, regime.confidence)
        opportunity = self.session.scalar(select(Opportunity).where(
            Opportunity.source == "scanner", Opportunity.symbol == symbol,
        ).order_by(desc(Opportunity.created_at)))
        if opportunity:
            registry.add(f"opportunity:{opportunity.id}", "SYSTEM_METRIC", {"signals": opportunity.signals,
                         "rank_score": opportunity.rank_score, "status": opportunity.status}, opportunity.rank_score)
        base = base_asset(symbol)
        known = knowable_at()
        events = [event for event in self.session.scalars(select(MarketEvent).where(
            known >= TimeService.now() - timedelta(days=2), known <= TimeService.now(),
        ).order_by(desc(MarketEvent.event_at)).limit(200)).all()
            if base in (event.affected_assets or []) or "RISK_ASSETS" in (event.affected_assets or [])][:15]
        self._event_evidence(registry, events)
        for series_id, item in macro_as_of(self.session, TimeService.now()).items():
            registry.add(f"macro:{series_id}", "SOURCE_FACT", item, item["value"])
        return registry

    # ---------------------------------------------------------------- core
    async def _run(self, task_type: str, subject_type: str, subject_id: str | None, registry: EvidenceRegistry,
                   instruction: str) -> dict[str, Any]:
        context_hash = fingerprint({"task": task_type, "subject": subject_id, "evidence": registry.prompt_block(),
                                    "instruction": instruction, "prompt_version": PROMPT_VERSION})
        cached = self.session.scalar(select(AIArtifact).where(
            AIArtifact.task_type == task_type, AIArtifact.subject_id == subject_id,
            AIArtifact.created_at >= TimeService.now() - timedelta(hours=6),
        ).order_by(desc(AIArtifact.created_at)))
        if cached and (cached.verification or {}).get("context_fingerprint") == context_hash:
            return {"status": "CACHED", "artifact_id": cached.id}
        prompt = f"TASK: {instruction}\n\nEVIDENCE (cite by ref):\n{registry.prompt_block()}"
        request = AIRequest(task_type, SYSTEM_PROMPT, prompt, self.settings.ai_max_output_tokens)
        try:
            result = await self.gateway.run(request)
        except (AIUnavailable, AIBudgetExceeded) as exc:
            return {"status": "UNAVAILABLE", "error": str(exc)}
        try:
            payload = parse_json(result.response.text)
        except MalformedAIResponse as exc:
            call = self.session.get(AICallRecord, result.call.id)
            if call:
                call.status, call.error = "MALFORMED_RESPONSE", str(exc)
            self.session.commit()
            return {"status": "MALFORMED_RESPONSE", "error": str(exc), "ai_call_id": result.call.id}
        claims, stats = ClaimVerifier(registry).verify(payload.get("claims", []))
        artifact = AIArtifact(
            ai_call_id=result.call.id, task_type=task_type, subject_type=subject_type, subject_id=subject_id,
            summary=str(payload.get("summary", ""))[:4000], claims=claims,
            proposals=[item for item in payload.get("proposals", []) if isinstance(item, dict)][:10],
            context_refs=list(registry.items), model=result.response.model, prompt_version=PROMPT_VERSION,
            verification={**stats, "context_fingerprint": context_hash,
                          "policy": "FACT requires QTR-supplied source evidence; otherwise UNVERIFIED"},
        )
        self.session.add(artifact)
        self.session.commit()
        return {"status": "OK", "artifact_id": artifact.id, "verification": stats}

    # --------------------------------------------------------------- tasks
    async def market_context(self, exchange: str, symbol: str, timeframe: str) -> dict[str, Any]:
        registry = self.market_evidence(exchange, symbol, timeframe)
        return await self._run("MARKET_CONTEXT_SUMMARY", "symbol", symbol, registry,
                               f"Summarize the current {symbol} {timeframe} market context and note anything unusual. "
                               "State clearly which statements are facts from evidence and which are interpretation.")

    async def interpret_event(self, event_id: str) -> dict[str, Any]:
        event = self.session.get(MarketEvent, event_id)
        if not event:
            return {"status": "NOT_FOUND"}
        registry = EvidenceRegistry()
        self._event_evidence(registry, [event])
        return await self._run("NEWS_INTERPRETATION", "market_event", event_id, registry,
                               "Interpret the possible market relevance of this event for crypto assets. "
                               "Do not add facts that are not in the evidence.")

    async def generate_hypotheses(self, exchange: str, symbol: str, timeframe: str) -> dict[str, Any]:
        from app.research.hypotheses import HypothesisEngine

        registry = self.market_evidence(exchange, symbol, timeframe)
        outcome = await self._run("HYPOTHESIS_GENERATION", "symbol", symbol, registry,
                                  f"Propose up to 3 testable long-only research hypotheses for {symbol} on {timeframe}. {DSL_GUIDE} "
                                  f"Use universe [\"{symbol}\"] and timeframes [\"{timeframe}\"].")
        artifact = self.session.get(AIArtifact, outcome.get("artifact_id")) if outcome.get("artifact_id") else None
        if not artifact or outcome["status"] == "CACHED":
            return outcome
        engine = HypothesisEngine(self.session, self.settings, self.event_bus)
        created: list[str] = []
        rejected: list[str] = []
        for proposal in artifact.proposals:
            spec = proposal.get("spec") if isinstance(proposal.get("spec"), dict) else {}
            hypothesis = engine.create(
                str(proposal.get("statement") or "AI proposed hypothesis")[:1000], origin="ai", spec=spec,
                description=str(proposal.get("rationale") or "")[:4000],
                market_conditions=proposal.get("market_conditions") if isinstance(proposal.get("market_conditions"), dict) else {},
                assumptions=[str(item) for item in proposal.get("assumptions", []) if isinstance(item, str)][:10],
                ai_artifact_id=artifact.id, variables={"exchange": exchange},
            )
            (rejected if hypothesis.stage == "REJECTED" else created).append(hypothesis.id)
        artifact.verification = {**(artifact.verification or {}), "hypotheses_created": created, "proposals_rejected": rejected}
        self.session.commit()
        return {**outcome, "hypotheses_created": created, "proposals_rejected": rejected}

    async def analyze_trade(self, trade_memory_id: str) -> dict[str, Any]:
        trade = self.session.get(TradeMemory, trade_memory_id)
        if not trade:
            return {"status": "NOT_FOUND"}
        registry = EvidenceRegistry()
        registry.add(f"trade:{trade.id}", "SYSTEM_METRIC", {
            "symbol": trade.symbol, "side": trade.side, "return_pct": trade.return_pct, "r_multiple": trade.r_multiple,
            "mae_pct": trade.mae_pct, "mfe_pct": trade.mfe_pct, "exit_reason": trade.exit_reason,
            "holding_seconds": trade.holding_seconds, "regime_at_entry": trade.regime_at_entry,
            "regime_at_exit": trade.regime_at_exit, "net_pnl": str(trade.net_pnl),
        }, trade.return_pct)
        analysis = self.session.scalar(select(PostTradeAnalysis).where(PostTradeAnalysis.trade_memory_id == trade.id))
        if analysis:
            registry.add(f"analysis:{analysis.id}", "SYSTEM_METRIC", {"expected": analysis.expected, "actual": analysis.actual,
                         "factors": analysis.factors, "thesis_correct": analysis.thesis_correct})
        events = self.session.scalars(select(MarketEvent).where(MarketEvent.id.in_(trade.event_ids or []))).all()
        self._event_evidence(registry, list(events))
        outcome = await self._run("POST_TRADE_ANALYSIS", "trade", trade.id, registry,
                                  "Explain what may account for this paper trade's outcome, using only the evidence. "
                                  "Separate factual observations from interpretation; suggest at most two follow-up research questions as HYPOTHESIS claims.")
        if analysis and outcome.get("artifact_id"):
            analysis.ai_artifact_id = outcome["artifact_id"]
            self.session.commit()
        return outcome

    async def explain_degradation(self, strategy_id: str) -> dict[str, Any]:
        strategy = self.session.get(Strategy, strategy_id)
        record = self.session.scalar(select(StrategyHealthRecord).where(
            StrategyHealthRecord.strategy_id == strategy_id).order_by(desc(StrategyHealthRecord.evaluated_at)))
        if not strategy or not record:
            return {"status": "NOT_FOUND"}
        registry = EvidenceRegistry()
        registry.add(f"health:{record.id}", "SYSTEM_METRIC", {"status": record.status, "historical": record.historical,
                     "recent": record.recent, "by_regime": record.by_regime, "reasons": record.reasons})
        return await self._run("DEGRADATION_EXPLANATION", "strategy", strategy_id, registry,
                               f"Suggest possible explanations for the change in performance of strategy '{strategy.name}'. "
                               "These are hypotheses for research, not conclusions.")

    async def review_trade(self, rationale: dict[str, Any]) -> dict[str, Any]:
        """Second opinion on a TRADE proposal. The AI may only argue for NO_TRADE; it cannot
        create, enlarge or re-price a trade. Its inputs are exactly QTR's recorded evidence."""
        registry = EvidenceRegistry()
        for index, item in enumerate(rationale.get("supporting_evidence", [])):
            registry.add(f"support:{index}", "SYSTEM_METRIC", item)
        for index, item in enumerate(rationale.get("contradicting_evidence", [])):
            registry.add(f"contra:{index}", "SYSTEM_METRIC", item)
        for series_id, item in (rationale.get("macro_context") or {}).items():
            registry.add(f"macro:{series_id}", "SOURCE_FACT", item, item.get("value"))
        events = self.session.scalars(select(MarketEvent).where(MarketEvent.id.in_(rationale.get("news_context") or []))).all()
        self._event_evidence(registry, list(events))
        registry.add("proposal", "SYSTEM_METRIC", {key: rationale.get(key) for key in (
            "asset", "direction", "thesis", "timeframe_roles", "market_regime", "invalidation", "time_horizon", "evidence_strength")})
        outcome = await self._run(
            "TRADE_REVIEW", "decision_proposal", f"{rationale.get('asset')}:{rationale.get('decision_time')}", registry,
            "Review this LONG trade proposal as a disciplined, risk-first trader. Identify evidence that supports it, evidence "
            "that contradicts it, and anything that would invalidate it. Return exactly one proposal object: "
            '{"verdict": "SUPPORT" | "REJECT" | "UNCERTAIN", "reasons": [string], "missing_information": [string]}. '
            "Choose REJECT if contradicting evidence is material. Do not propose prices or sizes.")
        if outcome.get("status") not in {"OK", "CACHED"}:
            return {"status": "UNAVAILABLE", "error": outcome.get("error"), "verdict": None,
                    "fallback": "deterministic evidence rules only (AI review not performed)"}
        artifact = self.session.get(AIArtifact, outcome["artifact_id"])
        proposal = next((item for item in (artifact.proposals if artifact else []) if isinstance(item, dict)), {})
        verdict = str(proposal.get("verdict", "UNCERTAIN")).upper()
        if verdict not in {"SUPPORT", "REJECT", "UNCERTAIN"}:
            verdict = "UNCERTAIN"
        return {"status": outcome["status"], "artifact_id": outcome["artifact_id"], "verdict": verdict,
                "reasons": [str(reason)[:300] for reason in proposal.get("reasons", [])][:8],
                "missing_information": [str(item)[:200] for item in proposal.get("missing_information", [])][:8],
                "verification": artifact.verification if artifact else {}}
