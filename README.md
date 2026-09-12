# Quant AI Trading Platform

Global multi-asset AI-assisted quantitative research, portfolio intelligence, paper trading, and eventually gated automated execution.

## Design goals
- India + US first, international adapter architecture from day one.
- Equities, ETFs, indices, derivatives, FX, commodities, metals/gold, fixed income, funds, and digital-asset-ready domain models.
- Probability and expected-value driven opportunity ranking.
- Risk-based position sizing, mandatory protective stops, daily-loss and drawdown circuit breakers.
- Multi-tenant architecture suitable for a future commercial SaaS product.

## Safety posture
- Paper trading only in the current phase.
- Live-money execution is disabled in code until explicit promotion gates are satisfied.
- Prediction and risk approval are separate systems.
- No AI or strategy may bypass the risk firewall.
- No forced trades and no guaranteed-return objective.

See `docs/ARCHITECTURE.md`, `docs/LIVE_MONEY_GATES.md`, `docs/PRODUCT_VISION.md`, `docs/AI_ROADMAP.md`, and `docs/GLOBAL_ASSET_PLAN.md`.
