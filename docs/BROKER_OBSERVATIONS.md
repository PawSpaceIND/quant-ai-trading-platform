# External broker observations and paper-account isolation

The paper execution gateway now reads positions, margin and account summary from the same paper ledger that receives its fills. Real-broker observations have an explicit separate API. This prevents paper orders from being sized against a different account's holdings or cash.

## Adapter migration

| Operation | Execution-facing method | Explicit external observation |
|---|---|---|
| Positions | `get_positions(tenant_id)` returns paper positions | Kite `read_external_positions(expected_account_id)`; IBKR `read_external_positions()` |
| Funds | `get_margin(tenant_id)` and `get_account_summary(tenant_id)` return paper state | Kite `read_external_funds(expected_account_id)`; IBKR `read_external_funds()` |
| Submit/cancel | Paper ledger only | No external write methods exist |

This deliberately changes the old mixed-account read semantics. No platform runtime callers of the old external-read behavior were found; direct consumers must migrate. External records are not `BrokerPosition` or `BrokerMargin` and do not authorize an instrument for the NSE cash pilot.

Kite reads bind the expected profile before and after funds/position collection. Its `net` is available trading funds, not net liquidation. Missing funds remain unknown. Net positions preserve token, exchange and product; derivatives are not reclassified as equities. Separate depository holdings, T1 and pledged/MTF quantities are not silently combined into this position inventory. [Kite funds](https://kite.trade/docs/connect/v3/user/), [portfolio definitions](https://kite.trade/docs/connect/v3/portfolio/).

IBKR validates account membership and summary/position identity. Position collection reads bounded pages through an empty page, rejects duplicate contract/model identities and fails without returning a partial portfolio if the 100-page limit is exceeded. Fractional positions remain Decimal quantities; contract identity, currency, security type and model are preserved. `avgCost` is labelled per-contract cost including the multiplier, not an equity-share price. Pagination is cached and non-atomic: this is inventory observation, not a qualified valuation. [IBKR positions](https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio/positions), [account query and summary](https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/).

The shared HTTP transport exposes GET only, requires HTTPS, rejects redirects and responses above 8 MB, detects duplicate/nonfinite JSON fields and preserves JSON decimal precision. It does not disable certificate validation. No broker credentials enter the report or dashboard.

## Kite daily order/trade inspection

The new private Activity panel displays a selected capture with search, status filtering, pagination, order details, linked executions, export and Atlas handoff. It independently checks normalized evidence using integer arithmetic for quantity and price comparisons. [Kite's documented order/trade endpoints and fields](https://kite.trade/docs/connect/v3/orders/) define the input semantics.

Capture sequence: expected profile → daily orders → daily trades → daily orders → daily trades → expected profile. The interval must be at most 30 seconds within one Asia/Kolkata date. Broker timestamps are interpreted in Asia/Kolkata, independent of host timezone. Normalized observations are hashed; profile contact details, access tokens and the literal account ID are excluded. The hash detects accidental modification, not malicious replacement or provider authenticity. The account reference is a deterministic pseudonym, not an anonymization guarantee.

The independent Python and dashboard implementations check:

- Duplicate/malformed identities, finite prices, exact integral quantities and bounded collections.
- Trade-to-order instrument, exchange, product, side and exchange-order identity.
- Summed execution quantity versus reported filled quantity; excessive quantity components.
- COMPLETE quantities and execution-weighted average price with a fixed 0.01 tolerance in quote units. Other statuses have no assumed completion average.
- Pending quantities on terminal orders, fills on rejected orders, unsupported statuses/varieties and invalid order/fill timing.
- Agreement between the two order reads and two trade reads. A changing capture withholds consistency even when one intermediate view looks valid.

The panel labels empty evidence separately. A capture older than 120 seconds is historical; future, wrong-account or malformed files cannot produce a current result. Zero issues means only that these selected checks agree. Sequential reads cannot establish atomicity, exhaustive coverage or the truth of broker records. Only regular/AMO orders are covered; iceberg, cover and auction varieties are flagged.

## Capture and private binding

Use an independently verified Kite account ID and an existing valid read session. The command makes six GET requests; it does not log in, renew a session, poll continuously or place/cancel orders. Supply credentials through the environment, never command-line arguments or committed files.

```sh
python scripts/capture_broker_observation.py \
  --account-id EXPECTED_KITE_USER_ID \
  --tenant-id india-paper \
  --output /absolute/private/path/broker-capture-001.json
```

Environment: `KITE_API_KEY`, `KITE_ACCESS_TOKEN`. Output must be a new path; permissions are 0600. Failures print a generic error without broker exception bodies. A failed/truncated file does not qualify; select a fresh path for retry.

Configure the private dashboard with `PRAMANA_BROKER_OBSERVATION` pointing to the capture and `PRAMANA_BROKER_ACCOUNT_REF` equal to its reviewed `accountRef`. Container paths must exist in the private `/data` volume. Both fields are explicit; a mismatched reference is rejected. There is no automatic source discovery. Refresh the selected evidence deliberately after collection; this version has no lifecycle archive or continuous monitoring service. Include captures in the deployment's reviewed private file/backup inventory; no automatic capture retention is claimed.

`GET /api/broker/observation` requires the dashboard session and returns a no-store attachment. The hosted snapshot publisher and Worker omit the entire broker observation. Atlas receives observation time, status, counts and bounded issue codes; account reference, order IDs, instrument names, prices and quantities are excluded from this context. No model tools or trading actions are added.

## Verification and unclosed work

Regression and production-browser evidence is recorded in `PILOT_VERIFICATION.md`. The fixture contains 14 orders and 26 executions, including split completion, an open partial fill and a cancelled partial fill. Synthetic captures do not prove a live broker connection or lifecycle correctness.

Still open: actual selected-account observation, persistent acknowledged-order lifecycle and deduplication, cancel/replace uncertainty, postbacks and reconnect recovery, position/cash/fee/settlement reconciliation, holdings/corporate-action processing, complete order varieties, IBKR execution reconciliation and independently verified source completeness. This report must never be compared with the separate paper book as if they were the same account.

QuantConnect's requested comparison is live versus out-of-sample backtest performance and fills. Daily broker consistency does not close that capability. [QuantConnect reconciliation](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/reconciliation). Broker reads also do not establish IBKR Risk Navigator analytics or Bloomberg PORT parity. The overall pilot and wider capability goal remains incomplete.
