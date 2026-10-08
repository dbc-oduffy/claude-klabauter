from __future__ import annotations

import importlib.machinery
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "tier_u_grant_cli", str(_BIN_DIR / "tier-u-grant-cli.py")
    )
    spec = importlib.util.spec_from_loader("tier_u_grant_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _StubGrant:

    def __init__(self, *, write_tier_u_grant=None, read_tier_u_grant=None,
                 check_tier_u_grant=None, revoke_tier_u_grant=None):
        self.write_tier_u_grant = write_tier_u_grant or (lambda *a, **k: True)
        self.read_tier_u_grant = read_tier_u_grant or (lambda *a, **k: None)
        self.check_tier_u_grant = check_tier_u_grant or (lambda *a, **k: (True, None))
        self.revoke_tier_u_grant = revoke_tier_u_grant or (lambda *a, **k: True)


@pytest.fixture()
def stub_import_module():
    from coordinator_core.session import grant_directive

    orig_import = _cli._import_module
    orig_write = grant_directive.write_tier_u_grant
    orig_revoke = grant_directive.revoke_tier_u_grant

    def _apply(stub_grant):
        _cli._import_module = lambda: stub_grant
        _cli._grant_directive_module = lambda: grant_directive
        grant_directive.write_tier_u_grant = stub_grant.write_tier_u_grant
        grant_directive.revoke_tier_u_grant = stub_grant.revoke_tier_u_grant

    yield _apply
    _cli._import_module = orig_import
    grant_directive.write_tier_u_grant = orig_write
    grant_directive.revoke_tier_u_grant = orig_revoke


def test_grant_true_exits_0(stub_import_module):
    stub_import_module(_StubGrant(write_tier_u_grant=lambda *a, **k: True))
    rc = _cli.main(["grant", "pm", "yes run it"])
    assert rc == 0


def test_grant_false_exits_1(stub_import_module):
    stub_import_module(_StubGrant(write_tier_u_grant=lambda *a, **k: False))
    rc = _cli.main(["grant", "pm", "yes run it"])
    assert rc == 1


def test_grant_forwards_ceremony_flag(stub_import_module):
    seen = {}

    def _write(granted_by, note, ceremony=None, **k):
        seen["args"] = (granted_by, note, ceremony)
        return True

    stub_import_module(_StubGrant(write_tier_u_grant=_write))
    rc = _cli.main(["grant", "ceremony", "cadence gate", "--ceremony", "workday-complete"])
    assert rc == 0
    assert seen["args"] == ("ceremony", "cadence gate", "workday-complete")


def test_grant_value_error_exits_2(stub_import_module):
    def _raise_value_error(*a, **k):
        raise ValueError("granted_by must be one of ['ceremony', 'pm']")

    stub_import_module(_StubGrant(write_tier_u_grant=_raise_value_error))
    rc = _cli.main(["grant", "robot", "note"])
    assert rc == 2


def test_grant_missing_args_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["grant", "pm"])
    assert rc == 2


def test_grant_no_args_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["grant"])
    assert rc == 2


def test_grant_dangling_ceremony_flag_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["grant", "pm", "note", "--ceremony"])
    assert rc == 2


def test_check_true_exits_0(stub_import_module):
    stub_import_module(_StubGrant(check_tier_u_grant=lambda *a, **k: (True, {"note": "x"})))
    rc = _cli.main(["check"])
    assert rc == 0


def test_check_false_exits_1(stub_import_module):
    stub_import_module(_StubGrant(check_tier_u_grant=lambda *a, **k: (False, None)))
    rc = _cli.main(["check"])
    assert rc == 1


def test_check_extra_args_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["check", "unexpected"])
    assert rc == 2


def test_revoke_true_exits_0(stub_import_module):
    stub_import_module(_StubGrant(revoke_tier_u_grant=lambda *a, **k: True))
    rc = _cli.main(["revoke"])
    assert rc == 0


def test_revoke_false_exits_1(stub_import_module):
    stub_import_module(_StubGrant(revoke_tier_u_grant=lambda *a, **k: False))
    rc = _cli.main(["revoke"])
    assert rc == 1


def test_revoke_extra_args_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["revoke", "unexpected"])
    assert rc == 2


def test_bare_revoke_passes_no_ceremony_guard(stub_import_module):
    seen = {}
    stub_import_module(
        _StubGrant(revoke_tier_u_grant=lambda *a, **k: seen.update(k) or True)
    )
    rc = _cli.main(["revoke"])
    assert rc == 0
    assert seen == {"only_ceremony": None}


def test_revoke_only_ceremony_forwards_the_name(stub_import_module):
    seen = {}
    stub_import_module(
        _StubGrant(revoke_tier_u_grant=lambda *a, **k: seen.update(k) or True)
    )
    rc = _cli.main(["revoke", "--only-ceremony", "workweek-complete"])
    assert rc == 0
    assert seen == {"only_ceremony": "workweek-complete"}


@pytest.mark.parametrize(
    "argv",
    [
        ["revoke", "--only-ceremony"],
        ["revoke", "--only-ceremony", ""],
        ["revoke", "--only-ceremony", "a", "b"],
        ["revoke", "workweek-complete"],
    ],
)
def test_malformed_only_ceremony_exits_2_without_revoking(argv, stub_import_module):
    called = []
    stub_import_module(
        _StubGrant(revoke_tier_u_grant=lambda *a, **k: called.append(k) or True)
    )
    assert _cli.main(argv) == 2
    assert called == []


def test_revoke_listed_in_usage_string(stub_import_module):
    stub_import_module(_StubGrant())
    assert "revoke" in _cli._SUBCOMMANDS


def test_read_prints_json_when_present_and_exits_0(stub_import_module):
    record = {"granted_by": "pm", "note": "yes"}
    stub_import_module(_StubGrant(read_tier_u_grant=lambda *a, **k: record))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(["read"])
    assert rc == 0
    assert '"granted_by": "pm"' in buf.getvalue()


def test_read_prints_nothing_when_absent_and_exits_0(stub_import_module):
    stub_import_module(_StubGrant(read_tier_u_grant=lambda *a, **k: None))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(["read"])
    assert rc == 0
    assert buf.getvalue() == ""


def test_read_extra_args_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["read", "unexpected"])
    assert rc == 2


def test_runtime_error_from_claude_klabauter_root_resolution_exits_3(stub_import_module):
    def _raise_runtime_error():
        raise RuntimeError("engine root unresolvable in test")

    _cli._import_module = _raise_runtime_error
    rc = _cli.main(["check"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3


def test_import_error_exits_3(stub_import_module):
    def _raise_import_error():
        raise ImportError("coordinator_core.session.grant not importable in test")

    _cli._import_module = _raise_import_error
    rc = _cli.main(["grant", "pm", "note"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3


def test_no_argv_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main([])
    assert rc == 2


def test_unknown_subcommand_exits_2(stub_import_module):
    stub_import_module(_StubGrant())
    rc = _cli.main(["not-a-real-subcommand"])
    assert rc == 2


def test_help_flag_exits_0(stub_import_module):
    def _fail_if_called():
        raise AssertionError("help flags must not reach _import_module")

    _cli._import_module = _fail_if_called

    for flag in ("--help", "-h", "help"):
        rc = _cli.main([flag])
        assert rc == 0
