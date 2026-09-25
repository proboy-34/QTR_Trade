# Research, validation and learning

QTR "learns" in a measurable, auditable sense:

```text
new evidence → stored → compared with prior evidence → patterns → hypotheses
→ tested → validated or rejected → future decisions informed
```

Nothing in QTR is a black-box model. Every step writes records you can inspect.

## Where ideas come from

| Origin | Source | Code |
| --- | --- | --- |
| `operator` | Research lab form or `POST /api/v1/research/hypotheses` | `HypothesisEngine.create` |
| `scanner` | Scanner signals mapped to reviewed templates | `app/research/generator.py` |
| `ai` | Gemini proposals (budgeted, firewalled) | `app/ai/research.py` |
| `challenger` | Variation of an existing strategy | `app/research/challengers.py` |
| `strategy_health` | Follow-up after degradation (regime-restricted variant) | `app/learning/strategy_health.py` |

Every idea must be a **declarative specification** (`app/research/dsl.py`):

```json
{
  "side": "long",
  "entry": [{"left": "ema:{fast}", "operator": "crosses_above", "right": "ema:{slow}"}],
  "exit":  [{"left": "ema:{fast}", "operator": "crosses_below", "right": "ema:{slow}"}],
  "risk": {"stop_loss_pct": 0.03, "take_profit_pct": 0.06},
  "timeframes": ["1h"], "universe": ["SOLUSDT"], "parameters": {"fast": 12, "slow": 36},
  "regimes": []
}
```

Operands are numbers, plain features (`close`, `open`, `high`, `low`, `volume`, `vwap`) or
period features (`ema`, `sma`, `rsi`, `atr_pct`, `return`, `volatility`, `volume_ratio`,
`highest`, `lowest`, `efficiency`) written `name:N` or `name:{param}`. Anything else is
rejected — no code is generated or executed, including by the AI. Existing repository
versions (`ema_fast crosses_above ema_slow`) are valid specifications.

## Validation gates (fixed in code, `ValidationGates`)

| Stage | Checks (default thresholds) |
| --- | --- |
| Research | Specification valid; ≥300 stored bars per asset; dataset frozen and fingerprinted |
| In-sample backtest (first 70%) | ≥15 trades, profit factor ≥1.0, max drawdown ≥ −30% |
| Out-of-sample (last 30%) | ≥8 trades, PF ≥1.0, expectancy >0, OOS/IS expectancy ≥0.25, **beats exposure-adjusted buy-and-hold** |
| Walk-forward (5 rolling unseen windows) | ≥3 windows with trades, ≥50% profitable, aggregate PF ≥1.0 |
| Robustness | Parameter-perturbation stability ≥60%, profitable at 2× costs, Monte Carlo p5 drawdown ≥ −35%, loss probability ≤40%, **deflated Sharpe ≥0.95** |
| Challenger (if any) | OOS expectancy above the unchanged champion, drawdown ≤5 points worse |
| Paper validation | ≥10 closed paper trades, positive expectancy, PF ≥1.0, consistent with OOS expectation within 2 standard errors |

Execution in research is next-bar-open, stops are assumed to hit before targets in the same
bar, gaps fill at the worse price, and fees + slippage + half-spread apply on both legs.

**Data-snooping awareness.** Every experiment is persisted (including failures). The deflated
Sharpe ratio uses the number of completed backtest/perturbation experiments for the same
timeframe as the trial count, so the more ideas QTR tries, the higher the bar.

Calibration evidence at the time of writing (synthetic data, default gates): an EMA rule on
30 driftless random walks passed **0/30**; on 10 oscillating markets with a genuine timing
edge it passed **10/10**. A trending market where the rule merely rode drift failed the
passive-benchmark gate — the intended behaviour.

## After validation

```text
ROBUSTNESS PASS → Strategy created (draft → under_validation → paper_testing)
→ Decision layer trades it with paper money (same entry/exit rules, declared stops)
→ paper validation PASS → ready_for_review → an operator approves/activates (or rejects)
```

Research can never approve or activate a strategy. Old versions are never overwritten.

## Memory and learning records

| Record | Written when | Endpoint |
| --- | --- | --- |
| Market memory (features, regime per closed candle) | Scanner / live candles | `POST /memory/market/similar` |
| Decision lineage (code version, strategy hashes, risk config, events, regime, AI artifacts) | Every decision | `GET /memory/decisions/{id}` |
| Trade memory (immutable; MAE/MFE, slippage, regime, events, R) | Every paper close | `GET /memory/trades` |
| Post-trade analysis + lesson | Learning job | `GET /learning/post-trade` |
| Counterfactuals ("why did we not trade?") | Risk rejections, near misses, unacted scanner opportunities | `GET /learning/counterfactuals` |
| Strategy health (HEALTHY / REVIEW_REQUIRED / DEGRADED / INSUFFICIENT_DATA) | Learning job | `GET /strategies/{id}/health` |
| Backtest vs paper discrepancy | Paper validation | `GET /learning/discrepancies` |
| Knowledge base (findings, failed ideas, lessons, patterns) | Throughout | `GET /memory/knowledge` |

Counterfactuals and degradation never change rules by themselves; they become evidence and,
where appropriate, new hypotheses that go through the same gates.

## AI

The AI receives QTR evidence with reference ids and must answer as typed claims. A `FACT`
is accepted only if it cites a source-verified event or a system metric that QTR supplied;
otherwise it is stored as `UNVERIFIED`. Metrics must match QTR's number. AI proposals become
hypotheses (or recorded rejections). Calls are budgeted (`AI_DAILY_BUDGET`,
`AI_MONTHLY_BUDGET`, `AI_MAX_REQUESTS_PER_HOUR`), retried with backoff, cached for
identical evidence, and logged with tokens, approximate cost, latency and errors.
The AI is only called for high-ranked opportunities and new trades, never per tick.
