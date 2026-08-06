"""test_session_claim_cli.py — unit test for coordinator/bin/session-claim-cli
(AC6). Asserts the CLI's exit-code contract in isolation from any live claude-klabauter
checkout: the imported `claims` module functions are stubbed via a monkeypatch
of the CLI's own `_import_module` seam, so this suite never requires
CLAUDE_KLABAUTER_ROOT to resolve or `coordinator_core` to be importable.

Matrix asserted (per docs/plans/2026-07-21-claim-lock-trampoline-flip.md AC2):
    bool True  -> exit 0
    bool False -> exit 1
    transport failure (unresolvable CLAUDE_KLABAUTER_ROOT / ImportError) -> exit 3
    usage error (missing/unknown subcommand, wrong arity) -> exit 2

Loaded by file path (`importlib.util.spec_from_file_location`) since
`session-claim-cli` is an extensionless polyglot entrypoint, not a `.py`
module — same load idiom as sibling bin/ unit tests (e.g.
test_check_install_divergence.py's `_load_divergence_module`).

Converted from a hand-rolled unittest runner to top-level pytest functions
with a pytest fixture carrying the per-seam monkeypatch/restore.

Spec backlink: docs/plans/2026-07-21-claim-lock-trampoline-flip.md § C1 / AC6.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util

import pytest

from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    # session-claim-cli is an extensionless polyglot entrypoint (no .py
    # suffix), so spec_from_file_location can't infer a loader from the
    # filename — an explicit SourceFileLoader is required (same idiom as
    # coordinator/bin/tests/test_lesson_add.py).
    loader = importlib.machinery.SourceFileLoader(
        "session_claim_cli", str(_BIN_DIR / "session-claim-cli")
    )
    spec = importlib.util.spec_from_loader("session_claim_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _StubClaims:
    """Stand-in for coordinator_core.session.claims — each attribute is a
    callable the test configures per-case; no live claude-klabauter import required."""

    def __init__(self, *, claim_artifact=None, release_artifact=None,
                 clear_claim_if_dead=None, claim_plan=None,
                 list_claims_by_session=None):
        self.claim_artifact = claim_artifact or (lambda *a, **k: True)
        self.release_artifact = release_artifact or (lambda *a, **k: True)
        self.clear_claim_if_dead = clear_claim_if_dead or (lambda *a, **k: True)
        self.claim_plan = claim_plan or (lambda *a, **k: True)
        self.list_claims_by_session = list_claims_by_session or (lambda *a, **k: [])


class _StubLiveness:
    """Stand-in for coordinator_core.session.liveness — mirrors _StubClaims'
    per-test-configurable-callable shape, on its OWN seam
    (_cli._import_liveness_module) so these tests never touch the claims stub."""

    def __init__(self, *, session_live=None):
        self.session_live = session_live or (lambda *a, **k: True)


class _StubStaleClaims:
    """Stand-in for coordinator_core.session.stale_claims, on its OWN seam
    (_cli._import_stale_claims_module)."""

    def __init__(self, *, list_stale_claim_handoffs=None):
        self.list_stale_claim_handoffs = list_stale_claim_handoffs or (lambda *a, **k: [])


@pytest.fixture()
def stub_import_module():
    """Stub `_cli._import_module` (the claims seam) for the test body, then
    restore the original."""
    orig = _cli._import_module

    def _apply(stub_claims):
        _cli._import_module = lambda: stub_claims

    yield _apply
    _cli._import_module = orig


@pytest.fixture()
def stub_import_liveness_module():
    orig = _cli._import_liveness_module

    def _apply(stub):
        _cli._import_liveness_module = lambda: stub

    yield _apply
    _cli._import_liveness_module = orig


@pytest.fixture()
def stub_import_stale_claims_module():
    orig = _cli._import_stale_claims_module

    def _apply(stub):
        _cli._import_stale_claims_module = lambda: stub

    yield _apply
    _cli._import_stale_claims_module = orig


# ---------------------------------------------------------------------------
# bool -> exit mapping (AC2): True -> 0, False -> 1, per subcommand.
# ---------------------------------------------------------------------------

def test_claim_artifact_true_exits_0(stub_import_module):
    stub_import_module(_StubClaims(claim_artifact=lambda *a, **k: True))
    rc = _cli.main(["claim-artifact", "handoff", "some-basename"])
    assert rc == 0


def test_claim_artifact_false_exits_1(stub_import_module):
    stub_import_module(_StubClaims(claim_artifact=lambda *a, **k: False))
    rc = _cli.main(["claim-artifact", "handoff", "some-basename"])
    assert rc == 1


def test_release_artifact_true_exits_0(stub_import_module):
    stub_import_module(_StubClaims(release_artifact=lambda *a, **k: True))
    rc = _cli.main(["release-artifact", "handoff", "some-basename"])
    assert rc == 0


def test_release_artifact_false_exits_1(stub_import_module):
    stub_import_module(_StubClaims(release_artifact=lambda *a, **k: False))
    rc = _cli.main(["release-artifact", "handoff", "some-basename"])
    assert rc == 1


def test_clear_claim_if_dead_true_exits_0(stub_import_module):
    stub_import_module(_StubClaims(clear_claim_if_dead=lambda *a, **k: True))
    rc = _cli.main(["clear-claim-if-dead", "handoff", "some-basename"])
    assert rc == 0


def test_clear_claim_if_dead_false_exits_1(stub_import_module):
    stub_import_module(_StubClaims(clear_claim_if_dead=lambda *a, **k: False))
    rc = _cli.main(["clear-claim-if-dead", "handoff", "some-basename"])
    assert rc == 1


def test_claim_plan_true_exits_0(stub_import_module):
    stub_import_module(_StubClaims(claim_plan=lambda *a, **k: True))
    rc = _cli.main(["claim-plan", "some-slug"])
    assert rc == 0


def test_claim_plan_false_exits_1(stub_import_module):
    stub_import_module(_StubClaims(claim_plan=lambda *a, **k: False))
    rc = _cli.main(["claim-plan", "some-slug"])
    assert rc == 1


def test_baton_repo_root_optional_arg_forwarded(stub_import_module):
    seen = {}

    def _claim_artifact(class_, basename, baton_repo_root="", **k):
        seen["args"] = (class_, basename, baton_repo_root)
        return True

    stub_import_module(_StubClaims(claim_artifact=_claim_artifact))
    rc = _cli.main(["claim-artifact", "memo", "foo", "/some/baton/root"])
    assert rc == 0
    assert seen["args"] == ("memo", "foo", "/some/baton/root")


# ---------------------------------------------------------------------------
# Transport failure (unresolvable CLAUDE_KLABAUTER_ROOT / ImportError) -> exit 3.
# ---------------------------------------------------------------------------

def test_runtime_error_from_claude_klabauter_root_resolution_exits_3(stub_import_module):
    def _raise_runtime_error():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable in test")

    _cli._import_module = _raise_runtime_error
    rc = _cli.main(["claim-artifact", "handoff", "some-basename"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3


def test_import_error_exits_3(stub_import_module):
    def _raise_import_error():
        raise ImportError("coordinator_core.session.claims not importable in test")

    _cli._import_module = _raise_import_error
    rc = _cli.main(["release-artifact", "handoff", "some-basename"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3


def test_transport_failure_precedes_subcommand_dispatch_for_claim_plan(stub_import_module):
    def _raise_runtime_error():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable in test")

    _cli._import_module = _raise_runtime_error
    rc = _cli.main(["claim-plan", "some-slug"])
    assert rc == 3


# ---------------------------------------------------------------------------
# Usage error (missing/unknown subcommand, wrong arity) -> exit 2.
# ---------------------------------------------------------------------------

def test_no_argv_exits_2(stub_import_module):
    # Usage-error paths for a KNOWN subcommand still call _import_module()
    # first (see the CLI's main() ordering) — stub it to a harmless success
    # stub so these cases exercise arity/usage validation, not transport.
    stub_import_module(_StubClaims())
    rc = _cli.main([])
    assert rc == 2


def test_unknown_subcommand_exits_2(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["not-a-real-subcommand"])
    assert rc == 2


def test_claim_artifact_missing_basename_exits_2(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["claim-artifact", "handoff"])
    assert rc == 2


def test_claim_artifact_no_args_exits_2(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["claim-artifact"])
    assert rc == 2


def test_release_artifact_missing_basename_exits_2(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["release-artifact", "handoff"])
    assert rc == 2


def test_clear_claim_if_dead_missing_basename_exits_2(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["clear-claim-if-dead", "handoff"])
    assert rc == 2


def test_claim_plan_no_args_exits_2(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["claim-plan"])
    assert rc == 2


# ---------------------------------------------------------------------------
# claim-artifact / release-artifact / clear-claim-if-dead catch the REQUIRED-
# arg ValueError claims.py raises on an empty class/basename (a
# syntactically-complete argv — arity passed — but an empty string slipped
# through, e.g. the d5 baton-assembler directive's ``Path(artifact_path).
# stem`` on an empty artifact_path) and report it exit 1 with a clean
# stderr line, the same class of clean failure claim-plan's own boundary
# check already produces on bad input — never a raw Python traceback out of
# main(). Without _call_claim_bool's try/except this ValueError would
# propagate uncaught and pytest would report an ERROR (not a clean
# assertion failure) — that IS the red-proof for this guard.
# ---------------------------------------------------------------------------

def test_release_artifact_empty_basename_value_error_exits_1_not_traceback(
    stub_import_module, capsys
):
    def _raise_empty_basename(*a, **k):
        raise ValueError("basename required")

    stub_import_module(_StubClaims(release_artifact=_raise_empty_basename))
    rc = _cli.main(["release-artifact", "plan", ""])
    assert rc == 1
    err = capsys.readouterr().err
    assert "basename required" in err


def test_claim_artifact_empty_basename_value_error_exits_1(stub_import_module, capsys):
    def _raise_empty_basename(*a, **k):
        raise ValueError("basename required")

    stub_import_module(_StubClaims(claim_artifact=_raise_empty_basename))
    rc = _cli.main(["claim-artifact", "plan", ""])
    assert rc == 1
    err = capsys.readouterr().err
    assert "basename required" in err


def test_clear_claim_if_dead_empty_basename_value_error_exits_1(stub_import_module, capsys):
    def _raise_empty_basename(*a, **k):
        raise ValueError("basename required")

    stub_import_module(_StubClaims(clear_claim_if_dead=_raise_empty_basename))
    rc = _cli.main(["clear-claim-if-dead", "plan", ""])
    assert rc == 1
    err = capsys.readouterr().err
    assert "basename required" in err


def test_release_artifact_empty_class_value_error_exits_1(stub_import_module, capsys):
    def _raise_empty_class(*a, **k):
        raise ValueError("artifact class required")

    stub_import_module(_StubClaims(release_artifact=_raise_empty_class))
    rc = _cli.main(["release-artifact", "", "some-basename"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "artifact class required" in err


# ---------------------------------------------------------------------------
# is-session-live exit-code contract: live sid -> 0; dead sid -> _NOT_LIVE
# (1); malformed/absent sid -> _MALFORMED_SID (4), NEVER the not-live code.
# ---------------------------------------------------------------------------

def test_live_sid_exits_0(stub_import_liveness_module):
    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: True))
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == 0


def test_dead_sid_exits_not_live_code(stub_import_liveness_module):
    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: False))
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._NOT_LIVE
    assert rc == 1


def test_empty_sid_exits_malformed_code_not_not_live_code(stub_import_liveness_module):
    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for a malformed sid")

    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    rc = _cli.main(["is-session-live", ""])
    assert rc == _cli._MALFORMED_SID
    assert rc == 4
    assert rc != _cli._NOT_LIVE


def test_whitespace_only_sid_exits_malformed_code(stub_import_liveness_module):
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["is-session-live", "   "])
    assert rc == _cli._MALFORMED_SID


def test_path_traversal_sid_exits_malformed_code(stub_import_liveness_module):
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["is-session-live", "../../etc/passwd"])
    assert rc == _cli._MALFORMED_SID


def test_missing_sid_arg_exits_usage_error(stub_import_liveness_module):
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["is-session-live"])
    assert rc == 2


def test_cwd_arg_forwarded(stub_import_liveness_module):
    seen = {}

    def _session_live(sid, cwd=None):
        seen["args"] = (sid, cwd)
        return True

    stub_import_liveness_module(_StubLiveness(session_live=_session_live))
    rc = _cli.main(["is-session-live", "some-sid", "/some/repo"])
    assert rc == 0
    assert seen["args"] == ("some-sid", "/some/repo")


def test_is_session_live_transport_failure_exits_3(stub_import_liveness_module):
    def _raise_runtime_error():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable in test")

    _cli._import_liveness_module = _raise_runtime_error
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._TRANSPORT_FAIL


# ---------------------------------------------------------------------------
# list-stale-claim-handoffs: emits TAB-delimited path+sid lines, exit 0.
# ---------------------------------------------------------------------------

def test_no_stale_entries_exits_0_no_output(stub_import_stale_claims_module):
    stub_import_stale_claims_module(_StubStaleClaims(list_stale_claim_handoffs=lambda *a, **k: []))
    rc = _cli.main(["list-stale-claim-handoffs"])
    assert rc == 0


def test_stale_entries_forwarded_and_repo_root_passed(stub_import_stale_claims_module):
    seen = {}

    class _Entry:
        def __init__(self, path, claimer_sid):
            self.path = path
            self.claimer_sid = claimer_sid

    def _list(repo_root=None):
        seen["repo_root"] = repo_root
        return [_Entry("/repo/state/handoffs/x.md", "dead-sid")]

    stub_import_stale_claims_module(_StubStaleClaims(list_stale_claim_handoffs=_list))
    rc = _cli.main(["list-stale-claim-handoffs", "/repo"])
    assert rc == 0
    assert seen["repo_root"] == "/repo"


def test_list_stale_claim_handoffs_transport_failure_exits_3(stub_import_stale_claims_module):
    def _raise_import_error():
        raise ImportError("coordinator_core.session.stale_claims not importable in test")

    _cli._import_stale_claims_module = _raise_import_error
    rc = _cli.main(["list-stale-claim-handoffs"])
    assert rc == _cli._TRANSPORT_FAIL


# ---------------------------------------------------------------------------
# list-claims-by-session: emits TAB-delimited class+basename lines, exit 0.
# Routes through the claims seam (_import_module), not the stale-claims one.
# ---------------------------------------------------------------------------

def test_list_claims_by_session_no_matches_exits_0(stub_import_module, capsys):
    stub_import_module(_StubClaims(list_claims_by_session=lambda *a, **k: []))
    rc = _cli.main(["list-claims-by-session", "some-sid"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_list_claims_by_session_matches_forwarded_and_sid_passed(stub_import_module, capsys):
    seen = {}

    def _list(sid, cwd=None):
        seen["sid"] = sid
        seen["cwd"] = cwd
        return [("handoff-claims", "hb-1.md"), ("plan-claims", "some-slug")]

    stub_import_module(_StubClaims(list_claims_by_session=_list))
    rc = _cli.main(["list-claims-by-session", "some-sid", "/repo"])
    assert rc == 0
    assert seen["sid"] == "some-sid"
    assert seen["cwd"] == "/repo"
    out = capsys.readouterr().out
    assert out == "handoff-claims\thb-1.md\nplan-claims\tsome-slug\n"


def test_list_claims_by_session_missing_sid_is_usage_error(stub_import_module):
    stub_import_module(_StubClaims())
    rc = _cli.main(["list-claims-by-session"])
    assert rc == 2


def test_list_claims_by_session_transport_failure_exits_3():
    def _raise_import_error():
        raise ImportError("coordinator_core.session.claims not importable in test")

    orig = _cli._import_module
    _cli._import_module = _raise_import_error
    try:
        rc = _cli.main(["list-claims-by-session", "some-sid"])
    finally:
        _cli._import_module = orig
    assert rc == _cli._TRANSPORT_FAIL


# ---------------------------------------------------------------------------
# --help / -h / help print usage on stdout and exit 0 (bypasses the import
# seam entirely — never touches transport).
# ---------------------------------------------------------------------------

def test_help_flag_exits_0(stub_import_module):
    def _fail_if_called():
        raise AssertionError("help flags must not reach _import_module")

    _cli._import_module = _fail_if_called

    for flag in ("--help", "-h", "help"):
        rc = _cli.main([flag])
        assert rc == 0
