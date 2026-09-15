"""Canonical filesystem locations shared by the CLI, the ghost daemon and the UI.

Every component that reads or writes paper-trading state resolves it through this
module so that ``pramana run-once``, ``python -m quant_ai.daemon`` and
``apps/pramana-ui`` agree on a single ledger file and a single proof directory
without any environment configuration.

Resolution order for the ledger:

1. ``PRAMANA_LEDGER_PATH`` - the canonical variable, shared verbatim with the UI.
2. A legacy per-component variable (``PRAMANA_PAPER_DB`` for the daemon,
   ``QUANT_AI_PAPER_DB`` for the CLI) so existing deployments keep working.
3. ``<project root>/pramana_ledger.sqlite``.

The proof directory follows the same order with ``PRAMANA_PROOF_DIR`` as the
canonical variable and ``PRAMANA_XAI_DIR`` / ``QUANT_AI_XAI_DIR`` as legacy names.
"""

from __future__ import annotations

import os
from pathlib import Path

LEDGER_ENV = "PRAMANA_LEDGER_PATH"
PROOF_ENV = "PRAMANA_PROOF_DIR"
TENANT_ENV = "PRAMANA_TENANT_ID"
HALT_FILE_ENV = "PRAMANA_HALT_FILE"
DECISION_QUALITY_ENV = "PRAMANA_DECISION_QUALITY_REPORT"
POST_MORTEM_DIR_ENV = "PRAMANA_POST_MORTEM_DIR"
HALT_OVERRIDE_LOG_ENV = "PRAMANA_HALT_OVERRIDE_LOG"
TRIAL_REGISTER_ENV = "PRAMANA_TRIAL_REGISTER"

DEFAULT_LEDGER_NAME = "pramana_ledger.sqlite"
DEFAULT_PROOF_DIRECTORY_NAME = "pramana-proofs"
DEFAULT_TENANT_ID = "default"
DEFAULT_HALT_FILE_NAME = "PRAMANA_HALT"
DEFAULT_DECISION_QUALITY_NAME = "decision-quality.json"
DEFAULT_POST_MORTEM_DIRECTORY_NAME = "post-mortems"
DEFAULT_HALT_OVERRIDE_LOG_NAME = "halt-overrides.jsonl"
DEFAULT_TRIAL_REGISTER_NAME = "trial-register.jsonl"

_ROOT_MARKERS = ("pyproject.toml", ".git")


def project_root(start: Path | None = None) -> Path:
    """Walk upwards from ``start`` (default: cwd) to the repository root.

    Falls back to the current working directory when no marker is found, which is
    the correct behaviour for an installed package running outside a checkout.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate
    return current


def _from_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def ledger_path(*legacy_env: str) -> Path:
    """Absolute path of the shared paper-trading SQLite ledger."""
    configured = _from_env(LEDGER_ENV, *legacy_env)
    if configured:
        return Path(configured).expanduser().resolve()
    return project_root() / DEFAULT_LEDGER_NAME


def proof_directory(*legacy_env: str) -> Path:
    """Absolute path of the shared XAI proof directory."""
    configured = _from_env(PROOF_ENV, *legacy_env)
    if configured:
        return Path(configured).expanduser().resolve()
    return project_root() / DEFAULT_PROOF_DIRECTORY_NAME


def tenant_id(*legacy_env: str, default: str = DEFAULT_TENANT_ID) -> str:
    """Tenant whose rows the CLI writes and the UI reads."""
    return _from_env(TENANT_ENV, *legacy_env) or default


def halt_file() -> Path:
    """Operator halt marker. Its presence engages the daemon's kill switch; removing it releases."""
    configured = _from_env(HALT_FILE_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return ledger_path().parent / DEFAULT_HALT_FILE_NAME


def decision_quality_report(*legacy_env: str) -> Path:
    """Decision-quality report the daemon rewrites every cadence; next to the ledger by default."""
    configured = _from_env(DECISION_QUALITY_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return ledger_path(*legacy_env).parent / DEFAULT_DECISION_QUALITY_NAME


def halt_override_log(*legacy_env: str) -> Path:
    """Hash-chained record of every operator override that cleared a durable fault halt."""
    configured = _from_env(HALT_OVERRIDE_LOG_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return ledger_path(*legacy_env).parent / DEFAULT_HALT_OVERRIDE_LOG_NAME


def trial_register(*legacy_env: str) -> Path:
    """Hash-chained register of research trials; next to the ledger by default."""
    configured = _from_env(TRIAL_REGISTER_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return ledger_path(*legacy_env).parent / DEFAULT_TRIAL_REGISTER_NAME


def post_mortem_directory(*legacy_env: str) -> Path:
    """Directory of per-session post-mortem files; ``post-mortems`` next to the ledger by default."""
    configured = _from_env(POST_MORTEM_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return ledger_path(*legacy_env).parent / DEFAULT_POST_MORTEM_DIRECTORY_NAME
