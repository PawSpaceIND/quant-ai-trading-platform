#!/usr/bin/env bash
#
# Deploy the pilot host (Lightsail) from a clean `origin/main` checkout.
#
# Replaces the hand-typed sequence in docs/VPS_GHOST_DEPLOYMENT.md section 7 with one
# that cannot be half-run: it refuses before it touches the checkout, stamps the release
# revision it actually deployed, and then proves the containers came back.
#
# Usage:  ./scripts/deploy_pilot_host.sh [--force]
#
#   --force   Deploy during NSE regular hours anyway. See the session gate below.
#
# Secrets: .env on this host holds live broker and model credentials. Nothing here reads
# a value out of it, and the only compose invocation that could print one (`config`) is
# run with --quiet. There is deliberately no `set -x`: a trace of this script would put
# the contents of every command line, including compose's rendered environment, into the
# operator's scrollback and into any terminal recording.
set -euo pipefail

COMPOSE_FILE="deploy/docker-compose.yml"
ENV_FILE=".env"
# Seconds to let the stack settle before re-reading restart counters. A crash loop needs
# a window to show a second restart in; 30s covers the engine's start-up and the UI's.
SETTLE_SECONDS="${PRAMANA_DEPLOY_SETTLE_SECONDS:-30}"

force=0
for argument in "$@"; do
  case "$argument" in
    --force) force=1 ;;
    -h|--help) sed -n '3,12p' "$0"; exit 0 ;;
    *) echo "deploy: unknown argument '$argument' (expected --force)" >&2; exit 2 ;;
  esac
done

fail() { echo "deploy: $*" >&2; exit 1; }

# --- Run from the repository root, or not at all -----------------------------------
# Every path below is relative, and `docker compose --build` resolves its build context
# relative to the compose file. Run from anywhere else and the build context, the .env it
# stamps and the repository it pulls are three different places.
command -v git >/dev/null 2>&1 || fail "git is not installed"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || fail "not inside a git repository; run this from the repository root"
[ "$(pwd -P)" = "$(cd "$repo_root" && pwd -P)" ] \
  || fail "run this from the repository root ($repo_root), not $(pwd -P)"
for marker in "$COMPOSE_FILE" "pyproject.toml" "src/quant_ai/execution/session.py"; do
  [ -e "$marker" ] || fail "$marker is missing; this is not the quant-ai-trading-platform checkout"
done
[ -f "$ENV_FILE" ] || fail "$ENV_FILE is missing; the host keeps its credentials there"

# --- The checkout must describe what is deployed -----------------------------------
# PRAMANA_RELEASE_REVISION is a deployment declaration the dashboard and the strategy
# manifest compare against (docs/RUNTIME_STRATEGY_MANIFEST.md). Stamping HEAD while the
# tree carries uncommitted edits would build an image that no revision describes, and the
# review gate would then be attesting to a commit that is not what is running.
[ -z "$(git status --porcelain)" ] \
  || fail "the working tree is dirty; commit or discard before deploying so the stamped revision is true"
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || fail "on branch '$branch'; deploy from main"

# --- Session gate -------------------------------------------------------------------
# A rebuild replaces the engine container, and the market-data candle aggregator it
# carries is in memory only. Restarting mid-session throws away the intraday candles
# built since the open, so the technical agent has nothing to decide on until roughly 50
# minutes of ticks have rebuilt them - during which the engine still holds positions.
#
# The window comes from src/quant_ai/execution/session.py, which is the same calendar the
# engine judges by: it knows the NSE holidays, the Budget-Sunday special session and the
# weekend, so "Saturday" and "26 January" are not special cases here. If that file's
# definition of REGULAR_HOURS changes, this gate changes with it.
command -v python3 >/dev/null 2>&1 \
  || fail "python3 is not installed; it is required to read the trading calendar"
# PRAMANA_DEPLOY_CLOCK overrides "now" with an ISO timestamp. It exists so the tests can
# drive this gate across a real trading hour, a holiday and a weekend; it grants nothing
# --force does not already grant, and a deploy never sets it.
if ! gate="$(PYTHONPATH="$repo_root/src" python3 - "${PRAMANA_DEPLOY_CLOCK:-}" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, default_holidays, session_for

india = ZoneInfo("Asia/Kolkata")
raw = sys.argv[1] if len(sys.argv) > 1 else ""
now = datetime.fromisoformat(raw) if raw else datetime.now(india)
if now.tzinfo is None:
    now = now.replace(tzinfo=india)
session = session_for(Market.INDIA, "NSE")
state = MarketCalendar(holidays=default_holidays()).state(Market.INDIA, now)
print(state.value, session.regular_open.isoformat("minutes"), session.regular_close.isoformat("minutes"),
      now.astimezone(india).isoformat(sep=" ", timespec="minutes"))
PY
)"; then
  fail "could not read the trading calendar from src/quant_ai/execution/session.py"
fi
read -r market_state session_open session_close now_ist <<<"$gate"
if [ "$market_state" = "REGULAR_HOURS" ] && [ "$force" -eq 0 ]; then
  fail "NSE is in regular hours ($session_open-$session_close IST; now $now_ist).
       A rebuild empties the in-memory candle aggregator and leaves the technical agent
       without a decision basis for about 50 minutes while positions stay open.
       Deploy after $session_close IST, or pass --force if you have accepted that."
fi
if [ "$market_state" = "REGULAR_HOURS" ]; then
  echo "deploy: --force given during NSE regular hours; the candle aggregator will rebuild from empty"
fi
echo "deploy: NSE session state $market_state at $now_ist IST"

# --- Pull -----------------------------------------------------------------------------
previous_revision="$(git rev-parse HEAD)"
git fetch origin
git pull --ff-only origin main
revision="$(git rev-parse HEAD)"
case "$revision" in
  *[!0-9a-f]* | "") fail "git rev-parse HEAD did not return a hexadecimal object name" ;;
esac
[ "${#revision}" -eq 40 ] || fail "git rev-parse HEAD did not return a full 40-character revision"
if [ "$revision" = "$previous_revision" ]; then
  echo "deploy: already at $revision; rebuilding anyway"
else
  echo "deploy: $previous_revision -> $revision"
fi

# --- Stamp the revision into .env ------------------------------------------------------
# Rewritten in place so the file keeps its mode and ownership; .env is 0600 on the host and
# a rewrite through a temporary file is one chmod away from publishing every credential in
# it. The revision is a validated 40-character hex string, so it carries no sed delimiter.
if grep -q '^PRAMANA_RELEASE_REVISION=' "$ENV_FILE"; then
  # BSD sed requires a separate empty backup suffix; GNU sed does not.
  # Neither invocation creates a backup containing the environment's credentials.
  if [ "$(uname -s)" = "Darwin" ]; then
    sed -i '' "s|^PRAMANA_RELEASE_REVISION=.*|PRAMANA_RELEASE_REVISION=${revision}|" "$ENV_FILE"
  else
    sed -i "s|^PRAMANA_RELEASE_REVISION=.*|PRAMANA_RELEASE_REVISION=${revision}|" "$ENV_FILE"
  fi
else
  printf 'PRAMANA_RELEASE_REVISION=%s\n' "$revision" >>"$ENV_FILE"
fi
grep -q "^PRAMANA_RELEASE_REVISION=${revision}$" "$ENV_FILE" \
  || fail "failed to stamp PRAMANA_RELEASE_REVISION into $ENV_FILE"

# --- Build and start -------------------------------------------------------------------
# With the Cloudflare overlay in use (docs/CLOUDFLARE_PRIVATE_PILOT.md) add
# `-f deploy/docker-compose.cloudflare.yml` to compose() below; the checks that follow
# then cover the tunnel container too.
compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# --quiet: `config` without it writes the fully rendered configuration, credentials and
# all, to stdout. This validates interpolation and required variables and prints nothing.
compose config --quiet || fail "the rendered compose configuration is invalid; nothing was deployed"
compose up -d --build

# --- Verify ------------------------------------------------------------------------------
inspect_format='{{.State.Status}} {{.State.Restarting}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'

# Use matching numeric indexes: the system Bash on macOS has no associative arrays.
# Service names stay data, never array arithmetic; each container keeps its own baseline.
containers=()
services=()
while read -r service; do
  [ -n "$service" ] || continue
  services+=("$service")
done < <(compose config --services)
[ "${#services[@]}" -gt 0 ] || fail "the compose file declares no services"

for service in "${services[@]}"; do
  container="$(compose ps --all --quiet "$service" | head -n 1)"
  [ -n "$container" ] || fail "service '$service' has no container after 'up -d --build'"
  containers+=("$container")
done

restarts_before=()
for index in "${!services[@]}"; do
  read -r _ _ count _ <<<"$(docker inspect --format "$inspect_format" "${containers[$index]}")"
  restarts_before[$index]="$count"
done

# A container that exits and is restarted by `unless-stopped` is "running" again a second
# later, so a single look cannot tell a healthy start from a crash loop. Look twice and
# compare the restart counter: it is the only thing that distinguishes them.
if [ "$SETTLE_SECONDS" -gt 0 ]; then sleep "$SETTLE_SECONDS"; fi

failures=0
for index in "${!services[@]}"; do
  service="${services[$index]}"
  read -r status restarting count health \
    <<<"$(docker inspect --format "$inspect_format" "${containers[$index]}")"
  echo "deploy: $service status=$status restarts=$count health=$health"
  if [ "$status" != "running" ]; then
    echo "deploy: $service is '$status', not running" >&2
    failures=$((failures + 1))
  fi
  if [ "$restarting" = "true" ]; then
    echo "deploy: $service is mid-restart" >&2
    failures=$((failures + 1))
  fi
  if [ "$count" -gt "${restarts_before[$index]}" ]; then
    echo "deploy: $service restarted ${restarts_before[$index]}->$count during the ${SETTLE_SECONDS}s settle window; it is in a restart loop" >&2
    failures=$((failures + 1))
  fi
  # `starting` is not a failure: market-monitor's check has a 300s start_period because
  # its first snapshot waits on the daily instrument-master download.
  if [ "$health" = "unhealthy" ]; then
    echo "deploy: $service is unhealthy" >&2
    failures=$((failures + 1))
  fi
done

if [ "$failures" -gt 0 ]; then
  fail "$failures container check(s) failed at revision $revision; inspect with 'docker compose --env-file $ENV_FILE -f $COMPOSE_FILE logs --tail=100'"
fi

echo "deploy: all ${#services[@]} services running at revision $revision"
