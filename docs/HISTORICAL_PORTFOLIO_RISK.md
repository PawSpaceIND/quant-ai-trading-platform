# Historical cash-portfolio risk diagnostic

The private Risk lab now estimates how the current INR cash-equity/ETF portfolio would respond to historical daily price changes. It provides covariance/correlation, instrument volatility contributions, a diversification diagnostic, 95% historical VaR/expected shortfall and inspectable loss scenarios. Every result is labelled **exploratory**. It is not account performance, a forecast, a guaranteed loss limit or accepted strategy evidence.

## Source and coverage contract

The market collector publishes `riskHistory` schema `pramana.risk_history.v1` alongside its existing snapshot. Its historical request now covers up to 400 calendar days, starting no earlier than 1 January 2026. Existing once-per-local-date/provider-token caching remains. A failed history request leaves no usable history; it does not fabricate prices. The producer preserves duplicate/invalid closes so the consumer can reject them.

Each instrument carries symbol, market, asset class, currency, exchange and provider token. The collector's fixed mapping distinguishes indices, equities and the existing gold/silver ETFs. This mapping is a declaration, not an independently maintained security master. Every held instrument must match exactly; an index or differently named ETF cannot stand in for a missing holding.

The producer includes the bundled NSE cash-session calendar, including the documented 1 February Budget session and only dates strictly before the capture's Asia/Kolkata date. Today's candle is excluded even after market close. The calendar is maintained for 2026; other years produce an explicit unavailable state until extended and qualified. Other special sessions and independently verified calendar completeness remain outside this diagnostic.
The market collector may display BSE and other Indian venue rows for observation, but this risk input retains only NSE cash rows until a venue-specific calendar and qualification are supplied. It never applies the NSE calendar to derivatives, commodities, funds, IFSC or BSE data.

Prices are labelled `provider_close_adjustments_unverified`. No split/dividend/total-return correctness, licensing, survivorship coverage or market-source qualification is asserted. The UI keeps this warning next to the results. A recent capture does not prove that its price observations or calendar are complete or correct.

The server requires:

- A reconciled, positive, unlevered INR engine valuation no older than 30 seconds; every held NSE cash-equity/ETF mark must be fresh and consistent with quantity/value. Holdings plus cash must equal equity. Cash-only portfolios have no instrument covariance to report.
- A source capture no older than 36 hours, explicit timezone/timestamps and calendar coverage, ordered unique completed weekday or explicitly supported Budget-Sunday sessions, and a last declared session no more than seven days before capture. These age bounds are safeguards, not independent calendar certification.
- Distinct holding/history identities and provider IDs, INR/NSE scope, and positive finite numeric closes in strictly increasing order on declared sessions. Malformed, duplicate, future, unsupported or contradictory data withholds the model.
- Complete data for **every held instrument** on the most recent 253 declared sessions, yielding at most 252 adjacent-session returns. Any missing holding/date withholds aggregate numbers and lists its coverage gap. No pairwise deletion, forward filling, zero substitution, subset renormalization or skipped-date return is used.
- At least 60 complete return intervals for covariance. The historical 95% loss display additionally requires 100 intervals, equivalent to at least five observations in the empirical tail. These are engineering display floors, not statistical sufficiency or investment recommendations.

Reads are bounded to 40 holdings, 500 input instruments, 1,000 calendar sessions and 1,000 observations per selected instrument. The market snapshot reader also enforces its existing 5 MB file bound. An exceeded bound withholds the model rather than dropping economic exposure. The validated risk summary is included in workspace responses; raw `riskHistory` is excluded from the workspace's market payload and Atlas context.

## Calculation convention

For each declared adjacent-session pair, `r[i,t] = close[i,t] / close[i,t−1] − 1`. All instruments use the identical date pairs. Sample covariance uses centered returns with denominator `n−1`.

Weights are current marked instrument values divided by total equity, including cash. Cash has zero price return in this calculation. Portfolio variance is `wᵀΣw`; daily volatility is its square root. Instrument volatility contribution is `w[i] × (Σw)[i] / portfolio_volatility`, and its share of variance is `w[i] × (Σw)[i] / portfolio_variance`. Contributions may be negative when holdings offset one another. Their sums reconcile when the denominators are defined. Zero-volatility correlations and undefined contributions are null, not invented zero correlations.

The diversification ratio divides the weighted sum of standalone daily volatilities by portfolio daily volatility. It is descriptive and undefined for a zero-volatility portfolio. No normal-distribution assumption, annualization, square-root-of-time horizon extrapolation, factor model or optimization is applied.

Historical scenario P&L is the sum of current instrument values multiplied by each historical daily return. The 95% VaR loss is the nearest-rank 95th percentile of signed losses (`−P&L`). Expected shortfall integrates the worst 5% of the empirical loss distribution, including fractional weight on its boundary observation. Negative loss values are retained: they mean gains in that historical sample. The worst observed scenario is not a maximum possible loss.

No historical strategy holdings are reconstructed. The model fixes today's portfolio, does not compound or rebalance it, and excludes executions, costs, liquidity, intraday paths, protective orders, external flows, FX, leverage and derivatives. This browser module does not feed order sizing, entry controls or promotion gates. Existing portfolio freshness gates continue independently.

The engine carries its own `Decimal` port of this convention in `quant_ai.risk.portfolio_risk`, reading closed daily bars rather than the collector's snapshot. That port *does* gate entries, through the warden's correlation, group and expected-shortfall limits. The calculation convention above is the shared reference for both; a divergence between them is a defect. See [cross-position risk controls](PORTFOLIO_RISK_CONTROLS.md).

## Dashboard, export and AI

Risk lab shows sample dates/counts, cash weight, volatility, tail losses, instrument contributions and a correlation-holding selector. Contribution ordering and ten-row loss-scenario pages are interactive. Source/method details remain inspectable. Missing history replaces the metrics with per-holding coverage, not a partial portfolio estimate.

`GET /api/portfolio/historical-risk` is covered by the private session proxy. Available reports download as no-store JSON attachments. Incomplete/unavailable results return 422 with coverage and no attachment; invalid data or inaccessible storage returns 503. A report with 60–99 intervals is still downloadable but its VaR/ES fields are null with an explicit reason.

The Atlas button pre-fills an explanation request without submitting it. The server computes fresh context, preserving all holdings, correlation, limits and ten worst scenarios with an omitted-scenario count. Covariance and raw daily risk-history arrays are excluded. Company-cutoff requests still exclude current-account context. Publisher and Worker ingestion remove the private calculated risk report; the hosted snapshot viewer does not present this as a current interactive feature.

The report SHA-256 covers the evaluated portfolio and input dataset. It is a local consistency identifier, not source authentication. Reproduction requires retaining the corresponding paper ledger/valuation, original market snapshot, runtime/revision and evaluated times. A report hash alone is not the input archive.

## Verification — 14 September 2026

The shared Python-produced fixture has 120 intervals with orthogonal ±10% return patterns, a constant instrument, INR 1,000 equity and 40% cash. Independent analytic expectations are sample instrument variance `1.2/119`, portfolio variance `0.156/119`, variance shares `9/13`, `4/13`, zero for the constant holding, and INR 50 for 95% VaR/ES. Tests also cover cash dilution, perfect and partial offsets (including negative contributions), constant prices, missing dates/instruments, invalid identity/chronology/amounts and fractional signed-loss tails.

A real paper-broker/telemetry fixture with synthetic history verifies the private API and saved Atlas context. The collector test uses a mocked SDK and verifies the longer request, once-per-day caching, token identity and exclusion of the current day without making external requests.

The isolated browser/API drill uses current paper marks for TCS and an ETF, 172 historical intervals and INR 10,098.39626 equity. Scalar Python scenario standard deviation independently matches the covariance-based portfolio result: daily volatility 0.003806049158195985 and INR 45.40 historical VaR/ES. Missing, short (80-interval), invalid, stale-valuation and constant-price cases return the expected states. Invalid history does not mutate unrelated readiness checks; stale portfolio marks fail the existing valuation gate.

Desktop sorting, correlation selection, scenario pagination, private download and mobile Atlas handoff pass. Mobile document width is 390px, table region 324px and selectors 154px, with no console warnings/errors. Conversation `0279b508-086e-464c-92e8-117bb1970d41` saved the 172-interval exploratory report, ten scenarios and 162 omitted scenarios. An intentionally empty provider key produced the expected visible setup error with zero provider calls. Temporary browser/server resources were stopped.

**522 Python, 58 dashboard and 13 Worker tests** pass, plus CI-scope Ruff, changed collector/publisher Ruff, TypeScript, webpack production build and static cloud export. Existing Starlette warnings remain. Separate real-source observations, exact revision and remote CI are recorded in the closure artifacts; synthetic results are not real-account performance or production qualification.

The first CI run exposed a test-isolation issue: the mocked collector test imported the optional `kiteconnect` pilot dependency before substituting its fake. The test now supplies that fake module before loading the collector, so base CI exercises the same assertions without installing or calling a broker SDK. No test was skipped; the separate real SDK capture remains integration evidence.

## Real-source observation and calendar correction

A separate read-only Zerodha capture returned 173 closes for each of 13 instruments, through 11 September. All included 1 February 2026, which the earlier regular-session calendar omitted. The strict reader rejected that mismatch. NSE confirms the Sunday cash session in [circular NSE/CMTR/72349](https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf). The shared `MarketCalendar` now includes this specific normal-hours session; explicit holiday overrides still close it. Its special-session configuration is included in the strategy fingerprint, with a drift regression test.

Rebuilding the risk input from the retained capture (without a second provider call) gives complete 173-close/172-interval coverage for all 11 equity/ETF instruments. The reader accepted those real series on a synthetic unit-price validation book; this is data-alignment evidence, not actual-account risk, returns or qualified adjustment quality. The two index histories were not represented as tradable holdings. The running collector/deployment was not changed.

## Operations and remaining benchmark work

Deploy the updated collector and private UI together using the existing runbook. `PRAMANA_MARKET_SNAPSHOT` must point to the same collector file. An older collector yields an explicit unavailable model. Existing credentials and startup policy are unchanged; this increment does not restart or upgrade the running deployment. Preserve a selected market input archive in the reviewed recovery inventory alongside account state and report exports.

Real history coverage, adjusted-price/calendar/security-master qualification, source rights, target-host behavior and prospective tail calibration remain open. This is partial progress toward IBKR's documented historical VaR/expected-shortfall workflow and Bloomberg's integrated portfolio-risk workflow, not mathematical or feature parity with either product. IBKR supports additional horizons and methods; Bloomberg describes broader multi-asset risk and scenario models. [IBKR VaR documentation](https://www.ibkrguides.com/orgportal/performanceandstatements/valueatrisk.htm), [Bloomberg PORT](https://professional.bloomberg.com/products/bloomberg-terminal/portfolio-analytics/).

Sector/factor/beta and Greeks/margin models, full-valuation scenarios, FX/multi-asset support, robust covariance estimation, stress calibration and risk-aware portfolio construction remain unfinished. No real-money execution or 100% pilot-readiness claim follows from this feature.


## Configured closure consistency

The collector now uses the same validated `PRAMANA_HOLIDAYS_JSON` additions as the engine for its session label and risk-history session list. Added closures are retained with a hash and an explicit operator-supplied/unverified label. Invalid structures fail before source requests. This producer cannot remove bundled closures or invent additional special sessions; those configurations require qualification. It retains any provider observation on a declared closed date for the strict reader to reject. No corporate-action, source-rights or calendar-completeness gate is closed by an operator override. [Deployment and verification](DEPLOYMENT_WIRING.md).
