# Pramana Ghost VPS Deployment

This runbook deploys the Ghost daemon with live market data and **paper-only execution**.
The runtime refuses to start if `TRADING_LIVE_MONEY_ACTIVE=true`.

## 1. Clone and pin the approved build

```bash
git clone https://github.com/PawSpaceIND/quant-ai-trading-platform.git
cd quant-ai-trading-platform
git fetch origin
git checkout main
git pull --ff-only origin main
git rev-parse HEAD
```

Verify the SHA is the exact approved deployment SHA before continuing.

## 2. Create the VPS environment file

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Keep `TRADING_LIVE_MONEY_ACTIVE=false`. Add the real Anthropic, Zerodha API key, and current
Zerodha access token. Do not commit `.env`.

For Zerodha-only India paper observation, explicitly map the subscribed token to the same target
symbol used by the cadence, for example:

```dotenv
TRADING_LIVE_MONEY_ACTIVE=false
ANTHROPIC_API_KEY=<secret>
ZERODHA_API_KEY=<secret>
ZERODHA_ACCESS_TOKEN=<secret>
PRAMANA_ZERODHA_TOKENS_JSON=[<licensed_instrument_token>]
PRAMANA_ZERODHA_SYMBOLS_JSON={"<licensed_instrument_token>":"NIFTY"}
PRAMANA_TARGET_SYMBOL=NIFTY
PRAMANA_TARGET_MARKET=INDIA
PRAMANA_TARGET_ASSET_CLASS=INDEX
PRAMANA_TARGET_CURRENCY=INR
PRAMANA_TARGET_EXCHANGE=NSE
PRAMANA_IBKR_ENABLED=false
```

Use the actual symbol and licensed instrument token you intend to observe. If the symbol map and
`PRAMANA_TARGET_SYMBOL` do not match, the stale/missing-data guard will correctly veto consensus.

## 3. Validate Compose before starting

Docker Compose v2 is the supported command path:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env config
```

The rendered configuration must show `TRADING_LIVE_MONEY_ACTIVE: "false"`.

## 4. Build and start the daemon

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

## 5. Verify process and heartbeat

```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=200 pramana-ghost
```

For continued observation:

```bash
docker compose -f deploy/docker-compose.yml logs -f pramana-ghost
```

A market-data disconnect is expected to enter reconnect/backoff. Missing or stale data must produce
a Warden veto and no paper fill. Anthropic timeout/overload must skip consensus rather than crash.

## 6. Persistence and restart proof

The SQLite paper ledger, XAI proofs, and daemon log live in the `pramana-data` Docker volume.
Verify restart behavior once before unattended operation:

```bash
docker compose -f deploy/docker-compose.yml restart pramana-ghost
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=100 pramana-ghost
```

## IBKR opt-in

IBKR is disabled by default. Enable it only when an IB Gateway/TWS endpoint is intentionally running
and reachable from inside the container. Set `PRAMANA_IBKR_ENABLED=true` plus `PRAMANA_IB_HOST`,
`PRAMANA_IB_PORT`, `PRAMANA_IB_CLIENT_ID`, and `PRAMANA_IB_CONTRACTS_JSON`.

## Safety invariant

This deployment is not authorization for live-money execution. `PaperBrokerService` remains the
execution path and `TRADING_LIVE_MONEY_ACTIVE=false` is enforced by the image, Compose service, and
daemon startup guard.

## Founder directives, halt and holidays

Mount a `founder-directives.json` (see `deploy/founder-directives.example.json`) and set
`PRAMANA_FOUNDER_DIRECTIVES_FILE` to it: capital, risk posture, allowed markets and
asset classes, the watchlist, the position cap and instructions all come from there.
`pramana halt` / `pramana resume` manage the operator halt marker (`PRAMANA_HALT_FILE`,
default next to the ledger); load NSE lunar-calendar closures with
`PRAMANA_HOLIDAYS_JSON`. Full runbook: `docs/GATE2_BURN_IN.md`.
