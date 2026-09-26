from __future__ import annotations

import importlib.machinery
import importlib.util
import sys

import pytest

from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "session_claim_cli", str(_BIN_DIR / "session-claim-cli.py")
    )
    spec = importlib.util.spec_from_loader("session_claim_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _StubClaims:

    def __init__(self, *, claim_artifact=None, release_artifact=None,
                 clear_claim_if_dead=None, claim_plan=None,
                 list_claims_by_session=None, claim_dir_for=None):
        self.claim_artifact = claim_artifact or (lambda *a, **k: True)
        self.release_artifact = release_artifact or (lambda *a, **k: True)
        self.clear_claim_if_dead = clear_claim_if_dead or (lambda *a, **k: True)
        self.claim_plan = claim_plan or (lambda *a, **k: True)
        self.list_claims_by_session = list_claims_by_session or (lambda *a, **k: [])
        self.claim_dir_for = claim_dir_for or (lambda *a, **k: None)


class _StubLiveness:

    def __init__(self, *, session_live=None):
        self.session_live = session_live or (lambda *a, **k: True)


class _StubStaleClaims:

    def __init__(self, *, list_stale_claim_handoffs=None):
        self.list_stale_claim_handoffs = list_stale_claim_handoffs or (lambda *a, **k: [])


class _StubClaimIndex:

    UNANSWERABLE = "__UNANSWERABLE__"
    ABORT_CAUSE_EMPTY_BASE = "empty_base"
    ABORT_CAUSE_CAP_EXCEEDED = "cap_exceeded"
    ABORT_CAUSE_IO_ERROR = "io_error"

    def __init__(self, *, lookup=None):
        self.lookup = lookup or (lambda paths, cwd=None: {p: [] for p in paths})


class _LookupResultStub(dict):

    def __init__(
        self, mapping, abort_cause, *, recorded_name=None, edit_ts=None,
        recorded_kind=None,
    ):
        super().__init__(mapping)
        self.abort_cause = abort_cause
        self.recorded_name = recorded_name or {}
        self.edit_ts = edit_ts or {}
        self.recorded_kind = recorded_kind or {}


class _StubRegistryRecord:

    def __init__(self, name):
        self.name = name


class _StubHarnessRegistry:

    def __init__(self, *, lookup=None):
        self.lookup = lookup or (lambda sid: None)


class _StubHolderEvidence:

    def __init__(self, *, liveness_basis=None):
        self.liveness_basis = liveness_basis or (lambda *a, **k: "stable-pid")


@pytest.fixture()
def stub_import_module():
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
def stub_import_harness_registry_module():
    orig = _cli._import_harness_registry_module

    def _apply(stub):
        _cli._import_harness_registry_module = lambda: stub

    yield _apply
    _cli._import_harness_registry_module = orig


@pytest.fixture()
def stub_import_holder_evidence_module():
    orig = _cli._import_holder_evidence_module

    def _apply(stub):
        _cli._import_holder_evidence_module = lambda: stub

    yield _apply
    _cli._import_holder_evidence_module = orig


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


def _claim_dir_for_under(base):

    def _claim_dir_for(class_, basename, baton_repo_root="", cwd=None):
        return Path(base) / f"{class_}-claims" / basename

    return _claim_dir_for


def test_clear_claim_if_dead_bogus_basename_emits_not_found_note_exit_0(
    stub_import_module, tmp_path, capsys
):
    # Real claim dir exists under a DIFFERENT basename; the bogus one is not
    (tmp_path / "plan-claims" / "the-real-plan").mkdir(parents=True)
    stub_import_module(_StubClaims(
        clear_claim_if_dead=lambda *a, **k: True,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["clear-claim-if-dead", "plan", "bogus-basename"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "no claim at" in err
    assert "'plan'" in err
    assert "'bogus-basename'" in err
    assert "refusing to clear claim" not in err


def test_clear_claim_if_dead_bogus_basename_with_md_suffix_hints_extension_trap(
    stub_import_module, tmp_path, capsys
):
    stub_import_module(_StubClaims(
        clear_claim_if_dead=lambda *a, **k: True,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["clear-claim-if-dead", "plan", "some-plan.md"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "no claim at" in err
    assert "'.md' extension" in err


def test_release_artifact_bogus_basename_emits_not_found_note_exit_0(
    stub_import_module, tmp_path, capsys
):
    (tmp_path / "handoff-claims" / "the-real-handoff").mkdir(parents=True)
    stub_import_module(_StubClaims(
        release_artifact=lambda *a, **k: True,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["release-artifact", "handoff", "bogus-basename"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "release-artifact" in err
    assert "no claim at" in err
    assert "'handoff'" in err
    assert "'bogus-basename'" in err


def test_release_artifact_bogus_basename_with_md_suffix_hints_extension_trap(
    stub_import_module, tmp_path, capsys
):
    stub_import_module(_StubClaims(
        release_artifact=lambda *a, **k: True,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["release-artifact", "handoff", "some-handoff.md"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "no claim at" in err
    assert "'.md' extension" in err


def test_release_artifact_existing_claim_dir_emits_no_not_found_note(
    stub_import_module, tmp_path, capsys
):
    (tmp_path / "handoff-claims" / "real-handoff").mkdir(parents=True)
    stub_import_module(_StubClaims(
        release_artifact=lambda *a, **k: True,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["release-artifact", "handoff", "real-handoff"])

    assert rc == 0
    assert "no claim at" not in capsys.readouterr().err


def test_clear_claim_if_dead_correct_basename_live_holder_refuses_no_not_found_note(
    stub_import_module, tmp_path, capsys
):
    (tmp_path / "plan-claims" / "real-plan").mkdir(parents=True)

    def _refuse(*a, **k):
        print(
            "cs_clear_claim_if_dead: refusing to clear claim 'real-plan' — "
            "holder is live (session: some-sid)",
            file=sys.stderr,
        )
        return False

    stub_import_module(_StubClaims(
        clear_claim_if_dead=_refuse,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["clear-claim-if-dead", "plan", "real-plan"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to clear claim" in err
    assert "holder is live" in err
    assert _NOT_FOUND_MARKER not in err


def test_clear_claim_if_dead_correct_basename_dead_holder_clears_no_not_found_note(
    stub_import_module, tmp_path, capsys
):
    (tmp_path / "plan-claims" / "real-plan").mkdir(parents=True)
    stub_import_module(_StubClaims(
        clear_claim_if_dead=lambda *a, **k: True,
        claim_dir_for=_claim_dir_for_under(tmp_path),
    ))

    rc = _cli.main(["clear-claim-if-dead", "plan", "real-plan"])

    assert rc == 0
    err = capsys.readouterr().err
    assert _NOT_FOUND_MARKER not in err
    assert "refusing to clear claim" not in err


_NOT_FOUND_MARKER = "no claim at"


def _claim_dir_for_sentinel():
    calls = []

    def _claim_dir_for(class_, basename, baton_repo_root="", cwd=None):
        calls.append((class_, basename, baton_repo_root, cwd))
        return None

    return calls, _claim_dir_for


def test_clear_claim_if_dead_not_found_precheck_never_fires_for_artifact_class(
    stub_import_module, tmp_path, capsys
):
    """The 'artifact' class routes to the PATH-TOUCH plane inside claims.py,
    a different lookup entirely -- the classed-form not-found precheck must
    not fire for it."""
    calls, claim_dir_for = _claim_dir_for_sentinel()
    stub_import_module(_StubClaims(
        clear_claim_if_dead=lambda *a, **k: True,
        claim_dir_for=claim_dir_for,
    ))

    rc = _cli.main(["clear-claim-if-dead", "artifact", "some/repo/relative/path.txt"])

    assert rc == 0
    assert calls == [], "claim_dir_for must not be consulted for 'artifact' class"
    assert _NOT_FOUND_MARKER not in capsys.readouterr().err


def test_release_artifact_not_found_precheck_never_fires_for_artifact_class(
    stub_import_module, tmp_path, capsys
):
    """The same arm on the RELEASE door, which had no test at all and was
    the one that broke.

    `release-artifact artifact <path>` is the per-path self-release route,
    and as of 2026-09-20 it is what `coordinator-safe-commit`'s refusal
    sends a blocked holder to. With 'artifact' wrongly in
    `_CLASSED_CLAIM_CLASSES` it printed "no claim at <base>/artifact-claims/
    <path>" -- a directory nothing consults for this class -- over a release
    that then succeeded. A holder acting on that note concludes it has no
    claim to release and leaves the peer blocked, which is this row's
    original failure reached through the remedy."""
    calls, claim_dir_for = _claim_dir_for_sentinel()
    stub_import_module(_StubClaims(
        release_artifact=lambda *a, **k: True,
        claim_dir_for=claim_dir_for,
    ))

    rc = _cli.main(["release-artifact", "artifact", "coordinator_core/ipc.py"])

    assert rc == 0
    assert calls == [], "claim_dir_for must not be consulted for 'artifact' class"
    assert _NOT_FOUND_MARKER not in capsys.readouterr().err


def test_clear_claim_if_dead_claim_dir_for_failure_skips_precheck_not_transport_fail(
    stub_import_module, capsys
):
    """A resolution failure in the best-effort precheck (e.g. claim_dir_for
    raising) must never surface as _TRANSPORT_FAIL or change the delegated
    result -- it is diagnostic-only, per `_claim_lookup_dir`'s own
    contract."""

    def _raise(*a, **k):
        raise RuntimeError("claim_dir_for unavailable in test")

    stub_import_module(_StubClaims(
        clear_claim_if_dead=lambda *a, **k: True,
        claim_dir_for=_raise,
    ))

    rc = _cli.main(["clear-claim-if-dead", "plan", "some-basename"])

    assert rc == 0
    assert _NOT_FOUND_MARKER not in capsys.readouterr().err


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


def test_runtime_error_from_claude_klabauter_root_resolution_exits_3(stub_import_module):
    def _raise_runtime_error():
        raise RuntimeError("engine root unresolvable in test")

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
        raise RuntimeError("engine root unresolvable in test")

    _cli._import_module = _raise_runtime_error
    rc = _cli.main(["claim-plan", "some-slug"])
    assert rc == 3


def test_no_argv_exits_2(stub_import_module):
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


# claim-artifact / release-artifact / clear-claim-if-dead catch the REQUIRED-

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


# is-session-live exit-code contract: live sid -> 0; dead sid -> _NOT_LIVE
# (1); malformed/absent sid -> _MALFORMED_SID (4), NEVER the not-live code.

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
    # `ntpath.join(base, "C:evil")` DISCARDS `base` entirely, a full
    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for a malformed sid")

    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    for bad_sid in ("C:evil", "C:\\evil", "C:/Windows/Temp/x"):
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
    # code 1 — indistinguishable from `_NOT_LIVE`'s "confirmed dead"
    # top-level backstop in `main` must catch it and exit `_TRANSPORT_FAIL`
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
        raise RuntimeError("engine root unresolvable in test")

    _cli._import_liveness_module = _raise_runtime_error
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._TRANSPORT_FAIL


def test_session_live_raise_exits_transport_fail_not_dead(
    stub_import_liveness_module, capsys
):
    """
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
    assert out_lines[0] == "dead"
    assert out_lines[1] == "liveness_basis:recency-window"


def test_live_elsewhere_sid_reports_live_elsewhere_not_dead(
    stub_import_liveness_module, stub_import_holder_evidence_module, capsys
):
    # this sibling CLI. Exit code is unchanged (_NOT_LIVE) for compat.
    stub_import_liveness_module(_StubLiveness(session_live=lambda *a, **k: False))
    stub_import_holder_evidence_module(
        _StubHolderEvidence(liveness_basis=lambda *a, **k: "harness-registry-elsewhere")
    )
    rc = _cli.main(["is-session-live", "peer-sid"])
    assert rc == _cli._NOT_LIVE
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines[0] == "live-elsewhere"
    assert out_lines[1] == "liveness_basis:harness-registry-elsewhere"


def test_liveness_basis_call_reuses_holder_evidence_not_a_second_derivation(
    stub_import_liveness_module, stub_import_holder_evidence_module
):
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

    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for a malformed sid")

    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    rc = _cli.main(["is-session-live", ""])
    assert rc == _cli._MALFORMED_SID
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["indeterminate"]


def test_transport_failure_emits_no_liveness_basis_line(stub_import_liveness_module, capsys):

    def _raise_runtime_error():
        raise RuntimeError("engine root unresolvable in test")

    _cli._import_liveness_module = _raise_runtime_error
    rc = _cli.main(["is-session-live", "some-sid"])
    assert rc == _cli._TRANSPORT_FAIL
    assert capsys.readouterr().out == ""


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


# who-claims-path: reads the PATH-TOUCH plane (claim_index.lookup) + liveness

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
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    """No recorded name and no registry record for either claimant -- both
    rows degrade to rung 3's NO-RECORD marker, alongside the unchanged
    sid/liveness columns. The registry answered here; it simply holds
    nothing, which is a different outcome from it being unaskable."""
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: {p: ["sess-live", "sess-dead"] for p in paths}
        )
    )
    stub_import_liveness_module(
        _StubLiveness(session_live=lambda sid, cwd=None: sid == "sess-live")
    )
    stub_import_harness_registry_module(_StubHarnessRegistry())
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == (
        f"sess-live\tlive\t{_cli._NO_REGISTRY_RECORD_MARKER}\t{_cli._UNKNOWN_KIND_MARKER}\n"
        f"sess-dead\tdead\t{_cli._NO_REGISTRY_RECORD_MARKER}\t{_cli._UNKNOWN_KIND_MARKER}\n"
    )


def test_who_claims_path_rung1_recorded_name_wins_over_live_registry(
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    from datetime import datetime, timedelta, timezone

    ts = datetime.now(timezone.utc) - timedelta(hours=2)
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: ["sess-a"] for p in paths},
                None,
                recorded_name={"some/path.txt": {"sess-a": "claude-klabauter-57"}},
                edit_ts={"some/path.txt": {"sess-a": ts}},
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=lambda sid, cwd=None: True))
    stub_import_harness_registry_module(
        _StubHarnessRegistry(
            lookup=lambda sid: (_ for _ in ()).throw(
                AssertionError("rung 2 must not be consulted when rung 1 resolves")
            )
        )
    )
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("sess-a\tlive\tproject-claude-klabauter-57 (recorded name")
    assert "held 2.0h" in out


def test_who_claims_path_labels_the_kind_of_each_hold(
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: ["sess-w", "sess-r", "sess-legacy"] for p in paths},
                None,
                recorded_kind={p: {"sess-w": "w", "sess-r": "r"} for p in paths},
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=lambda sid, cwd=None: True))
    stub_import_harness_registry_module(_StubHarnessRegistry(lookup=lambda sid: None))

    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0

    kinds = {
        row.split("\t")[0]: row.split("\t")[3]
        for row in capsys.readouterr().out.splitlines()
        if row
    }
    assert kinds == {
        "sess-w": "write",
        "sess-r": "read",
        "sess-legacy": _cli._UNKNOWN_KIND_MARKER,
    }


def test_the_kind_column_never_takes_down_the_row(capsys):
    class _NoKindField:
        pass

    assert _cli._render_claimant_kind("s", "p", _NoKindField()) == _cli._UNKNOWN_KIND_MARKER


def test_who_claims_path_rung1_absent_falls_to_rung2_live_registry(
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: ["sess-b"] for p in paths}, None,
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=lambda sid, cwd=None: True))
    stub_import_harness_registry_module(
        _StubHarnessRegistry(
            lookup=lambda sid: _StubRegistryRecord("claude-klabauter-99")
            if sid == "sess-b"
            else None
        )
    )
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == (
        "sess-b\tlive\tproject-claude-klabauter-99 (live harness registry lookup)\t"
        f"{_cli._UNKNOWN_KIND_MARKER}\n"
    )


def test_who_claims_path_neither_rung_resolves_prints_unnamed_marker(
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: ["sess-c"] for p in paths}, None,
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=lambda sid, cwd=None: False))
    stub_import_harness_registry_module(_StubHarnessRegistry(lookup=lambda sid: None))
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == (
        f"sess-c\tdead\t{_cli._NO_REGISTRY_RECORD_MARKER}\t{_cli._UNKNOWN_KIND_MARKER}\n"
    )
    assert "sess-c\t" not in _cli._NO_REGISTRY_RECORD_MARKER
    # The registry ANSWERED and holds nothing. That is a fact, and it must not
    assert _cli._NO_REGISTRY_RECORD_MARKER != _cli._NAME_UNRESOLVED_MARKER


def test_who_claims_path_rung2_registry_raise_degrades_to_unnamed(
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: ["sess-d"] for p in paths}, None,
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=lambda sid, cwd=None: True))

    def _raise(sid):
        raise RuntimeError("simulated registry lookup failure")

    stub_import_harness_registry_module(_StubHarnessRegistry(lookup=_raise))
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == (
        f"sess-d\tlive\t{_cli._NAME_UNRESOLVED_MARKER}\t{_cli._UNKNOWN_KIND_MARKER}\n"
    )
    # A DEGRADATION, not a fact: the registry was never successfully asked, so
    assert _cli._NAME_UNRESOLVED_MARKER != _cli._NO_REGISTRY_RECORD_MARKER


def test_who_claims_path_rung1_name_never_asserts_present_tense_reachability(
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
    """NEGATIVE SPEC (C2): a rung-1 (recorded-name) rendering never claims
    the name is presently reachable -- it must carry a staleness/verify
    warning and must NOT read as ready-to-SendMessage. Guards against a
    future edit dropping the qualifier and silently re-authorizing the
    exact stale-address failure C2 exists to prevent."""
    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: ["sess-e"] for p in paths},
                None,
                recorded_name={"some/path.txt": {"sess-e": "claude-klabauter-12"}},
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=lambda sid, cwd=None: True))
    stub_import_harness_registry_module(
        _StubHarnessRegistry(
            lookup=lambda sid: (_ for _ in ()).throw(
                AssertionError("rung 2 must not be consulted when rung 1 resolves")
            )
        )
    )
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "provenance only, not a live address" in out
    assert "verify" in out
    assert "SendMessage" not in out
    assert "ready to send" not in out


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


@pytest.mark.parametrize(
    "abort_cause",
    [
        _StubClaimIndex.ABORT_CAUSE_EMPTY_BASE,
        _StubClaimIndex.ABORT_CAUSE_CAP_EXCEEDED,
        _StubClaimIndex.ABORT_CAUSE_IO_ERROR,
    ],
)
def test_who_claims_path_unanswerable_reports_which_abort_fired(
    stub_import_claim_index_module, stub_import_liveness_module, capsys, abort_cause
):
    """AC4/AC6: the cause is printed ADDITIVE to the existing refusal
    sentence, which survives verbatim, and the exit code does not move
    (still 1, per AC5) — the parametrization drives all three causes from
    C1 through the same CLI arm."""

    def _fail_if_called(*a, **k):
        raise AssertionError("liveness must not be consulted for an unanswerable path")

    stub_import_claim_index_module(
        _StubClaimIndex(
            lookup=lambda paths, cwd=None: _LookupResultStub(
                {p: [_StubClaimIndex.UNANSWERABLE] for p in paths}, abort_cause
            )
        )
    )
    stub_import_liveness_module(_StubLiveness(session_live=_fail_if_called))
    rc = _cli.main(["who-claims-path", "some/path.txt"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not be determined" in err
    assert "NOT a verdict that the path is unclaimed" in err
    assert f"abort cause: {abort_cause}" in err


def test_who_claims_path_unanswerable_with_no_abort_cause_reports_unknown(
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
    assert rc == 1
    err = capsys.readouterr().err
    assert "abort cause: unknown" in err


def test_who_claims_path_lookup_raise_exits_transport_fail(
    stub_import_claim_index_module, stub_import_liveness_module, capsys
):

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
    stub_import_claim_index_module, stub_import_liveness_module,
    stub_import_harness_registry_module, capsys,
):
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
    stub_import_harness_registry_module(_StubHarnessRegistry())
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


def test_help_flag_exits_0(stub_import_module):
    def _fail_if_called():
        raise AssertionError("help flags must not reach _import_module")

    _cli._import_module = _fail_if_called

    for flag in ("--help", "-h", "help"):
        rc = _cli.main([flag])
        assert rc == 0


# The advertised verb list must reach the PATH-TOUCH release path.
# claim `who-claims-path` reports. A peer EM read `_SUBCOMMANDS`, saw eight


def test_subcommand_advertisement_names_the_artifact_path_class():
    advert = _cli._SUBCOMMANDS
    assert "artifact" in advert
    assert "release-artifact <class>" in advert
    assert "PATH" in advert or "path" in advert


def test_dispatch_import_chokepoint_is_reused_by_every_claim_query_seam(monkeypatch):
    calls = []
    monkeypatch.setattr(_cli, "_dispatch_import", lambda name: calls.append(name) or name)

    assert _cli._import_module() == "coordinator_core.session.claims"
    assert _cli._import_liveness_module() == "coordinator_core.session.liveness"
    assert _cli._import_stale_claims_module() == "coordinator_core.session.stale_claims"
    assert _cli._import_claim_index_module() == "coordinator_core.session.claim_index"
    assert calls == [
        "coordinator_core.session.claims",
        "coordinator_core.session.liveness",
        "coordinator_core.session.stale_claims",
        "coordinator_core.session.claim_index",
    ]


def _rig_stale_mirror(monkeypatch, tmp_path, missing_module):
    source_root = tmp_path / "source"
    dispatch_root = tmp_path / "dispatch"
    (source_root / "coordinator_core" / "session").mkdir(parents=True)
    (source_root / "coordinator_core" / "session" / "__init__.py").write_text("")
    (source_root / "coordinator_core" / "session" / f"{missing_module}.py").write_text("X = 1\n")
    (dispatch_root / "coordinator_core" / "session").mkdir(parents=True)
    (dispatch_root / "coordinator_core" / "session" / "__init__.py").write_text("")

    cc_mod = _cli._cc_invoke()
    monkeypatch.setattr(cc_mod, "require_dispatch_engine_on_path", lambda: str(dispatch_root))
    monkeypatch.setattr(cc_mod, "resolve_engine_root", lambda script_file: str(source_root))

    dotted = f"coordinator_core.session.{missing_module}"

    def _boom(name):
        raise ImportError(f"No module named '{dotted}'", name=dotted)

    monkeypatch.setattr(cc_mod.importlib, "import_module", _boom)
    return dotted


def test_is_session_live_stale_mirror_reports_diagnosis_not_bare_import_error(
    monkeypatch, tmp_path, capsys
):
    _rig_stale_mirror(monkeypatch, tmp_path, "liveness")

    rc = _cli.main(["is-session-live", "some-sid"])

    assert rc == _cli._TRANSPORT_FAIL
    err = capsys.readouterr().err
    assert "coordinator_core.session.liveness" in err
    assert "publish" in err.lower()
    assert "CLAUDE_KLABAUTER_ROOT resolution failed" not in err


def test_list_stale_claim_handoffs_stale_mirror_reports_diagnosis(
    monkeypatch, tmp_path, capsys
):
    _rig_stale_mirror(monkeypatch, tmp_path, "stale_claims")

    rc = _cli.main(["list-stale-claim-handoffs"])

    assert rc == _cli._TRANSPORT_FAIL
    err = capsys.readouterr().err
    assert "coordinator_core.session.stale_claims" in err
    assert "publish" in err.lower()
    assert "CLAUDE_KLABAUTER_ROOT resolution failed" not in err


def test_who_claims_path_claim_index_stale_mirror_reports_diagnosis(
    monkeypatch, tmp_path, capsys
):
    _rig_stale_mirror(monkeypatch, tmp_path, "claim_index")

    rc = _cli.main(["who-claims-path", "some/path.txt"])

    assert rc == _cli._TRANSPORT_FAIL
    err = capsys.readouterr().err
    assert "coordinator_core.session.claim_index" in err
    assert "publish" in err.lower()
    assert "CLAUDE_KLABAUTER_ROOT resolution failed" not in err


def test_who_claims_path_name_ladder_stale_mirror_reports_diagnosis_via_backstop(
    monkeypatch, tmp_path, capsys
):
    dotted = _rig_stale_mirror(monkeypatch, tmp_path, "name_ladder")

    monkeypatch.setattr(
        _cli,
        "_import_claim_index_module",
        lambda: _StubClaimIndex(lookup=lambda paths, cwd=None: {p: ["sess-a"] for p in paths}),
    )
    monkeypatch.setattr(
        _cli, "_import_liveness_module", lambda: _StubLiveness(session_live=lambda *a, **k: True)
    )

    rc = _cli.main(["who-claims-path", "some/path.txt"])

    assert rc == _cli._TRANSPORT_FAIL
    assert rc == 3
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["indeterminate"]
    assert dotted in captured.err
    assert "publish" in captured.err.lower()


def test_stale_mirror_not_in_source_either_never_says_publish(monkeypatch, tmp_path, capsys):
    source_root = tmp_path / "source"
    dispatch_root = tmp_path / "dispatch"
    (source_root / "coordinator_core" / "session").mkdir(parents=True)
    (dispatch_root / "coordinator_core" / "session").mkdir(parents=True)

    cc_mod = _cli._cc_invoke()
    monkeypatch.setattr(cc_mod, "require_dispatch_engine_on_path", lambda: str(dispatch_root))
    monkeypatch.setattr(cc_mod, "resolve_engine_root", lambda script_file: str(source_root))

    dotted = "coordinator_core.session.totally_made_up_thing"

    def _boom(name):
        raise ImportError(f"No module named '{dotted}'", name=dotted)

    monkeypatch.setattr(cc_mod.importlib, "import_module", _boom)

    rc = _cli.main(["is-session-live", "some-sid"])

    assert rc == _cli._TRANSPORT_FAIL
    err = capsys.readouterr().err
    assert "not in source either" in err
    assert "publish" not in err.lower()
