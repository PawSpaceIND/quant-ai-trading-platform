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

The SQLite paper ledger, XAI proofs, alert log (`alerts.jsonl`), scheduled backups
(`backups/`) and daemon log live in the `pramana-data` Docker volume.
Verify restart behavior once before unattended operation:

```bash
docker compose -f deploy/docker-compose.yml restart pramana-ghost
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=100 pramana-ghost
```

## 7. Update a running deployment

Merging to `main` does not deploy anything. CI builds and tests the images; the host
only changes when you pull and rebuild there. Run this on the Docker host, from the
repository checkout, after the `main` CI run for the merge is green:

```bash
git fetch origin
git checkout main
git pull --ff-only origin main
git rev-parse HEAD                                   # record the deployed revision
docker compose -f deploy/docker-compose.yml --env-file .env config --quiet
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=100 pramana-ghost
```

With the Cloudflare overlay (`docs/CLOUDFLARE_PRIVATE_PILOT.md`) pass both files to every
`docker compose` command above, for example
`-f deploy/docker-compose.yml -f deploy/docker-compose.cloudflare.yml`. `up -d --build`
recreates only the services whose image or configuration changed; the `pramana-data`
volume, and with it the ledger, proofs, AI budget counters and the halt marker, is kept.
Update outside NSE and US session hours when you can: the daemon restarts in seconds,
but an open paper position is unprotected for the length of the restart.

Confirm the dashboard reports the new revision (`PRAMANA_RELEASE_REVISION`, when set)
and that the first cadence tick after the restart writes a proof. New optional settings
land in `.env.example` with each release; copy the ones you want into `.env` before
`up -d --build`, otherwise the Compose defaults apply.

## Durable alerts

Every dispatched alert - kill switch engaged, stop-loss failed to liquidate, cadence
halted, drawdown breached, and the cadence briefs - is appended as one JSON line to
`/data/alerts.jsonl` in the shared volume (`PRAMANA_ALERT_LOG`). This needs no
credentials and is always on. The container's stderr is not a record: `up -d --build`
replaces the container and takes its logs with it, while the volume survives.

```bash
docker compose -f deploy/docker-compose.yml exec pramana-ghost \
  tail -n 50 /data/alerts.jsonl
docker compose -f deploy/docker-compose.yml exec pramana-ghost \
  sh -c "grep KILL_SWITCH_ENGAGED /data/alerts.jsonl | tail -n 5"
```

The file is created mode 0600, append-only, and contains no credentials. A write
failure is logged and swallowed - an alert sink must never break the cadence tick that
is reporting the problem, so check that the file is growing rather than assuming it.

Push delivery is optional and additive: set `PRAMANA_TELEGRAM_BOT_TOKEN` and
`PRAMANA_TELEGRAM_CHAT_ID` in `.env` (Compose passes both through, empty by default)
and recreate the engine. Without them you lose the push, never the record. Neither the
file nor this repository replaces the independent host monitor required by
`docs/PRIVATE_PILOT_RUNBOOK.md`: an alert written by a stopped engine is never sent.

## Scheduled backups

The `backup` service runs on the engine image and invokes the existing
`scripts/pilot_ops.py backup` once every `PRAMANA_BACKUP_INTERVAL_SECONDS` (default
86400, daily), keeping the newest `PRAMANA_BACKUP_KEEP` copies (default 14) in
`/data/backups`. Each copy is taken with SQLite's online backup API through a
read-only connection - designed to run against a live WAL database - then
integrity-checked and recorded in a `.manifest.json` with its sha256. The engine is
never paused, locked or restarted for a backup.

```bash
docker compose -f deploy/docker-compose.yml logs --tail=20 backup
docker compose -f deploy/docker-compose.yml exec backup ls -l /data/backups
# Take one now, outside the schedule (for example before an upgrade):
docker compose -f deploy/docker-compose.yml exec backup \
  python /app/scripts/scheduled_backup.py --database /data/pramana.db \
  --directory /data/backups --keep 14 --once
# Non-destructive restore drill against a copy; it never touches the live ledger:
docker compose -f deploy/docker-compose.yml exec backup \
  python /app/scripts/pilot_ops.py restore-drill \
  --database /data/backups/<copy>.db --destination /data/drills/<copy>-drill.db
```

On the systemd install (`deploy/pramana-ghost.service`) there is no Compose service;
run the same script with `--once` from a systemd timer or cron instead.

### An on-instance backup is not a backup

A copy in `/data/backups` sits on the same volume, the same disk and the same instance
as the ledger it protects. It survives a bad upgrade or a corrupted write. It does not
survive the instance being deleted, the disk failing, the account being lost or the
host being compromised. Copy it off the host, on a schedule you actually keep:

```bash
# On the Docker host: lift the backups out of the volume onto the host filesystem.
docker compose -f deploy/docker-compose.yml cp backup:/data/backups ./pramana-backups

# From your workstation (pull, never push): copy them off the instance.
rsync -az --chmod=D700,F600 \
  operator@<vps-host>:~/quant-ai-trading-platform/pramana-backups/ \
  ~/pramana-offsite/$(date -u +%Y-%m-%d)/

# Then verify what landed, against the sha256 each manifest recorded at copy time.
cd ~/pramana-offsite/$(date -u +%Y-%m-%d)
for copy in pramana-*.db; do
  printf '%s  %s\n' "$(python3 -c "import json;print(json.load(open('$copy.manifest.json'))['sha256'])")" "$copy"
done | sha256sum -c -
```

Back up the XAI proof directory (`/data/xai`), `/data/alerts.jsonl` and the console
database alongside the ledger; `docs/PRIVATE_PILOT_RUNBOOK.md` covers restoring the
whole bundle on a separate deployment, which is the only thing that proves a backup.
Private file permissions are not encryption: store the off-host copy encrypted.

## Resource limits and log rotation

Every Compose service declares a memory cap (`mem_limit`: 1g engine, 512m dashboard,
256m collector and backup) sized for a small 2 GB instance, matching the systemd
unit's `MemoryMax`. Caps are not reservations. Every service also caps its container
logs at 10 MB x 5 files, so a chatty week cannot fill the disk and stop the engine.
Confirm both in the rendered configuration before starting:

```bash
docker compose -f deploy/docker-compose.yml --env-file .env config | grep -A3 -E "mem_limit|logging"
docker compose -f deploy/docker-compose.yml ps           # after an OOM kill a service shows Exited (137)
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
