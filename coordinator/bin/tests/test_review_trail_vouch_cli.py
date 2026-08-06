"""test_review_trail_vouch_cli.py — unit test for coordinator/bin/review-trail-vouch-cli.

Asserts the CLI's exit-code contract in isolation from any live claude-klabauter
checkout: the imported `review_trail_vouch` module functions are stubbed via
a monkeypatch of the CLI's own `_import_module` seam — same idiom as
coordinator/bin/tests/test_tier_u_grant_cli.py, its sibling grant CLI's test.

Matrix asserted:
    grant bool True  -> exit 0
    grant bool False -> exit 1
    grant ValueError  -> exit 2
    grant missing --sha -> exit 2
    grant wrong positional arity -> exit 2
    check all-vouched   -> exit 0, prints vouched subset
    check not-all-vouched (dead session / wrong SHA) -> exit 1
    check missing --sha -> exit 2
    read prints JSON when present / nothing when absent, always exit 0
    transport failure (unresolvable CLAUDE_KLABAUTER_ROOT / ImportError) -> exit 3
    usage error (missing/unknown subcommand) -> exit 2

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`review-trail-vouch-cli` is an extensionless polyglot entrypoint, not a `.py`
module.

Spec backlink: coordinator_core/session/review_trail_vouch.py
"""
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
        "review_trail_vouch_cli", str(_BIN_DIR / "review-trail-vouch-cli")
    )
    spec = importlib.util.spec_from_loader("review_trail_vouch_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _StubVouch:
    """Stand-in for coordinator_core.session.review_trail_vouch — each
    attribute is a callable the test configures per-case; no live claude-klabauter
    import required."""

    def __init__(
        self,
        *,
        write_review_trail_vouch=None,
        read_review_trail_vouch=None,
        check_review_trail_vouch=None,
    ):
        self.write_review_trail_vouch = write_review_trail_vouch or (lambda *a, **k: True)
        self.read_review_trail_vouch = read_review_trail_vouch or (lambda *a, **k: None)
        self.check_review_trail_vouch = check_review_trail_vouch or (
            lambda *a, **k: (frozenset(), None)
        )


@pytest.fixture()
def stub_import_module():
    orig = _cli._import_module

    def _apply(stub_vouch):
        _cli._import_module = lambda: stub_vouch

    yield _apply
    _cli._import_module = orig


# ---------------------------------------------------------------------------
# grant subcommand
# ---------------------------------------------------------------------------

def test_grant_true_exits_0(stub_import_module):
    stub_import_module(_StubVouch(write_review_trail_vouch=lambda *a, **k: True))
    rc = _cli.main(["grant", "pm", "vouched for c12623534, reviewed live", "--sha", "c12623534"])
    assert rc == 0


def test_grant_false_exits_1(stub_import_module):
    stub_import_module(_StubVouch(write_review_trail_vouch=lambda *a, **k: False))
    rc = _cli.main(["grant", "pm", "note", "--sha", "abc1234"])
    assert rc == 1


def test_grant_forwards_shas_and_note(stub_import_module):
    seen = {}

    def _write(shas, note, granted_by=None, **k):
        seen["args"] = (list(shas), note, granted_by)
        return True

    stub_import_module(_StubVouch(write_review_trail_vouch=_write))
    rc = _cli.main(
        ["grant", "pm", "two shas reviewed", "--sha", "aaa1111", "--sha", "bbb2222"]
    )
    assert rc == 0
    assert seen["args"] == (["aaa1111", "bbb2222"], "two shas reviewed", "pm")


def test_grant_value_error_exits_2(stub_import_module):
    def _raise_value_error(*a, **k):
        raise ValueError("shas is required (non-empty)")

    stub_import_module(_StubVouch(write_review_trail_vouch=_raise_value_error))
    rc = _cli.main(["grant", "pm", "note", "--sha", "abc1234"])
    assert rc == 2


def test_grant_missing_sha_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["grant", "pm", "note"])
    assert rc == 2


def test_grant_dangling_sha_flag_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["grant", "pm", "note", "--sha"])
    assert rc == 2


def test_grant_wrong_positional_arity_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["grant", "pm", "--sha", "abc1234"])
    assert rc == 2


def test_grant_no_args_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["grant"])
    assert rc == 2


# ---------------------------------------------------------------------------
# check subcommand
# ---------------------------------------------------------------------------

def test_check_all_vouched_exits_0_and_prints_shas(stub_import_module):
    stub_import_module(
        _StubVouch(
            check_review_trail_vouch=lambda shas, *a, **k: (frozenset(shas), {"note": "x"})
        )
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(["check", "--sha", "abc1234"])
    assert rc == 0
    assert buf.getvalue().strip() == "abc1234"


def test_check_wrong_sha_not_vouched_exits_1(stub_import_module):
    """AC3: a grant naming SHA X does not authorize a range containing
    foreign SHA Y — check for Y (not in the stub's vouched set) exits 1."""
    stub_import_module(
        _StubVouch(check_review_trail_vouch=lambda shas, *a, **k: (frozenset(), {"note": "x"}))
    )
    rc = _cli.main(["check", "--sha", "deadYYY"])
    assert rc == 1


def test_check_dead_session_grant_exits_1(stub_import_module):
    """AC4: a grant from a dead session never authorizes the current one."""
    stub_import_module(
        _StubVouch(check_review_trail_vouch=lambda shas, *a, **k: (frozenset(), None))
    )
    rc = _cli.main(["check", "--sha", "abc1234"])
    assert rc == 1


def test_check_missing_sha_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["check"])
    assert rc == 2


def test_check_unexpected_positional_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["check", "unexpected", "--sha", "abc1234"])
    assert rc == 2


# ---------------------------------------------------------------------------
# read subcommand
# ---------------------------------------------------------------------------

def test_read_prints_json_when_present_and_exits_0(stub_import_module):
    record = {"granted_by": "pm", "note": "yes", "shas": ["abc1234"]}
    stub_import_module(_StubVouch(read_review_trail_vouch=lambda *a, **k: record))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(["read"])
    assert rc == 0
    assert '"granted_by": "pm"' in buf.getvalue()


def test_read_prints_nothing_when_absent_and_exits_0(stub_import_module):
    stub_import_module(_StubVouch(read_review_trail_vouch=lambda *a, **k: None))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(["read"])
    assert rc == 0
    assert buf.getvalue() == ""


def test_read_extra_args_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["read", "unexpected"])
    assert rc == 2


# ---------------------------------------------------------------------------
# transport failure
# ---------------------------------------------------------------------------

def test_runtime_error_from_claude_klabauter_root_resolution_exits_3(stub_import_module):
    def _raise_runtime_error():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable in test")

    _cli._import_module = _raise_runtime_error
    rc = _cli.main(["check", "--sha", "abc1234"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3


def test_import_error_exits_3(stub_import_module):
    def _raise_import_error():
        raise ImportError("coordinator_core.session.review_trail_vouch not importable in test")

    _cli._import_module = _raise_import_error
    rc = _cli.main(["grant", "pm", "note", "--sha", "abc1234"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3


# ---------------------------------------------------------------------------
# usage errors
# ---------------------------------------------------------------------------

def test_no_argv_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main([])
    assert rc == 2


def test_unknown_subcommand_exits_2(stub_import_module):
    stub_import_module(_StubVouch())
    rc = _cli.main(["not-a-real-subcommand"])
    assert rc == 2


# ---------------------------------------------------------------------------
# --help / -h / help
# ---------------------------------------------------------------------------

def test_help_flag_exits_0(stub_import_module):
    def _fail_if_called():
        raise AssertionError("help flags must not reach _import_module")

    _cli._import_module = _fail_if_called

    for flag in ("--help", "-h", "help"):
        rc = _cli.main([flag])
        assert rc == 0
