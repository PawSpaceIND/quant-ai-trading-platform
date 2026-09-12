# Quant AI Trading Platform

Standalone AI-assisted quantitative research and automated trading platform for India and US markets.

## Safety posture
- Paper trading only in the initial phases.
- Live-money execution is disabled in code until explicit promotion gates are satisfied.
- Prediction and risk approval are separate systems.
- No strategy is allowed to bypass the risk firewall.

## Initial scope
- Unified market/instrument models.
- Broker abstraction.
- Deterministic paper broker.
- Immutable risk policy.
- Historical replay foundation.
- CI gates for risk and execution logic.

See `docs/ARCHITECTURE.md` and `docs/LIVE_MONEY_GATES.md`.
