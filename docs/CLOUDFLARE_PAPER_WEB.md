# Hosted Pramana paper dashboard

URL: https://pramana-paper-web.karthik-fce.workers.dev

The Cloudflare Worker serves the static Next.js dashboard and reads the latest
snapshot from its own D1 database. It has no order execution path or broker key.
All pages, assets and read APIs require the private viewer credential. The
publisher has a separate credential that only permits snapshot upload.

## Current deployment boundary

The engine, source SQLite ledger and local API still run on the Mac. The
publisher sends five read-only endpoint snapshots every 60 seconds. The hosted
banner marks synchronization stale after three minutes. Market quote timestamps
and engine heartbeat remain separate freshness indicators. The hosted site can
stay available when the Mac stops, but its data then becomes stale.

The web manifest supports adding the site to a home screen on compatible
browsers. This is a mobile web dashboard, not an App Store/Play Store native app.
Browser installation and device-specific behavior still need user verification.

## Build and deploy

From this repository:

    npm ci --prefix apps/pramana-ui
    python3 scripts/build_cloud_web.py
    node --test deploy/cloudflare/worker.test.mjs
    npx --yes wrangler@4.131.2 deploy -c deploy/cloudflare/wrangler.jsonc

The build is isolated in .cloud-build and excludes local .env files. D1 uses
deploy/cloudflare/schema.sql. VIEW_AUTH and PUBLISH_TOKEN are Worker secrets;
never commit their values.

## Credentials and publisher

Private local configuration: ~/.config/pramana/cloud-web.json (mode 0600).
Private owner login: ~/.config/pramana/cloud-web-login.txt (mode 0600).
The viewer username is pramana. Recover the password from that local file,
rather than from command history or source code.

Start the publisher with the trading repository's Python environment:

    /path/to/trading/.venv/bin/python scripts/publish_cloud_snapshot.py

For one verification upload add --once. A file lock prevents duplicates.
Source is http://localhost:3002; portfolio tenant must be india-paper.
Failure never causes synthetic fallback. Publishing continues after transient
failure; after token rotation update the private config and restart.

## Verified on 14 September 2026

- Static production export and TypeScript build pass.
- Unauthenticated page, market API and manifest return 401.
- Authenticated page, market API, portfolio API and manifest return 200.
- D1 serves 13 real quote rows and six dated headlines.
- Portfolio tenant is india-paper, equity is simulated 100000 INR.
- Read and publish credentials cannot be used interchangeably.
- No live orders were sent and no broker secrets were uploaded.

Moving the trading engine to an always-on server, completing open-session paper
validation, and adding licensed macro/fundamental sources remain separate work.

## Pilot workspace compatibility update

The shared dashboard now exports in explicit hosted read-only mode. The Worker adapts the original five snapshots to `/api/workspace` and can retain an optional richer workspace snapshot from an updated publisher. Hosted portfolio marks and runtime status are labelled snapshots, never a current protection heartbeat. Atlas, halt and watchlist editing remain in the authenticated engine workspace; the Worker has no Claude/broker key and rejects all viewer mutations. The richer payload omits operator audit notes and downsamples equity curves to at most 240 points while retaining first/last values.

Before upgrading the local API used by the publisher, set `PRAMANA_SOURCE_DASHBOARD_SECRET` in the publisher process to the source dashboard's access key. The publisher obtains an HttpOnly session through the local sign-in endpoint, then reads its allowed API snapshots. An authentication failure stops that publish attempt; authentication is never bypassed. The source origin remains `http://localhost:3002` and must match the local dashboard's configured origin.

Cloud export removes the Node-only proxy and login page because the Worker authenticates pages/assets/APIs with its existing private viewer credential. Both the Worker and the normal Node dashboard retain their own authentication paths. Static exports are never intended to be served publicly without the Worker.

This compatibility update has not been deployed to the public Worker by this task. The earlier deployed revision and its credentials were left in place.
