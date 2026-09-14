# Quant AI Trading Platform

Global multi-asset AI-assisted quantitative research, portfolio intelligence, paper trading, and eventually gated automated execution.

## Design goals
- India + US first, international adapter architecture from day one.
- Equities, ETFs, indices, derivatives, FX, commodities, metals/gold, fixed income, funds, and digital-asset-ready domain models.
- Probability and expected-value driven opportunity ranking.
- Risk-based position sizing, mandatory protective stops, daily-loss and drawdown circuit breakers.
- Multi-tenant architecture suitable for a future commercial SaaS product.

## Runtime status
- Ghost daemon (`python -m quant_ai.daemon`): live Zerodha/IBKR ticks → real candles and marks → five-agent swarm + Atlas consensus → risk firewall → local paper ledger. Founder directives (`deploy/founder-directives.example.json`) set capital, risk posture, allowed markets/asset classes, the India+US watchlist, the position cap and instructions.
- Launch, operator halt, holidays and the observation checklist: `docs/GATE2_BURN_IN.md`.
- Sandbox CLI (`pramana run-once`): deterministic US-only smoke path, synthetic prices.

## Safety posture
- Paper trading only in the current phase.
- Live-money execution is disabled in code until explicit promotion gates are satisfied.
- Prediction and risk approval are separate systems.
- No AI or strategy may bypass the risk firewall.
- No forced trades and no guaranteed-return objective.

See `docs/ARCHITECTURE.md`, `docs/LIVE_MONEY_GATES.md`, `docs/PRODUCT_VISION.md`, `docs/AI_ROADMAP.md`, and `docs/GLOBAL_ASSET_PLAN.md`.
