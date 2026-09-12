# Closure Matrix

## Internally implemented
- Global multi-asset domain model
- Risk firewall, stops, drawdown and exposure limits
- Strategy families and market-regime detection
- Historical replay, costs, analytics, walk-forward and Monte Carlo validation
- Options/derivatives research primitives and defined-risk structures
- Model registry and explainable decision records
- Paper portfolio accounting and P&L
- Kill switch, idempotency, order lifecycle and tamper-evident audit journal
- Market session, corporate action, aggregation and data-quality pipeline
- End-to-end paper orchestrator
- SaaS plan/entitlement primitives and multi-tenant foundations

## External dependencies required for real integration
- Licensed market data provider selection and credentials
- India broker/API selection and credentials
- US broker/API selection and credentials
- News/fundamentals provider selection and credentials, if enabled
- AI model provider credentials, if enabled
- Jurisdiction/compliance approval for live products per market/asset class
- Production hosting, database and secret-management destination

## Non-negotiable live gate
Live execution stays disabled until paper/shadow evidence, broker credentials, compliance approvals and explicit live activation are all present.
