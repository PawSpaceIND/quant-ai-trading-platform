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
# Secrets: .env on this host holds live broker and model credentials. Stamping does not
# interpret or print its values, and the only compose invocation that could print one (`config`) is
# run with --quiet. There is deliberately no `set -x`: a trace of this script would put
# the contents of every command line, including compose's rendered environment, into the
# operator's scrollback and into any terminal recording.
set -euo pipefail

COMPOSE_FILE="deploy/docker-compose.yml"
ENV_FILE=".env"
# Seconds to let the stack settle before re-reading restart counters. A crash loop needs
# a window to show a second restart in; 30s covers the engine's start-up and the UI's.
SETTLE_SECONDS="${PRAMANA_DEPLOY_SETTLE_SECONDS-30}"

force=0
for argument in "$@"; do
  case "$argument" in
    --force) force=1 ;;
    -h|--help) sed -n '3,12p' "$0"; exit 0 ;;
    *) echo "deploy: unknown argument '$argument' (expected --force)" >&2; exit 2 ;;
  esac
done

fail() { echo "deploy: $*" >&2; exit 1; }

# Validate before Git or Docker can mutate anything. Canonical decimal avoids octal
# interpretation and overflow in older Bash arithmetic.
[[ "$SETTLE_SECONDS" =~ ^(0|[1-9][0-9]{0,3})$ ]] \
  && [ "$SETTLE_SECONDS" -le 3600 ] \
  || fail "settle seconds must be an integer from 0 to 3600"

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
# Python is already required by the session gate. Avoid GNU/BSD sed -i differences.
# A private same-directory temporary file preserves ownership/mode and atomically
# replaces .env only after its bytes are flushed. No credential is printed or parsed.
# This does not make the complete Git/build/deploy sequence an atomic transaction.
if ! python3 - "$ENV_FILE" "$revision" <<'STAMP'
import os
import stat
import sys
import tempfile

path, revision = sys.argv[1:]
temporary = None
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 1048576):
            raise ValueError("unsafe environment file")
        original = source.read()
        if b"\x00" in original:
            raise ValueError("invalid environment bytes")
        key = b"PRAMANA_RELEASE_REVISION="
        stamp = key + revision.encode("ascii")
        lines, seen = [], False
        for line in original.splitlines(keepends=True):
            if line.startswith(key):
                if not seen:
                    ending = b"\r\n" if line.endswith(b"\r\n") else b"\n"
                    lines.append(stamp + ending)
                    seen = True
            else:
                lines.append(line)
        updated = b"".join(lines)
        if not seen:
            if updated and not updated.endswith(b"\n"):
                updated += b"\n"
            updated += stamp + b"\n"
        out, temporary = tempfile.mkstemp(prefix=".env.release-", dir=".")
        with os.fdopen(out, "wb") as target:
            owner = os.fstat(target.fileno())
            if (owner.st_uid, owner.st_gid) != (before.st_uid, before.st_gid):
                os.fchown(target.fileno(), before.st_uid, before.st_gid)
            os.fchmod(target.fileno(), stat.S_IMODE(before.st_mode))
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_mode, info.st_uid, info.st_gid, info.st_nlink)
        if (identity(os.stat(path, follow_symlinks=False)) != identity(before)
                or identity(os.fstat(source.fileno())) != identity(before)):
            raise ValueError("environment changed during stamping")
        os.replace(temporary, path)
        temporary = None
except (OSError, ValueError):
    # Never echo .env lines or an exception containing credential-bearing input.
    raise SystemExit("deploy: could not safely stamp private .env; no containers started") from None
finally:
    if temporary is not None:
        os.unlink(temporary)
STAMP
then
  fail "release revision stamp failed"
fi

# --- Build and start -------------------------------------------------------------------
# With the Cloudflare overlay in use (docs/CLOUDFLARE_PRIVATE_PILOT.md) add
# `-f deploy/docker-compose.cloudflare.yml` to compose() below; the checks that follow
# then cover the tunnel container too.
compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# --quiet: `config` without it writes the fully rendered configuration, credentials and
# all, to stdout. This validates interpolation and required variables and prints nothing.
compose config --quiet || fail "the rendered compose configuration is invalid; nothing was deployed"
# Capture status explicitly: a process substitution can hide a failed config command.
service_list="$(compose config --services)" || fail "could not inspect compose services"
services=()
while IFS= read -r service; do
  [ -n "$service" ] || continue
  [[ "$service" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail "invalid compose service name"
  for ((index=0; index<${#services[@]}; index++)); do
    [ "${services[$index]}" != "$service" ] || fail "duplicate compose service name"
  done
  services+=("$service")
done <<<"$service_list"
[ "${#services[@]}" -gt 0 ] || fail "the compose file declares no services"

compose up -d --build

# --- Verify ---------------------------------------------------------------------------
inspect_format='{{.State.Status}} {{.State.Restarting}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'

# Aligned indexed arrays work in Bash 3.2 as well as modern Linux Bash. Only our numeric
# indices are array subscripts; Docker-supplied names never become arithmetic expressions.
containers=()
restarts_before=()
for service in "${services[@]}"; do
  container="$(compose ps --all --quiet "$service")" || fail "could not inspect container for '$service'"
  [ -n "$container" ] || fail "service '$service' has no container after 'up -d --build'"
  [[ "$container" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] \
    || fail "expected exactly one container for service '$service'"
  containers+=("$container")
done

inspect_container() {
  local inspection extra
  inspection="$(docker inspect --format "$inspect_format" "$1")" \
    || fail "container inspection failed"
  [[ "$inspection" != *$'\n'* ]] || fail "invalid multiline container inspection"
  read -r status restarting count health extra <<<"$inspection"
  [ -n "$status" ] && [ -n "$health" ] && [ -z "$extra" ] \
    || fail "incomplete or extra container inspection fields"
  [[ "$count" =~ ^(0|[1-9][0-9]{0,8})$ ]] || fail "invalid container inspection restart count"
  case "$restarting" in true|false) ;; *) fail "invalid container inspection restart flag" ;; esac
  case "$health" in none|starting|healthy|unhealthy) ;; *) fail "invalid container inspection health" ;; esac
  case "$status" in created|running|paused|restarting|removing|exited|dead) ;; *) fail "invalid container inspection state" ;; esac
}

for ((index=0; index<${#services[@]}; index++)); do
  inspect_container "${containers[$index]}"
  restarts_before+=("$count")
done

# A running state alone cannot distinguish recovery from a repeated crash. Compare the
# same container's restart counter across the settle window, once for every service.
if [ "$SETTLE_SECONDS" -gt 0 ]; then sleep "$SETTLE_SECONDS"; fi

failures=0
for ((index=0; index<${#services[@]}; index++)); do
  service="${services[$index]}"
  inspect_container "${containers[$index]}"
  before="${restarts_before[$index]}"
  echo "deploy: $service status=$status restarts=$count health=$health"
  if [ "$status" != "running" ]; then
    echo "deploy: $service is '$status', not running" >&2
    failures=$((failures + 1))
  fi
  if [ "$restarting" = "true" ]; then
    echo "deploy: $service is mid-restart" >&2
    failures=$((failures + 1))
  fi
  if [ "$count" -gt "$before" ]; then
    echo "deploy: $service restarted $before->$count during the ${SETTLE_SECONDS}s settle window; it is in a restart loop" >&2
    failures=$((failures + 1))
  elif [ "$count" -lt "$before" ]; then
    echo "deploy: $service inspection restart counter decreased $before->$count" >&2
    failures=$((failures + 1))
  fi
  # Preserve the existing startup semantics: 'starting' is not full application readiness.
  if [ "$health" = "unhealthy" ]; then
    echo "deploy: $service is unhealthy" >&2
    failures=$((failures + 1))
  fi
done

if [ "$failures" -gt 0 ]; then
  fail "$failures container check(s) failed at revision $revision; inspect with 'docker compose --env-file $ENV_FILE -f $COMPOSE_FILE logs --tail=100'"
fi

echo "deploy: all ${#services[@]} services running at revision $revision"
