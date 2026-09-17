"""Synthetic dotenv-boundary regressions; no Docker, login, or provider writes."""
from datetime import datetime, timezone

import pytest

from quant_ai.operations import zerodha_renewal as renewal
from quant_ai.operations.zerodha_session import SessionRecord

NOW = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)


class Profile:
    def set_access_token(self, token):
        assert token == "dummy"

    def profile(self):
        return {"user_id": "FIXTURE"}


def private_env(tmp_path, suffix=""):
    path = tmp_path / ".env"
    path.write_text("ZERODHA_API_KEY=fixture\n" + suffix)
    path.chmod(0o600)
    return path


def publish(path, factory=lambda _: Profile()):
    renewal.publish_to_env(path, SessionRecord("FIXTURE", "dummy", NOW),
                           now=NOW, client_factory=factory)


def forbidden(_):
    pytest.fail("Invalid environment reached the provider boundary")


@pytest.mark.parametrize("delimiter", ["=", ":", " : ", " = "])
def test_documented_delimiters_have_identical_field_authority(delimiter):
    assert renewal._fields(f"ZERODHA_API_KEY{delimiter}fixture")["ZERODHA_API_KEY"] == "fixture"


@pytest.mark.parametrize("field", [*renewal.MANAGED, "ZERODHA_API_KEY", "TRADING_LIVE_MONEY_ACTIVE"])
def test_mixed_delimiter_duplicate_is_not_hidden(field):
    with pytest.raises(renewal.RenewalError, match="duplicate"):
        renewal._fields(f"{field}=first\n{field}: second\n")


@pytest.mark.parametrize("field,value,reason", [
    (renewal.USER_ID, "OTHER", "account_change"),
    ("TRADING_LIVE_MONEY_ACTIVE", "true", "paper_only"),
])
def test_colon_account_and_mode_guards_refuse_before_provider(tmp_path, field, value, reason):
    path = private_env(tmp_path, f"{field}: {value}\n")
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match=reason):
        publish(path, forbidden)
    assert path.read_bytes() == original


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("field", [*renewal.MANAGED, "ZERODHA_API_KEY"])
def test_multiline_value_cannot_impersonate_top_level_assignment(tmp_path, quote, field):
    suffix = f"UNRELATED={quote}notes\n{field}=dummy\n{quote}\n"
    path = private_env(tmp_path, suffix)
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match="multiline"):
        publish(path, forbidden)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".zerodha-env-*.tmp"))


@pytest.mark.parametrize("value", ["fix\\ture", "'fix'\"ture\"", "\"fix\\nture\"", "${OTHER}"])
def test_shell_only_or_interpolated_credentials_are_not_reinterpreted(value):
    with pytest.raises(renewal.RenewalError, match="scalar_invalid"):
        renewal._fields(f"ZERODHA_API_KEY={value}")


def test_unquoted_hash_without_space_is_literal_not_a_shell_comment():
    assert renewal._fields("ZERODHA_API_KEY=fixture#suffix")["ZERODHA_API_KEY"] == "fixture#suffix"


@pytest.mark.parametrize("value", ["fixture # comment", "'fixture' # comment", '"fixture" # comment'])
def test_documented_inline_comments_keep_the_scalar(value):
    assert renewal._fields(f"ZERODHA_API_KEY={value}")["ZERODHA_API_KEY"] == "fixture"


@pytest.mark.parametrize("separator", ["\x00", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"])
def test_non_line_ending_controls_cannot_create_assignments(tmp_path, separator):
    path = private_env(tmp_path, f"UNRELATED=note{separator}ZERODHA_ACCESS_TOKEN=old\n")
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match="unsupported"):
        publish(path, forbidden)
    assert path.read_bytes() == original


def test_colon_publication_preserves_unrelated_bytes(tmp_path):
    path = tmp_path / ".env"
    original = (b"# fixture\r\nZERODHA_API_KEY: 'fixture'\r\n"
                b"ZERODHA_ACCESS_TOKEN: old\r\nUNRELATED='{\"text\": \"value\"}'\r\n")
    path.write_bytes(original)
    path.chmod(0o600)
    with path.open("rb") as old_reader:
        publish(path)
        assert old_reader.read() == original
    result = path.read_bytes()
    assert b"UNRELATED='{\"text\": \"value\"}'\r\n" in result
    assert b"ZERODHA_ACCESS_TOKEN: old" not in result
    assert result.count(b"ZERODHA_ACCESS_TOKEN") == 1
    assert renewal._fields(result.decode())["ZERODHA_ACCESS_TOKEN"] == "dummy"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("quote", ["'", '"'])
def test_unterminated_foreign_value_refuses_before_any_provider_call(tmp_path, quote):
    path = private_env(tmp_path, f"UNRELATED={quote}notes\nZERODHA_ACCESS_TOKEN=dummy\n")
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match="multiline"):
        publish(path, forbidden)
    assert path.read_bytes() == original


@pytest.mark.parametrize("line", ["not-an-assignment", "export ZERODHA_API_KEY", "=broken"])
def test_unsupported_assignment_syntax_is_not_silently_ignored(tmp_path, line):
    path = private_env(tmp_path, line + "\n")
    original = path.read_bytes()
    with pytest.raises(renewal.RenewalError, match="syntax_unsupported"):
        publish(path, forbidden)
    assert path.read_bytes() == original


@pytest.mark.parametrize("value", ['"fixture" extra', "'fixture'junk"])
def test_quoted_scalar_cannot_hide_trailing_value(value):
    with pytest.raises(renewal.RenewalError, match="scalar_invalid"):
        renewal._fields(f"ZERODHA_API_KEY={value}")


@pytest.mark.parametrize("literal", [r"'don\'t change'", r'"{\"value\":\"fixture\"}"'])
def test_unrelated_escaped_quotes_remain_byte_identical(tmp_path, literal):
    suffix = "UNRELATED=" + literal + "\n"
    path = private_env(tmp_path, suffix)
    publish(path)
    assert suffix in path.read_text()


def test_owner_helper_refuses_ambiguous_file_before_interactive_login(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    from quant_ai.operations import zerodha_login

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("renewal_owner_env_test", root / "scripts/renew_pilot_token.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    path = private_env(tmp_path, "UNRELATED='notes\nZERODHA_ACCESS_TOKEN=dummy\n")
    original = path.read_bytes()
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setattr(zerodha_login, "load_credentials", forbidden)
    assert helper.main(["--env-file", str(path)]) == 1
    assert path.read_bytes() == original
    assert "dummy" not in str(capsys.readouterr())
