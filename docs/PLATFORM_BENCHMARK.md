# Quant AI Trading Platform: launch readiness and leading-platform benchmark

> Baseline audit retained for traceability. The implementation and current closure state are tracked in [PILOT_CLOSURE.md](PILOT_CLOSURE.md) and [PILOT_VERIFICATION.md](PILOT_VERIFICATION.md); several findings below have since been addressed.

Assessment date: 14 September 2026. Source reviewed: commit `789c46cabfe9fc37ae56a8d5e871f5e1577c75d0`. Build evidence is from the preceding local verification of that revision. No production changes, broker transactions, or additional deployment were performed. This is an engineering and capability assessment, not an independently audited investment track record. A fresh remote revision check was unavailable during this follow-up; conclusions apply to the pinned revision.

## Decision

Launch a restricted founder-only paper pilot after the pilot blockers below are closed. Do not describe this release as a complete autonomous live-trading platform. Real-money order submission is physically disabled in its implementation. No verified strategy performance evidence was found that establishes positive future expectancy.

There is no defensible public certification that software is in the “top 1%.” This comparison uses documented capabilities from leading products in four different categories, not a fabricated league table or a claim to reproduce proprietary hedge-fund systems.

## Benchmark references

- **QuantConnect / LEAN:** one algorithm framework for research and live trading, realistic brokerage models and live-versus-backtest reconciliation. Its cashbook tracks currencies separately and converts values to an account currency. These are useful standards for execution consistency and accounting. [Live trading](https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading), [reconciliation](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/reconciliation), [cashbook](https://www.quantconnect.com/docs/v2/writing-algorithms/portfolio/cashbook).
- **Interactive Brokers TWS / Risk Navigator:** broker order management and portfolio-wide risk views, including hypothetical portfolios and scenario analysis. These are useful standards for operational control and portfolio risk. [Risk Navigator](https://www.interactivebrokers.com/en/trading/risk-navigator.php), [order tools](https://www.interactivebrokers.com/en/trading/ordertypes.php).
- **TradingView:** accessible strategy testing, configurable execution assumptions and intrabar fill modeling. These provide a benchmark for inspectable research workflows, not evidence that any strategy will be profitable. [Broker emulator](https://www.tradingview.com/support/solutions/43000786181-broker-emulator/), [Bar Magnifier](https://www.tradingview.com/support/solutions/43000669285-what-is-bar-magnifier-backtesting-mode/).
- **Bloomberg PORT:** integrated positions, performance attribution, data validation and multi-asset scenario/risk analytics. This is a longer-term benchmark for consistent portfolio reporting. [Portfolio & Risk Analytics](https://professional.bloomberg.com/products/bloomberg-terminal/portfolio-analytics/).

## What is good in our build

- Local verification passed: 283 Python tests, Ruff, TypeScript and a complete Next.js production build. The production dashboard rendered and its five API endpoints responded; no browser errors were captured.
- AI proposals pass through a deterministic risk warden. Position sizing, exposure limits, daily-loss and drawdown controls are explicit.
- Protective exits do not require AI approval. Stops, cooldowns, paper positions and risk state have persistence and regression coverage.
- Tick-to-bar aggregation produces closed bars; price freshness and missing-data vetoes exist.
- Paper fills include cost/friction accounting. Historical replay, replay valuation snapshots and trade proof inspection are implemented.
- Founder directives constrain instruments, markets, capital posture and open-position count.
- Reconnection supervision, cadence-failure halts, operator halt/resume and optional notifications exist.
- A read-only dashboard separates observation from order mutation.

These are useful foundations. A passed test proves the behavior tested; it does not establish production uptime, strategy profitability, data licensing, or complete integration of every module in the repository.

## Capability assessment

| Capability | Actual state | Needed for the intended outcome |
|---|---|---|
| Build and automated verification | Working on reviewed revision | Add production UI build to CI and reproducible release artifacts |
| Live price ingestion | Zerodha/IBKR stream adapters and real tick/bar path exist | Verify chosen instruments and provider sessions; record coverage, lag and reconnect evidence |
| Portfolio accounting | Persistent paper ledger, but raw INR/USD values can mix | Restrict to one currency or implement explicit currency accounts and conversion |
| Protective controls | Working paper exits and halt, checked on analysis cadence | Independent protective loop; broker-supported protection and outage plan before live money |
| Portfolio dashboard | Ledger-fill marks outside replay snapshot mode | Persist current engine marks, timestamps and source; reconcile displayed risk to engine |
| Historical research | Replay, fee/slippage modeling and lookahead tests | Licensed point-in-time history, survivorship/corporate-action handling and realistic stress scenarios |
| Walk-forward / Monte Carlo | Window-splitting and terminal-P&L bootstrap utilities | Complete experiment runner, untouched holdout, trial accounting, path/drawdown and dependence-aware simulation |
| Promotion policy | Numeric policy exists; call sites found in tests, not production runtime | Durable evidence artifacts and an enforced approval workflow for future live release |
| Strategy effectiveness | Consensus logic and strategy modules exist | Net-of-cost out-of-sample results and forward-paper evidence; no proven edge established |
| AI reliability | Structured output, fallback/veto paths and proof logging | Empirical calibration, prompt/model version pinning, drift checks and comparison against deterministic baseline |
| Broker execution | Read-only real-broker adapters; submissions route to paper | Real lifecycle, acknowledgements, partial fills, rejection/cancel races and continuous reconciliation |
| Portfolio risk breadth | Exposure caps, basic allocation and stress primitives | Correlation/sector/factor risk and asset-specific margin/Greeks before expanding scope |
| Multi-asset completeness | Domain types exceed operational coverage | Contract IDs, lot/multiplier/expiry/tick-size/session/settlement support per enabled instrument |
| Security / SaaS | FastAPI key/tenant/rate-limit primitives; read-only Next routes | Dashboard access control, persistent credentials, isolation, secret management and external review before customer access |
| Operations | Logs, retry/halt logic and a daemon Dockerfile | Complete deployment configuration, independent alerts, recovery/restore drill and release rollback |

## Specific findings and required fixes

### 1. Currency correctness — pilot blocker unless scope is restricted

The supplied directives permit both INR and USD. OrderIntent has no currency, the paper broker debits price × quantity from one balance, and portfolio metrics add those values directly. An isolated zero-slippage test deducted 100 for an India purchase at 100 and another 100 for a US purchase at 100. That cannot represent a coherent mixed-currency portfolio.

**Fast fix for pilot scope:** allow only INR-denominated NSE cash equities/ETFs, in a dedicated account. Enforce the restriction in configuration validation rather than relying on operator memory. Full multi-currency accounting remains a later prerequisite for US expansion.

Evidence: `src/quant_ai/domain/models.py`, `execution/paper_ledger.py`, `execution/portfolio.py`, `deploy/founder-directives.example.json`.

### 2. Protection latency and stale data — pilot engineering priority; live blocker

The runner calls analysis every ten minutes, and run_once applies operator halt and protective exits. A stop crossing between checks can be missed or acted on late; processing failures can extend the delay. Logical independence from the AI vote is good, but it is not independent scheduling.

**Acceptance:** a delayed/hung LLM must not delay halt processing or protective monitoring. Inject stop crossings, feed outages, duplicate ticks, process restart and database contention; verify the resulting state and alerts. Before live trading, use permitted broker/exchange-side protection where supported, verify acknowledgement and define a contingency for failed protection. Stop prices are trigger levels, not guaranteed realized loss caps; stop-limit orders can fail to fill. [IBKR order-risk discussion](https://us2.interactivebrokers.com/en/trading/orders/balance-impact-risk.php).

### 3. Honest performance reporting — pilot blocker

The execution brief receives Sharpe and Sortino from pipeline analytics built from recent instrument candle returns. Those are not strategy account-return statistics. The separate backtest tearsheet does use an equity curve, but that does not correct the execution brief.

**Acceptance:** strategy metrics must come from timestamped portfolio equity after costs; market metrics must be labeled separately. Report benchmark, return interval, sample length, realized/unrealized P&L, costs and data provenance. Dashboard and engine must agree on current portfolio valuation within a specified rounding tolerance.

Evidence: `intelligence/pipeline.py`, `execution/scheduler.py`, `backtesting/tearsheet.py`, `apps/pramana-ui/lib/portfolio.ts`.

### 4. Data and feature readiness — pilot blocker

Ghost fundamentals default to sandbox; news/macro also fall back to sandbox without configured providers. The local dashboard snapshot observed earlier reported missing FRED, licensed fundamentals and IBKR; these were snapshot reports, not independent credential checks. Fresh timestamps alone do not turn synthetic constants into real evidence.

**Acceptance:** every enabled decision feature has a source, observation time, availability status and documented fallback. Either supply licensed inputs or disable/reweight dependent signals explicitly and revalidate that strategy. Do not substitute a sandbox value in a supposedly real-data evaluation. Verify holiday overrides, symbol mapping and correct trade timestamps; render epoch-zero index timestamps as unavailable.

### 5. Deployment and access — pilot blocker

Compose omits newer founder, news, macro, holiday and Telegram settings and does not mount a directives file. It contains the daemon, not the dashboard. The Next.js routes reviewed do not enforce user authentication; the Python API's authentication is a separate path.

**Acceptance:** boot from a clean deployment with the intended directives, tenant and shared persistent ledger/proofs; use loopback/VPN or an authenticated gateway for the private pilot. Verify backup restore, restart, independent heartbeat alerts and log retention. No external messaging was sent during this assessment.

### 6. Live order management — mandatory before real money

`LIVE_ORDER_NETWORK_REQUESTS_ENABLED=False`; the live factory always refuses creation; real-broker adapters submit to the paper ledger. Enabling an environment variable is not a live implementation.

**Acceptance:** implement and independently verify one broker integration, including accepted/rejected/partial/filled/cancelled states, durable client order IDs, uncertain submission recovery, account/order/position reconciliation, buying power checks, authorized instruments and protection after partial fills. Any unresolved broker-state mismatch must block new risk.

### 7. Asset and venue completeness — restrict launch scope

Session checks are keyed by market, not full instrument venue/contract. One India session cannot establish complete behavior for equities, CDS and MCX. Generic labels such as GOLD or CRUDEOIL are not a complete execution contract specification. Bare indices are observation instruments, not automatically executable products.

**Acceptance before expansion:** full contract mapping, tradeability, lot/tick size, multiplier, expiry/roll, margin, settlement and venue-calendar tests. Keep derivatives, leveraged products and cross-border execution outside the initial pilot.

### 8. Broker and regulatory readiness — live/customer launch blocker

Requirements depend on whether this is a personal system, an algo-provider product, advice/research, or a customer execution platform. For Zerodha API orders, its current support page requires a static IP from 1 April 2026; it distinguishes market-data/read-only access from order placement. Verify the applicable broker and exchange requirements for the exact product, rather than treating possession of credentials as approval. [Zerodha static IP rules](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/static-ip), [SEBI framework](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html), [NSE algo-provider framework](https://www.nseindia.com/static/trade/empanelled-algo-providers-exchange).

## How to establish effectiveness

The objective should be durable net returns under a pre-agreed risk budget. More signals, models or markets do not by themselves create an edge. No return, win-rate or maximum-loss forecast is supported by this review.

1. Fix accounting and measurements before optimizing strategy parameters.
2. Freeze one strategy, its universe, holding horizon, execution rules and version. Log each experiment, including failures, to avoid selecting a lucky result from many trials.
3. Compare it with cash, a suitable passive benchmark and a simple deterministic strategy under comparable exposure and costs.
4. Evaluate on untouched historical periods and walk-forward windows across different conditions. Use only information available at each simulated decision time, including point-in-time fundamentals and macro releases if enabled.
5. Include spread, brokerage, statutory fees, latency, gaps, rejects, liquidity limits and infrastructure/AI costs in the appropriate performance report. Stress costs at multiples of the baseline assumption.
6. Measure net expectancy, profit factor, maximum drawdown, drawdown duration, tail losses, turnover, concentration and benchmark-relative return. Estimate uncertainty using methods that account for dependent trades, not just a raw trade count.
7. Run the full swarm and deterministic baseline in parallel paper experiments. Keep the LLM only where evidence shows incremental value after cost and latency.
8. Collect real-feed paper evidence and compare hypothetical fills against observed tradable prices. QuantConnect's reconciliation workflow is a useful reference for identifying differences between simulation and operation. [Reconciliation](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/reconciliation).

The repository's default promotion policy specifies at least 100 trades, 30 paper days, positive expectancy, profit factor at least 1.2, profitable results in at least two regimes, and maximum drawdown at most 10%. These are existing software defaults—not an industry certification, statistically sufficient sample, acceptable personal loss budget or proof that the build has met them. Their evaluation is currently a utility rather than an enforced release process. Record completed round trips versus order fills explicitly and define how paper days and regimes are measured.

For example, 40% wins averaging 2R and 60% losses averaging 1R imply 0.2R gross expectancy before costs. This illustrates why win rate alone is insufficient; it is not a prediction about this strategy. Gaps can also make a realized loss exceed planned 1R.

## Fastest defensible launch sequence

| Stage | Scope | Exit evidence |
|---|---|---|
| Private paper pilot | One operator, one broker feed, INR-only cash equities/ETFs, one strategy, no leverage | Pilot blockers closed; source-labeled inputs; accounting/UI parity; restart/stop/outage drills; complete paper trade-to-proof flow |
| Performance validation | Same frozen scope, historical holdout plus forward paper observation | Auditable net-of-cost results, benchmark comparison, uncertainty/stress analysis and promotion criteria met |
| Micro-capital live pilot | One supported broker, approved instruments, explicit capital ceiling | Live lifecycle/reconciliation and protection tested; applicable permissions confirmed; human approval |
| Wider rollout | Additional capital, markets, assets or customers individually | Repeat validation for each material change; tenant security and operational capacity appropriate to scope |

A 24–48-hour run is an operational smoke test, not proof of profitability. Do not promise a live date from the build result alone. Engineering can accelerate the pilot; adequate market observation cannot be manufactured. Customer launch and expansion to every asset class should not be prerequisites for learning from the first paper pilot.

## Longer-term quality targets

After the narrow pilot: correlation/factor exposure, portfolio stress scenarios, durable model/data version registry, drift and calibration monitoring, execution-cost attribution, realistic partial-fill models, point-in-time historical data, broker-state reconciliation, recovery exercises and secure multi-user operations. Add advanced order algorithms only when measured size/liquidity needs justify them. Matching every feature across four established products would slow launch without proving superior returns.

The priority is a complete and measurable path from valid data to an auditable decision, constrained order, verified fill and reconciled portfolio.

## Implementation delta — intrabar execution

Historical replay now supports complete lower-timeframe OHLC windows, chronological stop/target triggers, conservative ambiguous-bar resolution, worse-price stop gaps and explicit execution assumptions in the tearsheet. [Implementation and limits](INTRABAR_REPLAY.md). This is a partial advance on the documented TradingView Bar Magnifier comparison; realistic partial fills, queue/latency behavior and real-data qualification remain unclosed. The separate deterministic SMA experiment is unchanged.

## Implementation delta — portfolio what-if and concentration

The workspace now provides distinct shocks by holding, per-holding scenario P&L and aggregate equity impact, cash-inclusive exposure weights, largest-position concentration, effective holding count, and recorded-stop downside diagnostics. The scenario passes its actual per-holding settings into the Atlas prompt. Missing stops, breached thresholds and stale marks are visible. These cash-equity/ETF diagnostics partially advance the IBKR/Bloomberg what-if and portfolio-risk comparison. They are not correlation diversification, factor/sector risk, Greeks, broker-native protection or Bloomberg-style attribution. Those requirements remain open.

## Implementation delta — internal accounting reconciliation

Paper account state is now reconstructed from fills and recorded cash-debit fees and compared with persisted cash, quantities, average prices and protection levels. Pilot entries fail closed on discrepancies, daemon fault halts persist, and the dashboard exposes the checked ledger version and freshness. [Rules and evidence](PAPER_RECONCILIATION.md). This improves the accounting foundation but does not substitute for QuantConnect-style live/backtest reconciliation or an independent broker order/cash/position reconciliation service; those remain open.
