# Research dashboard integration handoff

Owner boundary: PR #50 remains with its existing Codex owner. This branch adds reusable
research presentation/export modules on top of merged PRs #51/#52. It does not modify
PR #50, mount routes, deploy a Worker, change credentials, start an engine or submit orders.

## Added modules

- src/quant_ai/research/dashboard_export.py: projects independent model comparisons,
  continuous simulations and company-event status into one small browser-safe snapshot.
  Every database is opened read-only. Missing/invalid/oversized input produces an
  unavailable module. Raw prompts, receipts, source descriptions and filesystem paths
  are excluded. Publication is atomic with file permissions 0600.
- apps/pramana-ui/components/research-lab-panel.tsx: reusable read-only React panel
  and runtime schema validator. Export freshness is separate from original source
  observation time. Null evidence, unknown API costs and incomplete validation remain
  visible. Rendering never interprets evidence as HTML and contains no execution controls.
- apps/pramana-ui/scripts/test-research-panel.cjs: executes the actual component through
  React server rendering, including invalid schema, bounds, stale/future timestamps,
  escaped text and missing evidence.
- tests/test_research_dashboard.py: read-only/integrity/privacy and accounting projection tests.

## Private configuration

Create a private JSON file on the engine host. Each source is optional:

~~~json
{
  "comparison": {
    "database": "/private/research/comparison.sqlite",
    "experiment": "your-experiment"
  },
  "simulation": {
    "database": "/private/research/simulation.sqlite"
  },
  "companyEvents": {
    "database": "/private/research/events.sqlite"
  }
}
~~~

These are research stores, not the live paper account database. Do not include keys.
Missing source files are never initialized by the exporter.

~~~sh
PYTHONPATH=src .venv/bin/python -m quant_ai.research.dashboard_export \
  --config /private/research/dashboard-sources.json \
  --output /private/research/dashboard-snapshot.json
~~~

The output folder must already exist. The exporter prints only module statuses.
Run after research records change; generation time does not make old source data fresh.
No scheduler has been installed by this branch.

## Wiring for the PR #50 owner

1. Integrate this branch/module set after reconciling current main. Keep #50's auth,
   risk, runtime manifest, governance and deployment behavior authoritative.
2. From an authenticated server boundary, load the fixed configured snapshot path
   (never a request-supplied filesystem path), cap the file size at 512 KB and parse JSON.
   Validate with isResearchSnapshot before returning it. Use Cache-Control: no-store.
   Missing, malformed or oversized snapshots must return an explicit unavailable result.
   Do not accept browser writes or expose private research SQLite/receipt files.
3. Mount ResearchLabPanel in the existing Research screen with the validated snapshot
   and one server-generated ISO now value. The parent owns refresh, authentication and
   loading/error presentation. Passing invalid/missing data shows the unavailable state.
4. For the Cloudflare viewer, explicitly add the sanitized snapshot to the authenticated
   publisher/Worker path allowlist and freshness envelope. Do not upload raw experiment
   packets or API keys. Keep paper-only scope and all mutation restrictions.
5. Verify authenticated desktop/mobile rendering, anonymous rejection, malformed/missing
   source behavior, expired exports, original observation timestamps and unknown-cost
   labels on the actual target host. Then deploy that reviewed release normally.

This handoff does not bypass #50's deployment or readiness gates. Availability of research
records is not a successful strategy review and never grants trading permission.

## Current main integration status

The private workspace now loads `PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT` from a fixed
server-side path, caps it at 512 KB, validates the `ResearchSnapshot` schema and returns
an explicit unavailable/invalid state on failure. The authenticated Research screen
mounts `ResearchLabPanel`; the deployment fixture generates the sanitized snapshot and
the authenticated container smoke contract checks all three module IDs. The hosted
Cloudflare viewer remains intentionally unable to receive private research evidence.

## Verification commands

~~~sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_research_dashboard.py
cd apps/pramana-ui
node scripts/test-research-panel.cjs
node node_modules/typescript/bin/tsc --noEmit
~~~

## Current external blockers

- OpenAI credential remains absent; the user plans to add it. Real Astra requests remain
  unverified. The actual Claude connection test already passed in #52.
- NSE announcement RSS timed out on the Mac. Event records must not be synthesized to
  make the screen look populated. Existing stock market quotes are a separate feed.
- The Cloudflare viewer responded HTTP 200 with a non-stale publication during this
  check. Its status still identifies engineHost=Mac. Full authenticated engine hosting,
  received independent alerts and recovery qualification remain with #50/deployment.
- Real open-session and sustained strategy evidence cannot be manufactured by a merge.

## Executed integration evidence — 14 September 2026

- Six new exporter regression tests pass; focused Ruff passes.
- Actual React server-render checks pass; TypeScript passes.
- Temporary merge with PR #50 at 206aa0e837ada9c5ae6ec2679f7203125aeae55c was
  conflict-free. All 480 combined Python tests pass, as do rendering and TypeScript checks.
- The temporary merge was aborted and removed. PR #50 and running services were untouched.
- Target-host deployment and hosted browser acceptance remain with the PR #50 owner;
  local authenticated workspace wiring and browser/API smoke are now covered on main.
