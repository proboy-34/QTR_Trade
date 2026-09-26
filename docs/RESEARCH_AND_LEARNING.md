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

**Trade review.** With `AI_TRADE_REVIEW=advisory` (default) the AI reviews each TRADE proposal
using only the recorded evidence and returns `SUPPORT`, `REJECT` or `UNCERTAIN` with reasons;
the verdict is stored on the decision rationale. With `veto`, a `REJECT` turns the TRADE into
NO TRADE. The AI can never create a trade, change a size, stop or target. If Gemini is not
configured or fails, the review is recorded as `NOT_CONFIGURED` / `UNAVAILABLE` and the
deterministic rules decide alone — no substitute text is generated.

## Point-in-time data (no look-ahead)

- Candles: signals use closed candles only; backtests fill on the next bar.
- News/events: usable only once `knowable_at` (= `available_at`, else `published_at`, else
  `event_at`) is at or before the decision time. Earlier versions allowed events up to one
  hour (lineage) or 24 hours (scanner) *after* the decision; both were fixed.
- Scheduled macro events are used as risk context only if the calendar entry was retrieved
  before the decision.
- FRED: each value is stored per vintage. A value first published on day D is treated as
  available from D+1 00:00 UTC (release times are not provided by FRED), and revisions are only
  visible from their own vintage date. `macro_as_of(t)` therefore never returns a later revision.
- Regimes: the reasoner only reads regime records whose candle closed before the decision.

## Research data, regime testing and eligibility

- Research uses the closed Binance candles synced by the REST loop (`RESEARCH_HISTORY_CANDLES`, default 2000 per asset; about 83 days of 1h candles).
- The robustness gate includes regime testing: trades must span at least 2 regimes (1 for regime-restricted specifications), and no regime with 5 or more trades may have a profit factor below 0.6.
- When the research backlog is empty, a baseline programme tests the reviewed templates (EMA trend change, volume-confirmed breakout, volatility expansion) on the most liquid eligible assets. These are ordinary hypotheses and most are expected to fail.
- A promoted candidate stores an explicit `validation_summary`: dataset, training and validation periods, walk-forward windows, costs, metrics, robustness, regime coverage and gate thresholds, plus `validated_at`. `/api/v1/strategies/lifecycle` shows candidate, validated, paper-eligible, active, disabled and rejected items, with reasons.
