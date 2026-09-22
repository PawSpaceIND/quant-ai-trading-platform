"""Executable synthetic-token regressions; no provider credentials or network required."""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import traceback
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest
import yaml

from quant_ai.notifications.trading import JsonlFileSink, TradingNotificationDispatcher
from quant_ai.operations import zerodha_renewal as renewal
from quant_ai.operations.zerodha_session import SessionRecord

NOW = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
TOKEN = "synthetic-access-not-a-real-credential"
ENV = {"TRADING_LIVE_MONEY_ACTIVE": "false", "ZERODHA_API_KEY": "synthetic-api-key",
       "ZERODHA_ACCESS_TOKEN": TOKEN, renewal.USER_ID: "SYNTHETIC",
       renewal.ISSUED_AT: (NOW - timedelta(minutes=5)).isoformat()}
ROOT = Path(__file__).resolve().parents[1]


class TokenException(Exception):
    """Named as Kite names its refusal class. The module matches on the name, so this
    exercises the real refusal branch without the SDK installed."""


class FakeKite:
    def __init__(self, user="SYNTHETIC", error=None):
        self.user = user
        self.error = error
        self.tokens = []
        self.profile_calls = 0

    def set_access_token(self, token):
        self.tokens.append(token)

    def profile(self):
        self.profile_calls += 1
        if self.error:
            raise self.error
        return {"user_id": self.user}


def record(at=NOW):
    return SessionRecord("SYNTHETIC", TOKEN, at - timedelta(minutes=5))


def private_env(tmp_path, extra=""):
    path = tmp_path / ".env"
    path.write_text("# existing private settings\nZERODHA_API_KEY=synthetic-api-key\n"
                    "ZERODHA_ACCESS_TOKEN=old-synthetic-access\n"
                    "ANTHROPIC_API_KEY=unrelated-synthetic-value\n" + extra)
    path.chmod(0o600)
    return path


def test_profile_validation_is_read_only_and_token_repr_is_private():
    kite = FakeKite()
    result = renewal.validate_token(ENV, now=NOW, client_factory=lambda key: kite)
    assert result.access_token == TOKEN
    assert kite.profile_calls == 1
    assert kite.tokens == [TOKEN]
    assert TOKEN not in repr(result)
    assert TOKEN not in repr(record())


@pytest.mark.parametrize("change,reason", [
    ({"ZERODHA_API_KEY": ""}, "credentials_missing"),
    ({"ZERODHA_ACCESS_TOKEN": ""}, "credentials_missing"),
    ({renewal.ISSUED_AT: "not-a-date"}, "issuance_invalid"),
    ({renewal.ISSUED_AT: "2026-09-17T02:55:00"}, "issuance_invalid"),
    ({renewal.ISSUED_AT: (NOW + timedelta(seconds=1)).isoformat()}, "issuance_in_future"),
    ({renewal.ISSUED_AT: (NOW - timedelta(days=1)).isoformat()}, "session_expired"),
    ({"TRADING_LIVE_MONEY_ACTIVE": "true"}, "paper_only"),
    ({"TRADING_LIVE_MONEY_ACTIVE": "typo"}, "paper_only"),
])
def test_invalid_local_credentials_refuse_before_any_provider_call(change, reason):
    def forbidden(_):
        pytest.fail("Local invalid credentials must not reach Kite")
    with pytest.raises(renewal.RenewalError, match=reason):
        renewal.validate_token({**ENV, **change}, now=NOW, client_factory=forbidden)


@pytest.mark.parametrize("user,reason", [("DIFFERENT", "identity_mismatch"),
                                         ("", "identity_missing"), (None, "identity_missing")])
def test_wrong_or_missing_profile_identity_refuses(user, reason):
    with pytest.raises(renewal.RenewalError, match=reason):
        renewal.validate_token(ENV, now=NOW, client_factory=lambda _: FakeKite(user=user))


def test_legacy_env_without_timestamp_still_requires_authoritative_profile():
    legacy = {key: value for key, value in ENV.items() if key not in renewal.MANAGED[1:]}
    kite = FakeKite()
    renewal.validate_token(legacy, now=NOW, client_factory=lambda _: kite)
    assert kite.profile_calls == 1
    # An unclassified error means the question was never answered, not that it was refused.
    with pytest.raises(renewal.RenewalError, match="profile_unavailable"):
        renewal.validate_token(legacy, now=NOW,
                               client_factory=lambda _: FakeKite(error=RuntimeError(TOKEN)))
    with pytest.raises(renewal.RenewalError, match="profile_rejected"):
        renewal.validate_token(legacy, now=NOW,
                               client_factory=lambda _: FakeKite(error=TokenException(TOKEN)))


def test_provider_failure_emits_critical_durable_alert_without_secret_or_traceback(tmp_path, capsys):
    path = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(path),))
    try:
        renewal.check_runtime_token(
            env=ENV, now=NOW, dispatcher=dispatcher, sleep=lambda _: None,
            client_factory=lambda _: FakeKite(error=RuntimeError(TOKEN)),
        )
    except renewal.RenewalError as error:
        rendered = "".join(traceback.format_exception(error))
    else:
        pytest.fail("Rejected token was accepted")
    payload = json.loads(path.read_text())
    assert payload["priority"] == "CRITICAL"
    assert payload["code"] == "ZERODHA_SESSION_INVALID"
    assert payload["metadata"]["phase"] == "boot"
    assert TOKEN not in path.read_text() + rendered + str(capsys.readouterr())


def test_bad_token_never_reaches_daemon_assembly(tmp_path, monkeypatch):
    from quant_ai import daemon
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("ZERODHA_API_KEY", "synthetic-api-key")
    monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", TOKEN)
    monkeypatch.delenv(renewal.ISSUED_AT, raising=False)
    monkeypatch.setattr(renewal, "_kite_client", lambda _: FakeKite(error=TokenException(TOKEN)))
    monkeypatch.setenv("PRAMANA_ALERT_LOG", str(tmp_path / "alerts.jsonl"))
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(tmp_path / "alerts.jsonl"),))
    monkeypatch.setattr(daemon, "_env_notifications", lambda: dispatcher)
    def forbidden(_):
        pytest.fail("Invalid session reached daemon/IB assembly")
    monkeypatch.setattr(daemon, "import_module", forbidden)
    with pytest.raises(renewal.RenewalError, match="profile_rejected"):
        daemon.build_ghost_runner_from_env()
    assert dispatcher.pending()[0].priority.value == "CRITICAL"
    # The boot check dedupes through the record beside the alert log, so a restart a
    # minute later says nothing new; the record never carries the token.
    record_text = (tmp_path / renewal.SESSION_ALERT_STATE_NAME).read_text()
    assert json.loads(record_text)["reason"] == "zerodha_profile_rejected"
    assert TOKEN not in record_text
    with pytest.raises(renewal.RenewalError, match="profile_rejected"):
        daemon.build_ghost_runner_from_env()
    assert len(dispatcher.pending()) == 1


def test_atomic_env_publication_preserves_unrelated_bytes_and_old_open_reader(tmp_path):
    path = private_env(tmp_path)
    original = path.read_bytes()
    with path.open("rb") as old_reader:
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())
        assert old_reader.read() == original  # inode replacement, never in-place truncation
    new = path.read_text()
    assert "ANTHROPIC_API_KEY=unrelated-synthetic-value\n" in new
    assert "# existing private settings\n" in new
    assert renewal._fields(new)["ZERODHA_ACCESS_TOKEN"] == TOKEN
    assert renewal._fields(new)[renewal.USER_ID] == "SYNTHETIC"
    assert renewal._fields(new)[renewal.ISSUED_AT] == record().issued_at.isoformat()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".zerodha-env-*.tmp"))


def test_partial_candidate_is_detected_before_replacement(tmp_path, monkeypatch):
    path = private_env(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(renewal, "_write_candidate", lambda handle, payload: handle.write(payload[:12]))
    with pytest.raises(renewal.RenewalError, match="partial_write"):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".zerodha-env-*.tmp"))


def test_interrupted_atomic_replace_preserves_old_environment(tmp_path, monkeypatch):
    path = private_env(tmp_path)
    original = path.read_bytes()
    def interrupt(*_):
        raise OSError("synthetic interrupted rename")
    monkeypatch.setattr(renewal.os, "replace", interrupt)
    with pytest.raises(OSError):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".zerodha-env-*.tmp"))


def test_concurrent_operator_edit_is_never_overwritten(tmp_path, monkeypatch):
    path = private_env(tmp_path)
    competing = path.read_bytes() + b"OPERATOR_EDIT=preserve\n"
    write = renewal._write_candidate
    def concurrent(handle, payload):
        write(handle, payload)
        path.write_bytes(competing)
    monkeypatch.setattr(renewal, "_write_candidate", concurrent)
    with pytest.raises(renewal.RenewalError, match="changed_concurrently"):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())
    assert path.read_bytes() == competing


@pytest.mark.parametrize("field", ["ZERODHA_ACCESS_TOKEN", "ZERODHA_API_KEY"])
def test_duplicate_managed_fields_are_refused_without_writing(tmp_path, field):
    path = private_env(tmp_path, f"export {field}=duplicate\n")
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match="duplicate"):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())
    assert path.read_bytes() == original


def test_public_file_permissions_refuse(tmp_path):
    path = private_env(tmp_path)
    path.chmod(0o644)
    with pytest.raises(renewal.RenewalError, match="must_be_private"):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())


@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
def test_linked_environment_refuses(tmp_path, link_type):
    source = private_env(tmp_path)
    alias = tmp_path / "alias.env"
    if link_type == "symlink":
        alias.symlink_to(source)
    else:
        os.link(source, alias)
    with pytest.raises(renewal.RenewalError, match="regular_unlinked"):
        renewal.publish_to_env(alias, record(), now=NOW, client_factory=lambda _: FakeKite())


def test_expired_publish_does_not_modify_environment(tmp_path):
    path = private_env(tmp_path)
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match="not_current"):
        renewal.publish_to_env(path, record(NOW - timedelta(days=1)), now=NOW)
    assert path.read_bytes() == original


def test_account_switch_is_not_silently_published(tmp_path):
    path = private_env(tmp_path, f"{renewal.USER_ID}=OTHERACCOUNT\n")
    with pytest.raises(renewal.RenewalError, match="account_change"):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())


@pytest.mark.parametrize("offset,due", [(-1, False), (0, True), (44, True), (45, False)])
def test_preopen_window_uses_ist(offset, due):
    assert renewal.preopen_due(NOW + timedelta(minutes=offset), None) is due


def test_preopen_failure_alert_survives_daemon_absence(tmp_path):
    path = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(path),))
    invalid = {**ENV, renewal.ISSUED_AT: (NOW - timedelta(days=1)).isoformat()}
    check = partial(renewal.check_runtime_token, env=invalid, dispatcher=dispatcher)
    previous = renewal.watch_cycle(NOW, None, check=check)
    assert previous == NOW
    payload = json.loads(path.read_text())
    assert payload["priority"] == "CRITICAL"
    assert payload["metadata"] == {"phase": "preopen", "reason": "zerodha_session_expired"}
    assert renewal.watch_cycle(NOW + timedelta(minutes=1), previous, check=check) == previous
    assert len(path.read_text().splitlines()) == 1
    assert renewal.watch_cycle(NOW + timedelta(minutes=5), previous, check=check) != previous
    assert len(path.read_text().splitlines()) == 2


def test_naive_watch_clock_refuses():
    with pytest.raises(renewal.RenewalError, match="aware"):
        renewal.preopen_due(NOW.replace(tzinfo=None), None)


def test_compose_watch_is_independent_and_every_consumer_gets_new_token_metadata():
    services = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())["services"]
    watch = services["token-watch"]
    assert "depends_on" not in watch
    assert watch["entrypoint"][-2:] == ["quant_ai.operations.zerodha_renewal", "watch"]
    assert "zerodha-watch.json" in str(watch["healthcheck"])
    for name in ("pramana-ghost", "market-monitor", "token-watch"):
        env = services[name]["environment"]
        assert env["TRADING_LIVE_MONEY_ACTIVE"] == "false"
        for key in renewal.MANAGED:
            assert env[key] == "${" + key + ":-}"


def helper():
    spec = importlib.util.spec_from_file_location("renew_pilot_token", ROOT / "scripts/renew_pilot_token.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("branch,dirty,at,reason", [
    ("feature", "", NOW, "requires_main"),
    ("main", " M source.py", NOW, "clean_tree"),
    ("main", "", NOW + timedelta(hours=1), "requires_force"),
])
def test_owner_refresh_gates(branch, dirty, at, reason, monkeypatch):
    module = helper()
    monkeypatch.setattr(module, "_git", lambda root, command, *args: branch if command == "branch" else dirty)
    with pytest.raises(renewal.RenewalError, match=reason):
        module.check_refresh_allowed(ROOT, now=at)


def test_refresh_command_never_builds_or_changes_unrelated_services(tmp_path, monkeypatch):
    module = helper()
    calls = []
    monkeypatch.setattr(module, "check_refresh_allowed", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    module.refresh_services(tmp_path, tmp_path / ".env")
    command = calls[0][0][0]
    assert command[-3:] == ["pramana-ghost", "market-monitor", "token-watch"]
    assert "--no-build" in command and "--no-deps" in command
    assert "--build" not in command and "pull" not in command
    assert TOKEN not in " ".join(command)
    assert calls[0][1]["check"] is True


def test_interactive_login_provider_exception_never_prints_new_access_token(tmp_path, monkeypatch):
    from quant_ai.operations import zerodha_login as login
    (tmp_path / "zerodha.json").write_text(json.dumps({"api_key": "synthetic-api-key", "api_secret": "secret"}))
    kite = FakeKite(error=RuntimeError(TOKEN))
    kite.login_url = lambda: "https://example.invalid/synthetic-login"
    kite.generate_session = lambda *args, **kwargs: {"access_token": TOKEN, "user_id": "SYNTHETIC"}
    monkeypatch.setattr(login, "_kite_client_type", lambda: lambda **kwargs: kite)
    with pytest.raises(login.LoginError) as caught:
        login.login(request_token="synthetic-request", config_dir=tmp_path, out=io.StringIO(), env={})
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert not (tmp_path / "zerodha-session.json").exists()


def test_refresh_rechecks_gate_immediately_before_docker(monkeypatch):
    module = helper()
    def deny(*args, **kwargs):
        raise renewal.RenewalError("refresh_gate_denied")
    monkeypatch.setattr(module, "check_refresh_allowed", deny)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("Docker was called"))
    with pytest.raises(renewal.RenewalError, match="refresh_gate_denied"):
        module.refresh_services(ROOT, ROOT / ".env")


def test_foreign_environment_owner_refuses(tmp_path, monkeypatch):
    path = private_env(tmp_path)
    uid = path.stat().st_uid
    monkeypatch.setattr(renewal.os, "geteuid", lambda: uid + 1)
    with pytest.raises(renewal.RenewalError, match="owner_mismatch"):
        renewal.publish_to_env(path, record(), now=NOW, client_factory=lambda _: FakeKite())


def test_unsafe_token_cannot_inject_environment_lines(tmp_path):
    path = private_env(tmp_path)
    unsafe = SessionRecord("SYNTHETIC", "token\nUNSAFE=true", NOW)
    with pytest.raises(renewal.RenewalError, match="identity_invalid"):
        renewal.publish_to_env(path, unsafe, now=NOW, client_factory=lambda _: FakeKite())


@pytest.mark.parametrize("text", ["ZERODHA_API_KEY=two words", 'ZERODHA_API_KEY="unterminated',
                                 "ZERODHA_API_KEY=${OTHER_KEY}"])
def test_uninterpretable_dotenv_scalars_refuse(text):
    with pytest.raises(renewal.RenewalError, match="scalar_invalid"):
        renewal._fields(text)


def test_invalid_alert_phase_refuses_without_serializing_input():
    with pytest.raises(renewal.RenewalError, match="phase_invalid") as error:
        renewal.check_runtime_token(env=ENV, now=NOW, phase=TOKEN,
                                    client_factory=lambda _: FakeKite())
    assert TOKEN not in str(error.value)


def test_request_token_prompt_requires_a_terminal(monkeypatch):
    module = helper()
    monkeypatch.setattr(module.sys, "stdin", io.StringIO())
    monkeypatch.setattr(module, "getpass", lambda _: pytest.fail("Non-private input reached"))
    with pytest.raises(renewal.RenewalError, match="interactive_terminal_required"):
        module.secret_prompt("Request token: ")


def test_owner_helper_wires_private_login_atomic_publish_and_refresh(tmp_path, monkeypatch, capsys):
    from quant_ai.operations import zerodha_login
    module = helper()
    path = private_env(tmp_path)
    calls = []
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setattr(zerodha_login, "load_credentials", lambda _: ("synthetic-api-key", "synthetic-secret"))
    def login(**kwargs):
        assert kwargs["prompt"] is module.secret_prompt
        calls.append("login")
        return record()
    monkeypatch.setattr(zerodha_login, "login", login)
    monkeypatch.setattr(module, "check_refresh_allowed", lambda *a, **k: calls.append("gate"))
    monkeypatch.setattr(renewal, "_now", lambda: NOW)
    monkeypatch.setattr(renewal, "_kite_client", lambda _: FakeKite())
    monkeypatch.setattr(module, "refresh_services", lambda *a, **k: calls.append("refresh"))
    assert module.main(["--env-file", str(path), "--restart"]) == 0
    assert calls == ["gate", "login", "gate", "refresh"]
    assert renewal._fields(path.read_text())["ZERODHA_ACCESS_TOKEN"] == TOKEN
    assert TOKEN not in str(capsys.readouterr())


# --- boot-loop alert dedupe ----------------------------------------------------------------
# 183 identical CRITICAL alerts between 05:31 and 08:39 IST on 21 September 2026: the
# engine's boot found the token expired, alerted, exited, and Docker restarted it a minute
# later. One alert per reason per half hour is the contract now; the pre-open reminders,
# which pass no record, keep alerting every five minutes on purpose.

EXPIRED_AT = NOW + timedelta(days=1)  # the 17 September token, seen after the 18th's cutoff


def _refuse(dispatcher, state, at, **overrides):
    with pytest.raises(renewal.RenewalError):
        renewal.check_runtime_token(
            env={**ENV, **overrides}, now=at, dispatcher=dispatcher, alert_state=state,
            sleep=lambda _: None,
            client_factory=lambda _: FakeKite(error=RuntimeError(TOKEN)),
        )


def test_a_refused_boot_alerts_once_then_again_after_thirty_minutes(tmp_path, caplog):
    log = tmp_path / "alerts.jsonl"
    state = tmp_path / "zerodha-session-alert.json"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    with caplog.at_level(logging.WARNING, logger="quant_ai.zerodha_renewal"):
        for minutes in (0, 1, 2, 29):
            _refuse(dispatcher, state, EXPIRED_AT + timedelta(minutes=minutes))
    assert [json.loads(line)["metadata"]["reason"] for line in log.read_text().splitlines()] == ["zerodha_session_expired"]
    suppressed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("zerodha_session_alert_suppressed")]
    assert suppressed == ["zerodha_session_alert_suppressed phase=boot reason=zerodha_session_expired repeat_after_s=1800"] * 3
    _refuse(dispatcher, state, EXPIRED_AT + timedelta(minutes=30))
    assert len(log.read_text().splitlines()) == 2
    record = json.loads(state.read_text())
    assert record == {"reason": "zerodha_session_expired", "phase": "boot",
                      "alerted_at": (EXPIRED_AT + timedelta(minutes=30)).isoformat()}
    assert TOKEN not in state.read_text()


def test_a_changed_reason_alerts_immediately(tmp_path):
    log = tmp_path / "alerts.jsonl"
    state = tmp_path / "zerodha-session-alert.json"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    _refuse(dispatcher, state, EXPIRED_AT)
    # A fresh token the provider then rejects is a different failure, one minute later.
    fresh = {renewal.ISSUED_AT: (EXPIRED_AT - timedelta(minutes=5)).isoformat()}
    _refuse(dispatcher, state, EXPIRED_AT + timedelta(minutes=1), **fresh)
    reasons = [json.loads(line)["metadata"]["reason"] for line in log.read_text().splitlines()]
    assert reasons == ["zerodha_session_expired", "zerodha_profile_unavailable"]
    assert json.loads(state.read_text())["reason"] == "zerodha_profile_unavailable"


def test_an_unreadable_or_malformed_record_alerts_rather_than_stays_silent(tmp_path):
    log = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    directory = tmp_path / "is-a-directory"
    directory.mkdir()
    _refuse(dispatcher, directory, EXPIRED_AT)
    _refuse(dispatcher, directory, EXPIRED_AT + timedelta(minutes=1))
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"reason": "zerodha_session_expired", "alerted_at": "not-a-time"}')
    _refuse(dispatcher, malformed, EXPIRED_AT + timedelta(minutes=2))
    naive = tmp_path / "naive.json"
    naive.write_text(json.dumps({"reason": "zerodha_session_expired", "alerted_at": "2026-09-18T03:00:00"}))
    _refuse(dispatcher, naive, EXPIRED_AT + timedelta(minutes=3))
    assert len(log.read_text().splitlines()) == 4


def test_without_a_record_every_refusal_alerts_as_the_preopen_reminders_rely_on(tmp_path):
    log = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    for minutes in (0, 5, 10):
        with pytest.raises(renewal.RenewalError):
            renewal.check_runtime_token(env=ENV, now=EXPIRED_AT + timedelta(minutes=minutes),
                                        dispatcher=dispatcher, phase="preopen")
    assert len(log.read_text().splitlines()) == 3


def test_the_default_record_sits_beside_the_alert_log(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_ALERT_LOG", str(tmp_path / "alerts" / "alerts.jsonl"))
    assert renewal.default_alert_state() == (tmp_path / "alerts" / "zerodha-session-alert.json").resolve()


# --------------------------------------------------------------------------------------
# The boot race observed on the Lightsail host, 22 September 2026
#
# `refresh_services` and the deployment script both recreate every token consumer at once.
# The daemon's first act is the profile check, and in the container's first second the
# call failed in transport twice - 02:09:29 and 02:15:39 - each time exiting the process
# and dispatching the same CRITICAL alert that a stolen token would. Both times it was
# running a minute later on the identical token, which the same call verified by hand.
# The alert an operator must never learn to ignore fired twice for nothing.


class FlakyKite(FakeKite):
    """Fails the first ``failures`` profile calls, then answers."""

    def __init__(self, failures, error=None, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures
        self.flaky_error = error or RuntimeError("synthetic transport failure")

    def profile(self):
        self.profile_calls += 1
        if self.profile_calls <= self.failures:
            raise self.flaky_error
        return {"user_id": self.user}


def test_a_provider_that_never_answered_is_retried_and_the_boot_survives(tmp_path):
    """The whole point: a transient transport failure must not exit the daemon."""
    slept = []
    log = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    kite = FlakyKite(failures=2)
    verified = renewal.check_runtime_token(
        env=ENV, now=NOW, dispatcher=dispatcher, client_factory=lambda _: kite,
        sleep=slept.append,
    )
    assert verified.user_id == "SYNTHETIC"
    assert kite.profile_calls == 3
    # Bounded and short: it must not turn a real outage into a slow boot.
    assert slept == [1.0, 2.0]
    assert sum(slept) < 5
    assert not log.exists() or log.read_text() == "", "a recovered boot must not alert"


def test_a_refused_token_is_not_retried_and_fails_on_the_first_answer():
    """Repeating a rejected token cannot change the answer; it only delays the alert."""
    kite = FlakyKite(failures=99, error=TokenException(TOKEN))
    slept = []
    with pytest.raises(renewal.RenewalError, match="^zerodha_profile_rejected$"):
        renewal.check_runtime_token(env=ENV, now=NOW, client_factory=lambda _: kite,
                                    dispatcher=TradingNotificationDispatcher(), sleep=slept.append)
    assert kite.profile_calls == 1
    assert slept == []


def test_a_provider_outage_still_refuses_after_its_bounded_attempts(tmp_path):
    log = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    kite = FlakyKite(failures=99)
    with pytest.raises(renewal.RenewalError, match="^zerodha_profile_unavailable$"):
        renewal.check_runtime_token(env=ENV, now=NOW, dispatcher=dispatcher,
                                    client_factory=lambda _: kite, sleep=lambda _: None)
    assert kite.profile_calls == renewal.PROFILE_ATTEMPTS
    payload = json.loads(log.read_text())
    assert payload["priority"] == "CRITICAL"
    assert payload["metadata"]["reason"] == "zerodha_profile_unavailable"


def test_the_two_failures_tell_the_operator_to_do_different_things(tmp_path):
    """A refused session needs a new login. An unreachable provider needs nothing from it,
    and saying otherwise sends someone to re-enter credentials that already work."""
    messages = {}
    for name, error in (("rejected", TokenException(TOKEN)), ("unavailable", RuntimeError(TOKEN))):
        log = tmp_path / f"{name}.jsonl"
        dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
        with pytest.raises(renewal.RenewalError):
            renewal.check_runtime_token(env=ENV, now=NOW, dispatcher=dispatcher,
                                        client_factory=lambda _, e=error: FakeKite(error=e),
                                        sleep=lambda _: None)
        messages[name] = json.loads(log.read_text())["message"]
    assert "renew_pilot_token.py" in messages["rejected"]
    assert "renew_pilot_token.py" not in messages["unavailable"]
    assert "could not be reached" in messages["unavailable"]
    assert "may be perfectly good" in messages["unavailable"]
    assert TOKEN not in "".join(messages.values())


def test_retrying_never_retains_or_renders_the_provider_exception(tmp_path, capsys):
    """SDK errors carry request context and sometimes the credential itself. A retry loop
    is the easy place to start holding one for a later message."""
    log = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(log),))
    with pytest.raises(renewal.RenewalError) as caught:
        renewal.check_runtime_token(
            env=ENV, now=NOW, dispatcher=dispatcher, sleep=lambda _: None,
            client_factory=lambda _: FlakyKite(failures=99, error=RuntimeError(TOKEN)),
        )
    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert TOKEN not in rendered + log.read_text() + str(capsys.readouterr())


def test_the_renewal_helper_itself_still_asks_exactly_once():
    """Only the runtime boot/watch path retries. The interactive helper has an operator
    in front of it who can see the failure and try again."""
    kite = FlakyKite(failures=1)
    with pytest.raises(renewal.RenewalError, match="^zerodha_profile_unavailable$"):
        renewal.validate_token(ENV, now=NOW, client_factory=lambda _: kite)
    assert kite.profile_calls == 1
