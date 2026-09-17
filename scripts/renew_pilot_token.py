#!/usr/bin/env python3
"""Owner-invoked interactive login, atomic .env publication, optional service refresh.

This script never pulls, builds, merges, places an order, or displays credentials.
Run it on the already-deployed main checkout after the daily 06:00 IST cutoff.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def secret_prompt(message: str) -> str:
    from quant_ai.operations.zerodha_renewal import RenewalError
    if not sys.stdin.isatty():
        raise RenewalError("zerodha_interactive_terminal_required")
    return getpass(message)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True,
        capture_output=True,
    ).stdout.strip()


def check_refresh_allowed(root: Path, *, now: datetime, force: bool = False) -> None:
    from quant_ai.domain.models import Market
    from quant_ai.execution.session import MarketCalendar, default_holidays
    from quant_ai.operations.zerodha_renewal import RenewalError

    if _git(root, "branch", "--show-current") != "main":
        raise RenewalError("zerodha_refresh_requires_main")
    if _git(root, "status", "--porcelain"):
        raise RenewalError("zerodha_refresh_requires_clean_tree")
    if now.tzinfo is None or now.utcoffset() is None:
        raise RenewalError("zerodha_refresh_clock_must_be_aware")
    state = MarketCalendar(holidays=default_holidays()).state(Market.INDIA, now)
    if state.value == "REGULAR_HOURS" and not force:
        raise RenewalError("zerodha_refresh_during_session_requires_force")


def refresh_services(root: Path, env_file: Path, *, force: bool = False) -> None:
    check_refresh_allowed(root, now=datetime.now(timezone.utc), force=force)
    # Exact allowlist: no build, pull, dashboard replacement, or unrelated service change.
    subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f",
         str(root / "deploy/docker-compose.yml"), "up", "-d", "--no-build",
         "--no-deps", "--force-recreate", "pramana-ghost", "market-monitor", "token-watch"],
        cwd=root, check=True, capture_output=True,
    )


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from quant_ai.operations.zerodha_login import LoginError, load_credentials, login
    from quant_ai.operations.zerodha_renewal import (
        RenewalError,
        _fields,
        _paper_only,
        _regular_private,
        publish_to_env,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/pramana")
    parser.add_argument("--restart", action="store_true",
                        help="Recreate only the three token consumers after successful publication")
    parser.add_argument("--force", action="store_true",
                        help="Explicitly accept loss of in-memory candles during the NSE session")
    args = parser.parse_args(argv)
    try:
        _paper_only(os.environ)
        _regular_private(args.env_file)
        existing = _fields(args.env_file.read_text())
        _paper_only(existing)
        key, _ = load_credentials(args.config_dir)
        if existing.get("ZERODHA_API_KEY") != key:
            raise RenewalError("zerodha_login_app_does_not_match_host")
        # Check before login: a new login may invalidate the currently running session.
        check_refresh_allowed(ROOT, now=datetime.now(timezone.utc), force=args.force)
        record = login(config_dir=args.config_dir, prompt=secret_prompt)
        check_refresh_allowed(ROOT, now=datetime.now(timezone.utc), force=args.force)
        publish_to_env(args.env_file, record)
        if args.restart:
            refresh_services(ROOT, args.env_file, force=args.force)
            print("Verified token published; token consumers recreated. Check health and alerts.")
        else:
            print("Verified token published. Running containers still need owner-authorized refresh.")
        return 0
    except (LoginError, RenewalError, OSError, ValueError, subprocess.SubprocessError):
        print("renew-pilot-token: refused or incomplete; no credential details displayed. "
              "Check private inputs, main/clean/session requirements, and service health.",
              file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("renew-pilot-token: cancelled; verify service health before the open.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
