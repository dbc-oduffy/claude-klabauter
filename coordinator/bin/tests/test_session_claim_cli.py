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


class _StubClaimIndex:
    """Stand-in for coordinator_core.session.claim_index, on its OWN seam
    (_cli._import_claim_index_module) -- who-claims-path coverage."""

    UNANSWERABLE = "__UNANSWERABLE__"

    def __init__(self, *, lookup=None):
        self.lookup = lookup or (lambda paths, cwd=None: {p: [] for p in paths})


class _StubHolderEvidence:
    """Stand-in for coordinator_core.pickup_assemble.holder_evidence, on its
    OWN seam (_cli._import_holder_evidence_module) — AC7/AC8 coverage."""

    def __init__(self, *, liveness_basis=None):
        self.liveness_basis = liveness_basis or (lambda *a, **k: "stable-pid")


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


@pytest.fixture()
def stub_import_claim_index_module():
    orig = _cli._import_claim_index_module

    def _apply(stub):
        _cli._import_claim_index_module = lambda: stub

    yield _apply
    _cli._import_claim_index_module = orig


@pytest.fixture()
def stub_import_holder_evidence_module():
    orig = _cli._import_holder_evidence_module

    def _apply(stub):
        _cli._import_holder_evidence_module = lambda: stub

    yield _apply
    _cli._import_holder_evidence_module = orig


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


def test_colon_drive_letter_sid_exits_malformed_code(stub_import_liveness_module):
    # Review: coordinator:code-reviewer — a blocklist of `/`, `\`, `..`, NUL
    # did not reject a bare drive-letter/colon component, and on Windows
    # `ntpath.join(base, "C:evil")` DISCARDS `base` entirely, a full
    # containment escape out of the sessions corpus. liveness must never be
    # consulted for such a sid.
    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for a malformed sid")

    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    for bad_sid in ("C:evil", "C:\\evil", "C:/Windows/Temp/x"):  # abs-path-ok: attack-shaped sid literals, not a machine path citation
        rc = _cli.main(["is-session-live", bad_sid])
        assert rc == _cli._MALFORMED_SID
        assert rc != _cli._NOT_LIVE


def test_missing_sid_arg_exits_usage_error(stub_import_liveness_module):
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["is-session-live"])
    assert rc == 2


def test_unexpected_exception_from_ungarded_callsite_exits_transport_fail_not_1(
    monkeypatch,
):
    # Review: coordinator:code-reviewer — guard-per-callsite structural
    # fragility. claim-artifact/release-artifact/clear-claim-if-dead route
    # through `_call_claim_bool`, which only catches `ValueError` (the
    # required-arg guard) — NOT a general engine failure. Before the
    # top-level `main` safety net, an unexpected exception here (e.g. an
    # `OSError` from the underlying claims.py call) propagated uncaught out
    # of `main`, and an uncaught Python exception exits the interpreter with
    # code 1 — indistinguishable from `_NOT_LIVE`'s "confirmed dead"
    # verdict, exactly the claim-theft shape this file exists to close. The
    # top-level backstop in `main` must catch it and exit `_TRANSPORT_FAIL`
    # instead, regardless of which callsite forgot its own guard.
    class _FakeClaimsModule:
        @staticmethod
        def claim_artifact(*a, **k):
            raise OSError("simulated unexpected engine failure")

    monkeypatch.setattr(_cli, "_import_module", lambda: _FakeClaimsModule())
    rc = _cli.main(["claim-artifact", "handoff", "some-basename"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc != _cli._NOT_LIVE
    assert rc != 1


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


def test_session_live_raise_exits_transport_fail_not_dead(
    stub_import_liveness_module, capsys
):
    """Review: staff-eng-review A. session_live itself raising (e.g.
    MissingPsutilError propagating past an unguarded Layer-1 arm) must NOT
    exit _NOT_LIVE (1) -- this CLI's own header documents exit 1 as a
    determinate "confirmed dead" verdict, which a bash arbitration caller
    reads as permission to take a peer's claim. Reuse _TRANSPORT_FAIL (3),
    never a new code."""

    def _raise(*a, **k):
        raise RuntimeError("simulated unexpected session_live failure")

    stub_import_liveness_module(_StubLiveness(session_live=_raise))
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3
    assert rc != _cli._NOT_LIVE
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["indeterminate"]


def test_who_claims_path_session_live_raise_exits_transport_fail(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):
    """Same fail-open guard as is-session-live, for who-claims-path's own
    liveness_mod.session_live call site."""

    def _raise(*a, **k):
        raise RuntimeError("simulated unexpected session_live failure")

    stub_import_claim_index_module(
        _StubClaimIndex(lookup=lambda paths, cwd=None: {p: ["some-sid"] for p in paths})
    )
    stub_import_liveness_module(_StubLiveness(session_live=_raise))
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["indeterminate"]


# ---------------------------------------------------------------------------
# AC7/AC8/AC9: liveness_basis surfaced alongside the verdict, reusing
# holder_evidence.liveness_basis (never a second derivation), with the
# pre-existing live/dead token's position, spelling, and exit codes
# unchanged from the baseline recorded above (line 1 == "live"/"dead"/
# "indeterminate"; exit 0/1/2/3/4 unchanged).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "basis_value",
    ["harness-registry", "stable-pid", "recency-window", "recency-window-mtime", "unknown"],
)
def test_live_sid_reports_liveness_basis_line(
    stub_import_liveness_module, stub_import_holder_evidence_module, capsys, basis_value
):
    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: True))
    stub_import_holder_evidence_module(
        _StubHolderEvidence(liveness_basis=lambda *a, **k: basis_value)
    )
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    # Baseline token (AC9): line 1 is exactly "live", unchanged position/spelling.
    assert out_lines[0] == "live"
    assert out_lines[1] == f"liveness_basis:{basis_value}"


def test_dead_sid_reports_liveness_basis_line(
    stub_import_liveness_module, stub_import_holder_evidence_module, capsys
):
    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: False))
    stub_import_holder_evidence_module(
        _StubHolderEvidence(liveness_basis=lambda *a, **k: "recency-window")
    )
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._NOT_LIVE
    assert rc == 1
    out_lines = capsys.readouterr().out.splitlines()
    # Baseline token (AC9): line 1 is exactly "dead", unchanged position/spelling.
    assert out_lines[0] == "dead"
    assert out_lines[1] == "liveness_basis:recency-window"


def test_liveness_basis_call_reuses_holder_evidence_not_a_second_derivation(
    stub_import_liveness_module, stub_import_holder_evidence_module
):
    """AC8: the CLI must call holder_evidence.liveness_basis(sid, cwd) — the
    existing derivation — never compute a basis itself."""
    seen = {}

    def _liveness_basis(sid, cwd=None):
        seen["args"] = (sid, cwd)
        return "harness-registry"

    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: True))
    stub_import_holder_evidence_module(_StubHolderEvidence(liveness_basis=_liveness_basis))
    rc = _cli.main(["is-session-live", "some-sid", "/some/repo"])
    assert rc == 0
    assert seen["args"] == ("some-sid", "/some/repo")


def test_liveness_basis_failure_degrades_to_unknown_without_changing_verdict(
    stub_import_liveness_module, stub_import_holder_evidence_module, capsys
):
    """Fail-soft additive output: a basis-derivation failure must not change
    the live/dead token or exit code (AC9), and must not raise out of the
    subcommand."""

    def _raise(*a, **k):
        raise RuntimeError("holder_evidence import failed in test")

    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: True))
    _cli._import_holder_evidence_module = _raise
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines[0] == "live"
    assert out_lines[1] == "liveness_basis:unknown"


def test_malformed_sid_emits_no_liveness_basis_line(stub_import_liveness_module, capsys):
    """Malformed/absent sid carries no decided verdict (exit 4) — no basis
    line is appended; baseline single-line "indeterminate" output unchanged."""

    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for a malformed sid")

    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    rc = _cli.main(["is-session-live", ""])
    assert rc == _cli._MALFORMED_SID
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["indeterminate"]


def test_transport_failure_emits_no_liveness_basis_line(stub_import_liveness_module, capsys):
    """Transport failure (exit 3) carries no decided verdict — no basis line,
    no stdout at all, matching the pre-C3 baseline."""

    def _raise_runtime_error():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable in test")

    _cli._import_liveness_module = _raise_runtime_error
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._TRANSPORT_FAIL
    assert capsys.readouterr().out == ""


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
# who-claims-path: reads the PATH-TOUCH plane (claim_index.lookup) + liveness
# per claimant, TAB-delimited "<sid>\t<live|dead>" rows, exit 0. A separate
# question from list-claims-by-session (artifact-claim store) above — see
# the CLI's own comment block.
# ---------------------------------------------------------------------------

def test_who_claims_path_no_claimant_exits_0_no_output(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):
    stub_import_claim_index_module(
        _StubClaimIndex(lookup=lambda paths, cwd=None: {p: [] for p in paths})
    )
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_who_claims_path_with_claimants_reports_liveness_per_row(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: {p: ["sess-live", "sess-dead"] for p in paths}
        )
    )
    stub_import_liveness_module(
        _StubLiveness(session_live=lambda sid, cwd=None: sid == "sess-live")
    )
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "sess-live\tlive\nsess-dead\tdead\n"


def test_who_claims_path_unanswerable_exits_nonzero_not_unclaimed(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):
    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for an unanswerable path")

    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: {p: [_StubClaimIndex.UNANSWERABLE] for p in paths}
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "could not be determined" in err
    assert "NOT a verdict that the path is unclaimed" in err


def test_who_claims_path_lookup_raise_exits_transport_fail(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):
    """Review: staff-eng slice-A P1 #1 — claim_index.lookup sits on the same
    arm this commit hardened for session_live; an unguarded raise there must
    not escape main() as a bare traceback (which exits 1, indistinguishable
    from a determinate "confirmed dead"-shaped exit on this CLI)."""

    def _raise(paths, cwd=None):
        raise RuntimeError("simulated claim_index.lookup failure")

    stub_import_claim_index_module(_StubClaimIndex(lookup=_raise))
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["indeterminate"]


def test_who_claims_path_multi_claimant_raise_mid_stream_emits_only_indeterminate(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):
    """Review: staff-eng slice-A P1 #2 — with N claimants, a raise on
    claimant k must not have already printed k-1 well-formed "sid\tstate"
    rows: that shape reads to a TAB-splitting consumer as a claimant
    literally named "indeterminate" with an empty state, not an abort
    marker. Buffering keeps the failure path's stdout exactly
    ["indeterminate"], same as is-session-live's own contract."""
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: {
                p: ["sess-live", "sess-raises", "sess-never-reached"] for p in paths
            }
        )
    )

    def _session_live(sid, cwd=None):
        if sid == "sess-live":
            return True
        if sid == "sess-raises":
            raise RuntimeError("simulated unexpected session_live failure")
        raise AssertionError("claimant after the raise must not be consulted")

    stub_import_liveness_module(_StubLiveness(session_live=_session_live))
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["indeterminate"]


def test_who_claims_path_path_and_cwd_forwarded(
    stub_import_claim_index_module, stub_import_liveness_module
):
    seen = {}

    def _lookup(paths, cwd=None):
        seen["lookup"] = (list(paths), cwd)
        return {p: [] for p in paths}

    stub_import_claim_index_module(_StubClaimIndex(lookup=_lookup))
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["who-claims-path", "some/path.txt", "/some/repo"])
    assert rc == 0
    assert seen["lookup"] == (["some/path.txt"], "/some/repo")


def test_who_claims_path_missing_path_arg_exits_usage_error(
    stub_import_claim_index_module, stub_import_liveness_module
):
    stub_import_claim_index_module(_StubClaimIndex())
    stub_import_liveness_module(_StubLiveness())
    rc = _cli.main(["who-claims-path"])
    assert rc == 2


def test_who_claims_path_transport_failure_exits_3(stub_import_liveness_module):
    def _raise_import_error():
        raise ImportError("coordinator_core.session.claim_index not importable in test")

    orig = _cli._import_claim_index_module
    _cli._import_claim_index_module = _raise_import_error
    try:
        rc = _cli.main(["who-claims-path", "some/path.txt"])
    finally:
        _cli._import_claim_index_module = orig
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
