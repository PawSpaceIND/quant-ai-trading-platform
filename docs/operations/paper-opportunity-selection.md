# Bounded paper opportunity selection (disabled by default)

This source feature selects opportunities within the unchanged founder-authorized
NSE cash catalog. It does not expand that catalog, alter subscriptions, grant data
rights, promote observation-only research instruments or increase risk/capital/AI caps.
Broad 100-name discovery outside the current catalog still requires reviewed admission.

The existing builder flag `paper_opportunity_selection=False` defaults off. The optional
`PRAMANA_PAPER_OPPORTUNITY_SELECTION` environment flag also defaults off; this patch
sets no environment/Compose value and performs no runtime activation. A future reviewed
activation requires paper mode, pilot monitoring, bound-v1 order identity and OMS,
NSE cash instruments, no IBKR stream, and the existing complete bijective subscription
mapping. The 50-name pilot/catalog cap remains enforced before opening broker state.

At an eligible session cadence the source adapter invokes the existing prior-session
research-shortlist engine once per session for eligible stock rows already in the
catalog. It uses the daemon's exchange calendar for the previous trading session and
existing history provider/sector map. The engine retains its liquidity/momentum/volume
and diversification screen and at-most-15 shortlist (fewer is valid). No extra model
or alternative-data provider is added. No paid/provider calls are performed by tests.

A fresh current-session scan only stages candidate membership. BUY admission requires
both a later cadence time and an accepted Zerodha tick whose exchange observation time
is strictly after staging, at most 60 seconds old and not future. A configured token
mapping alone cannot prove delivery. The selector reads the existing validated tick
buffer; a valid received mapped tick is evidence of per-symbol subscription delivery,
not an independent broker acknowledgment or proof the stream will remain connected.
The tick is checked again immediately before submission. All existing price drift,
protection, session, identity, reconciliation, quorum, sizing and book-risk checks remain.

Every held catalog symbol remains in the decision universe even if absent from the
shortlist or unpriced. The entire original catalog remains the independent protection,
identity, risk-history and subscription universe; it is never replaced by the shortlist.
SELL/protective paths do not read the new BUY membership gate. A holding outside the
bound catalog refuses selection and clears BUY admission; it never receives an invented
identity. Existing protection continues independently.

Missing, stale/future, wrong-session, empty, malformed or failed scans deterministically
fall back to the original fixed catalog. Fallback entries still require observed fresh
Zerodha ticks plus every existing execution gate. No fresh eligible entry means no new
BUY admission, rather than quota filling. A primary reporting cadence is retained when
no entries/holdings qualify; its BUY is rejected by the new pre-submit gate. Selection
state is transient and re-staged after restart or session rollover, so a restart cannot
inherit entry permission. The fixed-catalog fallback can contain more than 15 names;
that preserves prior behavior and is distinct from the capped dynamic shortlist.

Selection, fallback cause, per-symbol refusal reasons, entry membership and retained
holdings are emitted to the existing cadence audit and persisted beside recorded
journal decisions in additive `funnel_evidence.opportunity_selection`; no schema change
or backfill is required. Cycles producing no journal decision have only process audit
evidence, not a durable complete cycle ledger. The strategy manifest binds static
selector/catalog/subscription/research-sector configuration, excluding transient cache,
pending membership and ticks from its strategy fingerprint.

Offline tests cover paper/mode refusal before broker construction, cap/identity boundary,
observation-only/unknown/unsubscribed symbols, held retention, independent unchanged
protection catalog, stale/empty/error scans, invalid/future/stale/wrong-source prices,
later-cycle admission, session rollover, BUY-only pre-submit refusal, actual bound-paper
builder wiring without subscription/order calls, source adapter cache, journal evidence
and manifest stability. Existing daemon/protection/journal/manifest compatibility checks
remain required. This feature does not demonstrate better returns, calibrated probability,
prospective comparison or live readiness. Deployment and activation are separate actions.
