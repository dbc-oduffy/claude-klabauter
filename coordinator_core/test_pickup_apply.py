"""
coordinator_core.test_pickup_apply — co-located pytest for
coordinator_core.pickup_assemble.apply.

Covers the closed-dispatch security bound (AC9e) as a data-flow property (not
import hygiene) and the apply-specific exit-code contract (AC9g), including
the partial-mutation state. Also covers C2b: already_satisfied-skip,
depends_on ordering, the bool-vs-int return-type normalization, the
claim_grant freshness recheck, the AC9f general revalidate_at_dispatch rule
on a non-claim_grant judgment point, and the AC5c/AC5d directive-execution
seam. Also covers C2c (AC10): the scoped commit's explicit pathspec survives
a dirty shared index with two sibling sessions' own files staged. Also covers
C2d: `drop`'s clean round-trip and its behaviour on a half-applied
(partial-mutation) artifact, the session-scoped decision-file disposition
round-trip, and `drop`'s own explicit-session-id-under-two-live-sessions red
test. Deliberately does NOT import test_pickup_assemble.py's fixtures — that
file is edited by a concurrent chunk in this same plan; fixtures here are
self-contained.

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_pickup_apply.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble.apply as pa_apply

# Real-git spawn is load-bearing: AC10's scoped-commit test proves the
# explicit pathspec survives a dirty shared index with real sibling-session
# files staged, which requires an actual git index/worktree — no mock stands
# in for that. Fixtures spin up per-test repos (mutation-heavy: commits/
# staged-index state per test), so not hoisted to module scope.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENV_KEYS_TO_ISOLATE = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


@pytest.fixture(autouse=True)
def _isolated_session_env(monkeypatch):
    """Every test starts with a clean identity-env slate — apply's own
    session-id propagation is exactly what's under test, and a leaked
    COORDINATOR_SESSION_ID from the outer test-runner env would silently mask
    a broken propagation path."""
    for key in _ENV_KEYS_TO_ISOLATE:
        monkeypatch.delenv(key, raising=False)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_handoff_ready_to_fire(repo: Path, name: str) -> Path:
    """Same shape as `_seed_handoff`, but `deployment_state: ready_to_fire` —
    the modern-vocabulary "on the shelf, never yet claimed" state, distinct
    from `_seed_handoff`'s retiring `deployment_state: active`. C2d's
    half-applied-drop test needs THIS shape: `cs_unclaim_handoff`'s own
    precondition requires `deployment_state` in `{in_flight, ready_to_fire}`
    (`handoff_transition.py:778`) — an untouched `active` fixture would fail
    that precondition rather than idempotently no-op, so a genuinely
    not-yet-claimed handoff for this test must already read
    `ready_to_fire`, not the old-vocab `active` the other fixture uses."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_memo(repo: Path, name: str) -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        "status: open\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_foreign_fresh_claim(repo: Path, class_: str, basename: str, foreign_sid: str) -> None:
    """Fabricates a claim dir held by a session id nobody has registered as
    live and with a just-now `claimed_at` — compute_claim_grant's row 4 (not
    live, inside the settling window) resolves this to `verdict: denied`
    without this test needing a real second live session."""
    claim_dir = repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "pid").write_text("999999\n", encoding="utf-8")
    (claim_dir / "session_id").write_text(f"{foreign_sid}\n", encoding="utf-8")
    from coordinator_core.session import core as _session_core

    (claim_dir / "claimed_at").write_text(f"{_session_core.now_iso()}\n", encoding="utf-8")


def _register_live_session(repo: Path, sid: str) -> None:
    """Makes `sid` read LIVE per `session.liveness.session_live`'s Layer-2
    recency fallback: an empty, freshly-created session dir with no
    meta.json falls back to the dir's own (just-now) mtime, which is well
    inside the 30-minute recency window."""
    (repo / ".git" / "coordinator-sessions" / sid).mkdir(parents=True, exist_ok=True)


def _seed_handoff_claimed_by(repo: Path, name: str, claimed_by_sid: str) -> Path:
    """Same shape as `_seed_handoff`, but with a `claimed_by` frontmatter
    field naming a (separately registered) live peer session — the signal
    `compute_liveness_signal`'s row (a) reads, independent of the
    `.git/coordinator-sessions/handoff-claims/` claim-dir `compute_claim_grant`
    consults."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
        f"claimed_by: {claimed_by_sid}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


# ---------------------------------------------------------------------------
# AC9e (a) — the dispatch resolver is a unit test on the resolver itself, not
# a synthetic-brief/CLI-level test (the Staff Engineer finding #6): no reachable artifact
# can produce an unmapped `cli` value, since every literal the assembler
# emits is hardcoded.
# ---------------------------------------------------------------------------

class TestResolveCliClosedDispatch:
    def test_unrecognized_cli_raises_and_mutates_nothing(self, tmp_path):
        with pytest.raises(pa_apply.UnrecognizedDirective):
            pa_apply._resolve_cli("rm")
        # "mutates nothing": the closed dict itself was never touched, and no
        # filesystem side effect exists for this call to have produced.
        assert list(tmp_path.iterdir()) == []

    def test_every_real_directive_cli_resolves(self):
        for name in ("session-claim-cli", "archive-stamp-cli", "coordinator-tasks-mirror"):
            assert callable(pa_apply._resolve_cli(name))

    def test_dispatch_table_is_closed_over_exactly_the_real_set(self):
        assert set(pa_apply._CLI_DISPATCH) == {
            "session-claim-cli",
            "archive-stamp-cli",
            "coordinator-tasks-mirror",
        }


# ---------------------------------------------------------------------------
# AC9e (b) — replaces the decorative "no subprocess import" grep (the Staff Engineer
# finding #1): monkeypatch subprocess.run for the duration of an apply run
# and assert every recorded argv[0] is `git`, and no directive `args` element
# ever appears in any recorded argv.
# ---------------------------------------------------------------------------

class TestNoBriefDerivedArgvReachesSubprocess:
    def test_apply_run_only_ever_shells_to_git_with_no_leaked_args(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        recorded_argvs: list[list[str]] = []
        real_run = subprocess.run

        def _recording_run(args, *a, **kw):
            recorded_argvs.append(list(args))
            return real_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", _recording_run)

        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="test-sid-1", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]
        assert recorded_argvs, "expected at least one subprocess.run call (git)"
        for argv in recorded_argvs:
            assert argv[0] == "git"

        leaked_terms = {"handoff", "state/handoffs/h1.md", "h1.md", "claim-artifact", "claim-handoff"}
        flat = {term for argv in recorded_argvs for term in argv}
        # `h1.md`/its path legitimately appear in ordinary git plumbing calls
        # (e.g. `git log -- state/handoffs/h1.md`) as part of THIS module's
        # own read-only preflight — the property under test is narrower than
        # "the string never appears anywhere": no directive's `args` LIST
        # element is ever forwarded VERBATIM as a subprocess.run `cli` name
        # like `claim-artifact`/`claim-handoff`, which would only show up
        # if a handler had shelled out instead of calling in-process.
        assert "claim-artifact" not in flat
        assert "claim-handoff" not in flat


# ---------------------------------------------------------------------------
# AC9g — exit-code contract for the mutating half, all five codes.
# ---------------------------------------------------------------------------

class TestExitCodeContract:
    def test_applied_clean(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-clean", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]

    def test_halted_at_judgment(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        exit_code, report = pa_apply.apply(
            "cross-repo/inbox/m1.md", session_id="sid-halt", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        # C7 (per-directive halt) + C8 (bank-the-grab): the mechanical
        # claim/stamp directives land even on a halt — only the terminal
        # "action-memo" directive (gated on the still-open kind-dispatch
        # judgment point) stays unfired.
        assert report["landed"] == ["d1", "claim-memo-stamp"]
        assert "action-memo" not in report["landed"]
        assert report["unresolved_judgment_points"]

    def test_claim_denied(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _write_foreign_fresh_claim(repo, "handoff", "h1.md", foreign_sid="some-other-session")

        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-denied", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
        assert report["landed"] == []
        assert report["claim_grant"]["verdict"] == "denied"

    def test_transport_failure_no_repo_root(self, tmp_path):
        # tmp_path is deliberately NOT a git worktree.
        exit_code, report = pa_apply.apply(
            "state/handoffs/missing.md", session_id="sid-transport", repo_root=tmp_path
        )

        assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert report["error"]

    def test_transport_failure_unresolvable_artifact(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        exit_code, report = pa_apply.apply(
            "state/handoffs/does-not-exist.md", session_id="sid-transport-2", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL

    def test_partial_mutation_reports_exactly_which_directives_landed(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated archive-stamp-cli failure")

        monkeypatch.setattr(pa_apply, "cs_claim_handoff", _boom)

        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-partial", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_PARTIAL_MUTATION
        assert report["landed"] == ["d1"]
        assert report["failed_directive"] == "d2"


# ---------------------------------------------------------------------------
# Session-id propagation (AC9(a)) — explicit session id never falls through
# to the ambient tier-4 sentinel; both identity chains apply's composed
# primitives use are pinned to the same explicit id.
# ---------------------------------------------------------------------------

class TestSessionIdPropagation:
    def test_no_resolvable_session_id_fails_transport_not_ambient(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        exit_code, report = pa_apply.apply("state/handoffs/h1.md", repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "ambient tier-4" in report["error"]

    def test_explicit_session_id_pins_both_identity_chains_during_the_call(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        exit_code, _report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-pinned", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        # The context manager restores the pre-call env on exit — propagation
        # during the call, not a process-wide leak afterward.
        assert os.environ.get("COORDINATOR_SESSION_ID") is None
        assert os.environ.get("CLAUDE_SESSION_ID") is None

    def test_coordinator_session_id_env_honored_without_explicit_flag(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-from-env")

        exit_code, report = pa_apply.apply("state/handoffs/h1.md", repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]


# ---------------------------------------------------------------------------
# In-repo path bound (AC9e) — the resolved artifact/basename path is asserted
# inside repo_root before any mutation.
# ---------------------------------------------------------------------------

class TestInRepoPathBound:
    def test_archive_stamp_handler_rejects_an_out_of_repo_path(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        with pytest.raises(pa_apply.OutOfRepoPath):
            pa_apply._dispatch_archive_stamp_cli(
                ["claim-handoff", "../../etc/passwd"], repo
            )

    def test_session_claim_cli_rejects_a_basename_with_path_traversal(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        with pytest.raises(pa_apply.OutOfRepoPath):
            pa_apply._dispatch_session_claim_cli(
                ["claim-artifact", "handoff", "../escape.md"], repo
            )


class TestConsumeHandoffDeprecatedAlias:
    """DR-084 verb rename: `apply()` itself only ever EMITS the canonical
    "claim-handoff" verb (see the leaked_terms assertion above), but
    `_dispatch_archive_stamp_cli` must still accept the pre-rename
    "consume-handoff" spelling on the READ side, so an in-flight
    pre-computed decision object emitted before this rename does not break."""

    def test_dispatch_accepts_deprecated_consume_handoff_verb(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h1.md")

        monkeypatch.setattr(
            pa_apply,
            "cs_claim_handoff",
            lambda _path, return_result=False: (
                {"exit_code": 0, "applied": True} if return_result else 0
            ),
        )

        result = pa_apply._dispatch_archive_stamp_cli(
            ["consume-handoff", str(hp.relative_to(repo))], repo
        )
        # Same normalized-canonical shape apply() itself emits for d2 —
        # dispatching the deprecated alias never leaks the old spelling
        # back into the directive report.
        assert result["verb"] == "claim-handoff"


# ---------------------------------------------------------------------------
# C2b — already_satisfied skip: a second apply on a self-held artifact is a
# clean no-op, not a failure.
# ---------------------------------------------------------------------------

class TestAlreadySatisfiedSkip:
    def test_already_satisfied_directive_is_landed_without_dispatching_its_handler(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        def _boom(*_a, **_kw):
            raise AssertionError("already_satisfied directive must never reach its underlying primitive")

        # `_dispatch_session_claim_cli` calls `claim_artifact` internally —
        # patching it out proves d1's handler body never runs, without
        # touching the closed `_CLI_DISPATCH` table itself.
        monkeypatch.setattr(pa_apply, "claim_artifact", _boom)

        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": True},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "d1", "already_satisfied": False},
        ]

        with pa_apply._session_identity("sid-already-satisfied"):
            exit_code, report = pa_apply._execute_directives(directives, [], repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]
        result_by_id = {r["id"]: r for r in report["results"]}
        assert result_by_id["d1"]["already_satisfied"] is True
        assert result_by_id["d1"]["detail"] is None
        assert result_by_id["d2"]["already_satisfied"] is False

    def test_a_second_apply_on_a_self_held_directive_set_is_a_clean_no_op(self, tmp_path):
        # AC3b (the Director of Engineering review F2): an artifact whose brief() recompute reports
        # `directives[].already_satisfied` for a self-held claim (the shape
        # __init__.py's assembler is contracted to emit for the self-holder
        # row of `gates.claim_grant`'s truth table) reruns as a clean no-op —
        # exercised at this seam per C1d, with a hand-built `directives[]`,
        # rather than depending on brief()'s own end-to-end production of
        # that shape (a concurrent chunk's surface, not this module's).
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        already_held_directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": True},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "d1", "already_satisfied": False},
        ]

        with pa_apply._session_identity("sid-self"):
            exit_code, report = pa_apply._execute_directives(already_held_directives, [], repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]
        assert report["results"][0]["already_satisfied"] is True


# ---------------------------------------------------------------------------
# C2b — directive ordering: depends_on is honored, not list position.
# ---------------------------------------------------------------------------

class TestDependsOnOrdering:
    def test_a_directive_dispatches_after_the_directive_it_depends_on_even_out_of_list_order(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        # d2 (claim-handoff) listed FIRST, depends_on d1 (claim-artifact)
        # listed SECOND — execution must still run d1 before d2.
        directives = [
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "d1", "already_satisfied": False},
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": False},
        ]

        with pa_apply._session_identity("sid-order-1"):
            exit_code, report = pa_apply._execute_directives(directives, [], repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]

    def test_a_depends_on_value_naming_a_judgment_point_id_is_ignored_once_judgment_points_is_empty(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        # Mirrors what the assembler leaves on directives[1] when a liveness
        # signal fires transiently and is later cleared: depends_on may still
        # name a judgment-point id ("j1") that is not itself a directive.
        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": False},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "j1", "already_satisfied": False},
        ]

        with pa_apply._session_identity("sid-order-2"):
            exit_code, report = pa_apply._execute_directives(directives, [], repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d2"]

    def test_a_genuine_cycle_between_two_directives_fails_loud_and_mutates_nothing(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": "d2", "already_satisfied": False},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "d1", "already_satisfied": False},
        ]

        exit_code, report = pa_apply._execute_directives(directives, [], repo)

        assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert report["landed"] == []


# ---------------------------------------------------------------------------
# C2b — return-type normalization: claims.* (bool) vs cs_*/cmd_init (int)
# never leaks its raw asymmetry into a directive's success/failure reading.
# ---------------------------------------------------------------------------

class TestReturnTypeNormalization:
    def test_bool_true_normalizes_to_success(self):
        assert pa_apply._normalize_primitive_result(True) is True

    def test_bool_false_normalizes_to_failure(self):
        assert pa_apply._normalize_primitive_result(False) is False

    def test_int_zero_normalizes_to_success_not_falsy(self):
        # The exact asymmetry this normalizes: a bare `if rc:` on an `int`
        # exit code reads a successful `0` as falsy/failure.
        assert pa_apply._normalize_primitive_result(0) is True

    def test_nonzero_int_normalizes_to_failure(self):
        assert pa_apply._normalize_primitive_result(1) is False
        assert pa_apply._normalize_primitive_result(-1) is False

    def test_unrecognized_type_raises_rather_than_silently_coercing(self):
        with pytest.raises(TypeError):
            pa_apply._normalize_primitive_result("0")

    def test_archive_stamp_handler_treats_a_nonzero_exit_as_failure_via_normalization(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        handoff = _seed_handoff(repo, "h1.md")

        monkeypatch.setattr(
            pa_apply,
            "cs_claim_handoff",
            lambda _path, return_result=False: (
                {"exit_code": 1, "applied": False, "error": "simulated failure"}
                if return_result
                else 1
            ),
        )

        with pytest.raises(RuntimeError):
            pa_apply._dispatch_archive_stamp_cli(
                ["claim-handoff", str(handoff.relative_to(repo))], repo
            )


# ---------------------------------------------------------------------------
# AC5c/AC5d — the directive-execution seam (decided C1d): a unit test
# directly on `_execute_directives`, not an end-to-end CLI/brief-injection
# test, with two `judgment_points` lists differing only in `recommendation`.
# ---------------------------------------------------------------------------

class TestJudgmentHaltIgnoresRecommendation:
    # C7 (per-directive halt): d1 depends_on names the judgment point under
    # test, so it is actually gated by it — a bare `depends_on: None` (fine
    # under the old blunt whole-run-halts rule) would fire unconditionally
    # under the new per-directive predicate and never exercise the halt at
    # all, no matter what `judgment_points` carries.
    _DIRECTIVES = [
        {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": "j1", "already_satisfied": False},
    ]

    def test_halts_regardless_of_a_recommendation_on_every_judgment_point(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        judgment_points_with_recommendation = [
            {
                "id": "j1",
                "question": "Any peer live?",
                "recommendation": {"disposition": "proceed", "rationale": "no live peer seen"},
                "revalidate_at_dispatch": True,
            }
        ]

        exit_code, report = pa_apply._execute_directives(
            self._DIRECTIVES, judgment_points_with_recommendation, repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == []

    def test_bare_and_recommendation_bearing_judgment_points_produce_the_identical_halt(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        bare = [{"id": "j1", "question": "Any peer live?", "revalidate_at_dispatch": True}]
        with_recommendation = [dict(bare[0], recommendation={"disposition": "proceed", "rationale": "r"})]

        exit_bare, report_bare = pa_apply._execute_directives(self._DIRECTIVES, bare, repo)
        exit_reco, report_reco = pa_apply._execute_directives(self._DIRECTIVES, with_recommendation, repo)

        assert exit_bare == exit_reco == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report_bare["unresolved_judgment_points"] == report_reco["unresolved_judgment_points"]
        assert report_bare["landed"] == report_reco["landed"] == []

    def test_empty_judgment_points_proceeds_to_execution_regardless_of_recommendation_absence(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        with pa_apply._session_identity("sid-recommend-1"):
            exit_code, report = pa_apply._execute_directives(self._DIRECTIVES, [], repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1"]


# ---------------------------------------------------------------------------
# AC9f — claim_grant is re-resolved immediately before mutating and never
# trusts a brief-time snapshot; the same freshness discipline generalizes
# (not special-cased) to any OTHER judgment point, achieved structurally
# rather than via a per-judgment-point flag. As of chunk C7 Part A (AC13b)
# j1 no longer carries `revalidate_at_dispatch: true` — apply() re-derives
# j1 on every call because it always recomputes brief() fresh from disk,
# never trusting a cached decision object. claim_grant remains the one
# input this module explicitly re-resolves via a dedicated closure, but the
# *general* rule (never trust a stale snapshot) holds for j1 too.
# Review: code-reviewer — reworded to describe the actual mechanism after
# revalidate_at_dispatch was retired for j1 (Finding 1).
# ---------------------------------------------------------------------------

class TestRevalidateAtDispatchGeneralRule:
    def test_claim_grant_reresolution_catches_a_peer_that_claimed_between_compute_and_apply(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        # A "compute" (an earlier `brief` read) saw no competing claim —
        # simulated here by monkeypatching apply's own `brief` call to a
        # canned, stale-clear decision object. A peer then claims for real,
        # on disk, between that compute and this `apply` call.
        from coordinator_core.pickup_assemble import BriefResult, EXIT_OK as _BRIEF_OK

        stale_clear_decision = {
            "artifact": {"path": "state/handoffs/h1.md", "classification": "handoff"},
            "directives": [
                {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": False},
                {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": None, "already_satisfied": False},
            ],
            "judgment_points": [],
        }
        monkeypatch.setattr(pa_apply, "brief", lambda *a, **kw: BriefResult(stale_clear_decision, _BRIEF_OK))

        _write_foreign_fresh_claim(repo, "handoff", "h1.md", foreign_sid="peer-claimed-after-compute")

        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-race", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
        assert report["claim_grant"]["verdict"] == "denied"
        assert report["landed"] == []

    def test_general_rule_on_a_non_claim_grant_judgment_point_a_peer_liveness_signal_firing_between_calls(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        # "Compute": an earlier read of the brief sees a clear coast — no
        # live peer yet, so no j1 judgment point.
        from coordinator_core.pickup_assemble import brief as real_brief

        compute_time_result = real_brief("state/handoffs/h1.md", repo_root=repo)
        assert compute_time_result.decision_object["judgment_points"] == []

        # A peer claims the handoff (compute_liveness_signal row (a)) and
        # registers itself live, for real, between that compute and this
        # `apply` invocation.
        peer_sid = "peer-live-1"
        _register_live_session(repo, peer_sid)
        handoff_path = repo / "state" / "handoffs" / "h1.md"
        content = handoff_path.read_text(encoding="utf-8")
        content = content.replace("deployment_state: active\n", f"deployment_state: active\nclaimed_by: {peer_sid}\n")
        handoff_path.write_text(content, encoding="utf-8")
        _git(repo, "add", "state/handoffs/h1.md")
        _git(repo, "commit", "-m", "peer claims h1")

        # `apply` never consults `compute_time_result` — it recomputes
        # `brief()` fresh in-process. AC9f: the general rule holds for j1
        # (re-derived structurally on every apply() call, NOT via a
        # revalidate_at_dispatch flag) exactly as it does for claim_grant —
        # a disposition an EM might have formed from the earlier clear
        # compute is never reusable here; apply halts.
        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-race-2", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        # C7/C8: d1 (claim-artifact, depends_on: None) banks the grab even
        # though the run halts — j1 (revalidate_at_dispatch, NOT
        # claim_grant) blocks whatever downstream directive depends on it,
        # not d1 itself.
        assert report["landed"] == ["d1"]
        assert "j1" in report["unresolved_judgment_points"]


# ---------------------------------------------------------------------------
# AC10 (C2c) — apply's scoped commit is pathspec-limited to the mutated
# artifact and never sweeps a peer session's own staged files. The red test
# below is the concrete shape this plan names: a dirty shared index with two
# sibling sessions' files staged survives an `apply` run completely unswept.
# ---------------------------------------------------------------------------

class TestScopedCommitDoesNotSweepSharedIndex:
    def test_apply_commits_only_the_mutated_artifact_and_leaves_peer_staged_files_untouched(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        # Two sibling sessions' own in-flight edits, staged but NOT committed
        # — exactly the shared-index shape this plan's body calls out.
        peer_a = repo / "state" / "handoffs" / "peer-a.md"
        peer_a.write_text("peer a session's own in-flight edit\n", encoding="utf-8")
        _git(repo, "add", "state/handoffs/peer-a.md")

        peer_b = repo / "cross-repo" / "inbox" / "peer-b.md"
        peer_b.parent.mkdir(parents=True, exist_ok=True)
        peer_b.write_text("peer b session's own in-flight edit\n", encoding="utf-8")
        _git(repo, "add", "cross-repo/inbox/peer-b.md")

        staged_before = set(_git(repo, "diff", "--cached", "--name-only").stdout.splitlines())
        assert staged_before == {"state/handoffs/peer-a.md", "cross-repo/inbox/peer-b.md"}

        exit_code, report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-scoped-commit", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["commit_sha"]

        # The peers' staged files are exactly as they were: still staged,
        # content untouched — `apply`'s commit never widened its pathspec.
        staged_after = set(_git(repo, "diff", "--cached", "--name-only").stdout.splitlines())
        assert staged_after == {"state/handoffs/peer-a.md", "cross-repo/inbox/peer-b.md"}
        assert peer_a.read_text(encoding="utf-8") == "peer a session's own in-flight edit\n"
        assert peer_b.read_text(encoding="utf-8") == "peer b session's own in-flight edit\n"

        committed_files = _git(
            repo, "show", "--stat", "--format=", report["commit_sha"]
        ).stdout
        assert "state/handoffs/h1.md" in committed_files
        assert "peer-a.md" not in committed_files
        assert "peer-b.md" not in committed_files

    def test_scoped_commit_is_a_no_op_when_the_artifact_path_has_no_staged_change(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

        sha = pa_apply._scoped_commit(repo, "state/handoffs/h1.md", "handoff", "h1.md", ["d1"])

        assert sha is None
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before

    def test_scoped_commit_rejects_an_out_of_repo_artifact_path(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        with pytest.raises(pa_apply.OutOfRepoPath):
            pa_apply._scoped_commit(repo, "../../etc/passwd", "handoff", "passwd", [])


# ---------------------------------------------------------------------------
# C2d — `drop` round-trips a granted claim cleanly: back to `open` +
# `ready_to_fire`, claim record wiped, as if the pickup never happened.
# ---------------------------------------------------------------------------

class TestDropRoundTrip:
    def test_drop_round_trips_a_granted_claim_cleanly(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        sid = "sid-drop-roundtrip"

        exit_code, _report = pa_apply.apply("state/handoffs/h1.md", session_id=sid, repo_root=repo)
        assert exit_code == pa_apply.APPLY_EXIT_OK

        claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
        assert claim_dir.is_dir()

        handoff_path = repo / "state" / "handoffs" / "h1.md"
        claimed_text = handoff_path.read_text(encoding="utf-8")
        assert "status: claimed" in claimed_text
        assert "deployment_state: in_flight" in claimed_text

        exit_code, report = pa_apply.drop("state/handoffs/h1.md", session_id=sid, repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["released"] is True
        assert report["unclaimed"] is True
        assert not claim_dir.is_dir()

        dropped_text = handoff_path.read_text(encoding="utf-8")
        assert "status: open" in dropped_text
        assert "deployment_state: ready_to_fire" in dropped_text
        assert "claimed_by" not in dropped_text

    def test_a_second_drop_on_an_already_dropped_artifact_is_a_clean_no_op(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_ready_to_fire(repo, "h1.md")
        sid = "sid-drop-idempotent"

        exit_code, report = pa_apply.drop("state/handoffs/h1.md", session_id=sid, repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["released"] is True
        assert report["unclaimed"] is True
        assert report["commit_sha"] is None


# ---------------------------------------------------------------------------
# C2d / AC9g — `drop` on a half-applied (partial-mutation) artifact: the
# claim primitive landed, the handoff-transition primitive never ran, and
# `drop` composes both of its own primitives unconditionally rather than
# inspecting which of THAT prior run's directives landed.
# ---------------------------------------------------------------------------

class TestDropOnHalfAppliedArtifact:
    def test_drop_fully_reverts_a_claim_only_half_applied_artifact(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        handoff_path = _seed_handoff_ready_to_fire(repo, "h1.md")
        sid = "sid-drop-half-applied"

        # Simulates d1 (session-claim-cli claim-artifact) having landed while
        # d2 (archive-stamp-cli claim-handoff) never ran — the handoff
        # frontmatter is untouched at its original open+ready_to_fire, but a
        # claim dir already exists, recorded under THIS session.
        _write_foreign_fresh_claim(repo, "handoff", "h1.md", foreign_sid=sid)
        claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
        assert claim_dir.is_dir()
        original_text = handoff_path.read_text(encoding="utf-8")

        exit_code, report = pa_apply.drop("state/handoffs/h1.md", session_id=sid, repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["released"] is True
        # cs_unclaim_handoff's own idempotency: already at open+ready_to_fire
        # is a no-op success, not a failure, on the never-claimed half.
        assert report["unclaimed"] is True
        assert not claim_dir.is_dir()
        # Never touched — there was nothing for the unclaim no-op to change.
        assert handoff_path.read_text(encoding="utf-8") == original_text
        assert report["commit_sha"] is None


# ---------------------------------------------------------------------------
# C2d / the Director of Engineering review F6 — disposition round-trip from the session-scoped
# decision-object file: `apply` reads dispositions from that file rather
# than requiring `--decisions` be recalled and retyped, which survives only
# as the explicit crash-resume/audit override.
# ---------------------------------------------------------------------------

class TestDispositionRoundTripFromFile:
    def test_apply_derives_its_decisions_map_from_the_session_scoped_file(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        sid = "sid-disposition-1"
        artifact_path = "state/handoffs/h1.md"

        decision_file = pa_apply._session_decision_file_path(repo, sid, artifact_path)
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.write_text(
            json.dumps(
                {
                    "judgment_points": [
                        {"id": "jcc", "disposition": "proceed"},
                        {"id": "j2"},  # no disposition filled in yet — excluded
                    ]
                }
            ),
            encoding="utf-8",
        )

        from coordinator_core.pickup_assemble import BriefResult, EXIT_OK as _BRIEF_OK

        captured: dict = {}

        def _fake_brief(artifact_path_arg, decisions=None, repo_root=None):
            captured["decisions"] = decisions
            return BriefResult(
                {
                    "artifact": {"path": artifact_path_arg, "classification": "handoff"},
                    # A real (non-terminal) handoff brief with a supplied
                    # decision returns a directive resolved by it — an
                    # empty directives+judgment_points pair here (with
                    # non-empty decisions) would now trip apply()'s
                    # 450add37 terminal-artifact fail-loud guard, which
                    # this test isn't exercising. `already_satisfied`
                    # keeps the stand-in a no-op: it lands without
                    # dispatching a real `cli` handler.
                    "directives": [{"id": "d1", "cli": "session-claim-cli", "already_satisfied": True}],
                    "judgment_points": [],
                },
                _BRIEF_OK,
            )

        monkeypatch.setattr(pa_apply, "brief", _fake_brief)

        exit_code, _report = pa_apply.apply(artifact_path, session_id=sid, repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert captured["decisions"] == {"jcc": {"disposition": "proceed"}}

    def test_an_explicit_decisions_argument_wins_over_the_session_file(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        sid = "sid-disposition-2"
        artifact_path = "state/handoffs/h1.md"

        decision_file = pa_apply._session_decision_file_path(repo, sid, artifact_path)
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.write_text(
            json.dumps({"judgment_points": [{"id": "jcc", "disposition": "from-file"}]}),
            encoding="utf-8",
        )

        from coordinator_core.pickup_assemble import BriefResult, EXIT_OK as _BRIEF_OK

        captured: dict = {}

        def _fake_brief(artifact_path_arg, decisions=None, repo_root=None):
            captured["decisions"] = decisions
            return BriefResult(
                {
                    "artifact": {"path": artifact_path_arg, "classification": "handoff"},
                    # A real (non-terminal) handoff brief with a supplied
                    # decision returns a directive resolved by it — an
                    # empty directives+judgment_points pair here (with
                    # non-empty decisions) would now trip apply()'s
                    # 450add37 terminal-artifact fail-loud guard, which
                    # this test isn't exercising. `already_satisfied`
                    # keeps the stand-in a no-op: it lands without
                    # dispatching a real `cli` handler.
                    "directives": [{"id": "d1", "cli": "session-claim-cli", "already_satisfied": True}],
                    "judgment_points": [],
                },
                _BRIEF_OK,
            )

        monkeypatch.setattr(pa_apply, "brief", _fake_brief)

        explicit_decisions = {"jcc": {"disposition": "from-explicit-flag"}}
        exit_code, _report = pa_apply.apply(
            artifact_path, session_id=sid, repo_root=repo, decisions=explicit_decisions
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert captured["decisions"] == explicit_decisions

    def test_an_absent_decision_file_degrades_to_an_empty_dispositions_map(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        from coordinator_core.pickup_assemble import BriefResult, EXIT_OK as _BRIEF_OK

        captured: dict = {}

        def _fake_brief(artifact_path_arg, decisions=None, repo_root=None):
            captured["decisions"] = decisions
            return BriefResult(
                {
                    "artifact": {"path": artifact_path_arg, "classification": "handoff"},
                    "directives": [],
                    "judgment_points": [],
                },
                _BRIEF_OK,
            )

        monkeypatch.setattr(pa_apply, "brief", _fake_brief)

        exit_code, _report = pa_apply.apply(
            "state/handoffs/h1.md", session_id="sid-disposition-3", repo_root=repo
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert captured["decisions"] == {}

    def test_a_malformed_decision_file_degrades_to_an_empty_dispositions_map_rather_than_raising(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sid = "sid-disposition-4"
        artifact_path = "state/handoffs/h1.md"

        decision_file = pa_apply._session_decision_file_path(repo, sid, artifact_path)
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.write_text("{not valid json", encoding="utf-8")

        assert pa_apply._read_session_dispositions(repo, sid, artifact_path) == {}


# ---------------------------------------------------------------------------
# C2d / AC9(a) — `drop`'s own explicit-session-id-under-two-live-sessions red
# test: mirrors `apply`'s propagation hazard for the mutating primitives
# `drop` composes. An ambient sentinel naming a DIFFERENT live session must
# never be what `release_artifact`'s holder-identity check resolves against.
# ---------------------------------------------------------------------------

class TestDropExplicitSessionIdUnderTwoLiveSessions:
    def test_drop_releases_under_the_explicit_session_id_never_the_ambient_sentinel(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_ready_to_fire(repo, "h1.md")

        my_sid = "sid-drop-explicit"
        other_live_sid = "sid-other-live-peer"
        _register_live_session(repo, my_sid)
        _register_live_session(repo, other_live_sid)

        # The claim is recorded under MY session — the id `drop` must act
        # under via the explicit `--session-id`.
        _write_foreign_fresh_claim(repo, "handoff", "h1.md", foreign_sid=my_sid)
        claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
        assert claim_dir.is_dir()

        # Ambient env ambiguously names the OTHER live session. If `drop`
        # ever fell through to ambient resolution instead of the explicit
        # argument, `release_artifact`'s holder-identity check would compare
        # against the wrong sid and leave a claim it in fact holds stranded.
        monkeypatch.setenv("COORDINATOR_SESSION_ID", other_live_sid)
        monkeypatch.setenv("CLAUDE_SESSION_ID", other_live_sid)

        exit_code, report = pa_apply.drop("state/handoffs/h1.md", session_id=my_sid, repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["released"] is True
        assert not claim_dir.is_dir()

    def test_drop_with_no_resolvable_session_id_fails_transport_not_ambient(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_ready_to_fire(repo, "h1.md")

        exit_code, report = pa_apply.drop("state/handoffs/h1.md", repo_root=repo)

        assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "ambient tier-4" in report["error"]


# ---------------------------------------------------------------------------
# `main_apply` `--decisions` shape validation — mirrors the malformed-JSON
# usage-error path (same exit code, same stderr channel) for well-formed
# JSON carrying the wrong VALUE shape (bare string / list / null instead of
# `{"disposition": <value>}`). Regression for the silent-no-op defect: a
# bare-string decision used to be silently ignored, leaving the judgment
# point unresolved with no error.
# ---------------------------------------------------------------------------

class TestMainApplyDecisionsShapeValidation:
    def test_bare_string_decision_value_is_usage_error(self, capsys):
        rc = pa_apply.main_apply(
            ["state/handoffs/h1.md", "--decisions", '{"j1": "proceed"}']
        )
        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "j1" in err
        assert '{"j1": {"disposition": "<value>"' in err

    def test_list_decision_value_is_usage_error(self, capsys):
        rc = pa_apply.main_apply(
            ["state/handoffs/h1.md", "--decisions", '{"j1": ["proceed"]}']
        )
        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "j1" in err

    def test_null_decision_value_is_usage_error(self, capsys):
        rc = pa_apply.main_apply(
            ["state/handoffs/h1.md", "--decisions", '{"j1": null}']
        )
        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "j1" in err

    def test_valid_shaped_decisions_reaches_apply(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)

        captured = {}

        def _fake_apply(artifact_path, *, session_id=None, repo_root=None, decisions=None):
            captured["decisions"] = decisions
            return pa_apply.APPLY_EXIT_OK, {"landed": []}

        monkeypatch.setattr(pa_apply, "apply", _fake_apply)

        rc = pa_apply.main_apply(
            ["state/handoffs/h1.md", "--decisions", '{"j1": {"disposition": "proceed"}}']
        )

        assert rc == pa_apply.APPLY_EXIT_OK
        assert captured["decisions"] == {"j1": {"disposition": "proceed"}}

    def test_disposition_with_actioned_note_reaches_apply(self, tmp_path, monkeypatch):
        """2026-07-25 defect fix: `--decisions` used to reject any key
        beyond `disposition`, so an `ack-nil`-shaped `actioned_note` (the
        documented happy path for the most common `fyi` disposition) could
        never reach `apply` through the CLI at all."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)

        captured = {}

        def _fake_apply(artifact_path, *, session_id=None, repo_root=None, decisions=None):
            captured["decisions"] = decisions
            return pa_apply.APPLY_EXIT_OK, {"landed": []}

        monkeypatch.setattr(pa_apply, "apply", _fake_apply)

        rc = pa_apply.main_apply(
            [
                "state/handoffs/h1.md",
                "--decisions",
                '{"j-kind": {"disposition": "ack-nil", "actioned_note": "no impact here"}}',
            ]
        )

        assert rc == pa_apply.APPLY_EXIT_OK
        assert captured["decisions"] == {
            "j-kind": {"disposition": "ack-nil", "actioned_note": "no impact here"}
        }

    def test_disposition_alone_still_validates(self, capsys, tmp_path, monkeypatch):
        """Schema-level: an omitted note is not a shape error — the
        fail-loud for a missing note stays downstream, in `cs_action_memo`
        itself, not in `--decisions` shape validation."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(pa_apply, "apply", lambda *a, **k: (pa_apply.APPLY_EXIT_OK, {"landed": []}))

        rc = pa_apply.main_apply(
            ["state/handoffs/h1.md", "--decisions", '{"j-kind": {"disposition": "ack-nil"}}']
        )

        assert rc == pa_apply.APPLY_EXIT_OK
        assert capsys.readouterr().err == ""

    def test_unknown_content_key_is_still_usage_error(self, capsys):
        rc = pa_apply.main_apply(
            [
                "state/handoffs/h1.md",
                "--decisions",
                '{"j-kind": {"disposition": "ack-nil", "not_a_real_field": "x"}}',
            ]
        )
        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "not_a_real_field" in err
        assert "j-kind" in err


# ---------------------------------------------------------------------------
# `apply`/`drop` reject a multi-artifact argument (2026-08-11 defect,
# same harm class as `brief`'s bullet-list silent drop): neither CLI arm ever
# called `split_artifact_args`, so an ` AND `-joined or bulleted argument fell
# straight into `resolve_artifact` as ONE literal path — silently mutating
# (claiming/committing/releasing) whichever ONE artifact the hard-line-wrap
# sanitize fallback happened to match, with no indication a second artifact
# was ever named. `brief` itself keeps its existing array-fan-out (a hard
# cross-repo consumer contract) — only `apply`/`drop`, which mutate and never
# had a multi-artifact contract, are made to refuse.
# ---------------------------------------------------------------------------

class TestMainApplyRejectsMultiArtifact:
    def test_and_joined_argument_fails_loud_not_first_wins(self, capsys):
        rc = pa_apply.main_apply(["state/handoffs/h1.md AND cross-repo/inbox/m1.md"])

        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "2 artifacts" in err
        assert "state/handoffs/h1.md" in err
        assert "cross-repo/inbox/m1.md" in err

    def test_bulleted_argument_fails_loud_naming_every_path(self, capsys):
        raw = "- state/handoffs/h1.md\n  - cross-repo/inbox/m1.md"
        rc = pa_apply.main_apply([raw])

        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "2 artifacts" in err
        assert "state/handoffs/h1.md" in err
        assert "cross-repo/inbox/m1.md" in err

    def test_single_path_is_unaffected(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(
            pa_apply, "apply", lambda *a, **k: (pa_apply.APPLY_EXIT_OK, {"landed": []})
        )

        rc = pa_apply.main_apply(["state/handoffs/h1.md"])

        assert rc == pa_apply.APPLY_EXIT_OK


class TestMainDropRejectsMultiArtifact:
    def test_and_joined_argument_fails_loud_not_first_wins(self, capsys):
        rc = pa_apply.main_drop(["state/handoffs/h1.md AND cross-repo/inbox/m1.md"])

        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "2 artifacts" in err
        assert "state/handoffs/h1.md" in err
        assert "cross-repo/inbox/m1.md" in err

    def test_bulleted_argument_fails_loud_naming_every_path(self, capsys):
        raw = "- state/handoffs/h1.md\n  - cross-repo/inbox/m1.md"
        rc = pa_apply.main_drop([raw])

        assert rc == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "2 artifacts" in err
        assert "state/handoffs/h1.md" in err
        assert "cross-repo/inbox/m1.md" in err

    def test_single_path_is_unaffected(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)
        monkeypatch.setattr(
            pa_apply, "drop", lambda *a, **k: (pa_apply.APPLY_EXIT_OK, {"released": True})
        )

        rc = pa_apply.main_drop(["state/handoffs/h1.md"])

        assert rc == pa_apply.APPLY_EXIT_OK


# ---------------------------------------------------------------------------
# Same-session claim re-entry (2026-07-25 live incident) — `apply` is
# explicitly re-runnable (module docstring § "the hold-path residue"), but a
# memo's `d1` (`session-claim-cli claim-artifact memo`) was unconditionally
# `already_satisfied: False`, so re-invoking `apply` in the SAME session
# after `d1` had already landed called `claims.claim_artifact` a second time,
# which REJECTS a same-session reclaim for the memo class by design
# (`session.claims` module negative-spec) and raised, hard-failing `d1` and
# halting the whole run — even though every OTHER directive gated behind it
# was otherwise ready. `pickup_assemble._claim_already_self_held` (consulted
# from the memo branch of `brief()`) closes this by marking `d1`
# `already_satisfied` when THIS session already holds the lock, mirroring the
# handoff branch's own long-standing `claim["holder"] is not None` shape.
# A DIFFERENT session's holder — live or dead-but-settling — must still
# block exactly as it does today; that path is untouched by the fix.
# ---------------------------------------------------------------------------

def _seed_memo_fyi(repo: Path, name: str) -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        "status: open\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


class TestCliDecisionsCarriesActionMemoNoteEndToEnd:
    """Full CLI-level reproduction of the reported defect: `pickup-assemble
    apply <memo> --decisions '{"j-kind": {"disposition": "ack-nil"}}'`
    (no note) fails loud downstream at `cs_action_memo`'s own precondition;
    the same call WITH `actioned_note` must actually land the memo as
    `status: actioned` — proving the note travels all the way from the
    `--decisions` JSON string through `validate_decisions_shape` into
    `d-action-memo`'s argv, not merely that the shape validator accepts it."""

    def test_actioned_note_via_cli_decisions_json_lands_the_memo(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        memo_path = _seed_memo_fyi(repo, "m1.md")
        sid = "sid-cli-decisions-note"
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

        rc = pa_apply.main_apply(
            [
                "cross-repo/inbox/m1.md",
                "--session-id",
                sid,
                "--decisions",
                '{"j-kind": {"disposition": "ack-nil", "actioned_note": "no impact here"}}',
            ]
        )

        assert rc == pa_apply.APPLY_EXIT_OK
        final_text = memo_path.read_text(encoding="utf-8")
        assert "status: actioned" in final_text
        assert "actioned_note" in final_text

    def test_omitted_note_via_cli_decisions_json_fails_loud_downstream_not_at_shape(
        self, tmp_path, monkeypatch
    ):
        """The exact reported repro: disposition alone, no note. Shape
        validation now lets it through (disposition is the only required
        key) — the fail-loud correctly surfaces from `cs_action_memo`'s own
        precondition, not from `--decisions` usage validation."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo_fyi(repo, "m1.md")
        sid = "sid-cli-decisions-no-note"
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

        rc = pa_apply.main_apply(
            [
                "cross-repo/inbox/m1.md",
                "--session-id",
                sid,
                "--decisions",
                '{"j-kind": {"disposition": "ack-nil"}}',
            ]
        )

        # `--decisions` shape validation passes (disposition alone is
        # sufficient shape) -- the failure surfaces downstream, from
        # `cs_action_memo`'s own required-content precondition, as a
        # partial mutation (d1/claim-memo-stamp landed, d-action-memo did
        # not), never a transport failure.
        assert rc == pa_apply.APPLY_EXIT_PARTIAL_MUTATION


class TestMemoClaimSameSessionReentry:
    def test_same_session_reapply_reports_d1_already_satisfied_and_gated_directives_proceed(
        self, tmp_path
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo_fyi(repo, "m1.md")
        sid = "sid-memo-reentry"
        artifact_path = "cross-repo/inbox/m1.md"

        # First apply: no j-kind disposition supplied yet -- d1 and
        # claim-memo-stamp land (grab mechanics, unconditional), the run
        # halts at the still-open j-kind judgment point (exactly the C7
        # Part B per-directive halt semantics, unrelated to this fix).
        first_exit, first_report = pa_apply.apply(artifact_path, session_id=sid, repo_root=repo)
        assert first_exit == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert first_report["landed"] == ["d1", "claim-memo-stamp"]

        # Second apply, SAME session, j-kind now resolved -- pre-fix this
        # raised "session-claim-cli claim-artifact memo m1.md: claim failed"
        # on d1 and returned APPLY_EXIT_PARTIAL_MUTATION with landed: [].
        second_exit, second_report = pa_apply.apply(
            artifact_path,
            session_id=sid,
            repo_root=repo,
            decisions={"j-kind": {"disposition": "ack-nil", "actioned_note": "no impact here"}},
        )

        assert second_exit == pa_apply.APPLY_EXIT_OK
        assert second_report["landed"] == ["d1", "claim-memo-stamp", "d-action-memo"]
        result_by_id = {r["id"]: r for r in second_report["results"]}
        assert result_by_id["d1"]["already_satisfied"] is True
        assert result_by_id["d1"]["detail"] is None
        # claim-memo-stamp is NOT already_satisfied at the directive level --
        # it relies on cs_claim_memo_stamp's own no-op-on-self-reclaim
        # behaviour (build_memo_directives' docstring), so its handler still
        # dispatches (and no-ops) every re-apply.
        assert result_by_id["claim-memo-stamp"]["already_satisfied"] is False
        assert result_by_id["d-action-memo"]["already_satisfied"] is False

    def test_different_session_live_holder_still_blocks_denied_unchanged(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo_fyi(repo, "m1.md")

        foreign_sid = "sid-foreign-live"
        _register_live_session(repo, foreign_sid)
        _write_foreign_fresh_claim(repo, "memo", "m1.md", foreign_sid)

        my_sid = "sid-mine"
        artifact_path = "cross-repo/inbox/m1.md"

        exit_code, report = pa_apply.apply(artifact_path, session_id=my_sid, repo_root=repo)

        # Unchanged: the pre-loop blanket claim_grant gate denies before any
        # directive (including d1) ever dispatches -- a live, non-lineage-
        # related different-session holder has always blocked here and still
        # does. `reason` is asserted on SHAPE (names the foreign holder, does
        # not claim self-holdership), not on an exact string: `9bc811a0`
        # (C19/held_by_self) widened `compute_claim_grant`'s live-holder
        # reason to fold in `_holder_evidence`'s activity-recency finding, so
        # the exact wording is now evidence-dependent (e.g. "no recent file
        # activity found ... may be a stale claim" is itself part of the
        # live-and-contended verdict, not a sign the block loosened) --
        # pinning to the old fixed string was already stale the moment that
        # landed. `held_by_self` (the field that change introduced) is the
        # precise machine-readable assertion this test exists to make: this
        # is contention by a DIFFERENT session, never mistaken for
        # self-reentry.
        assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
        assert report["landed"] == []
        claim_grant = report["claim_grant"]
        assert claim_grant["verdict"] == "denied"
        assert claim_grant["holder"] == foreign_sid
        assert claim_grant["holder_live"] is True
        assert claim_grant["held_by_self"] is False
        assert foreign_sid in claim_grant["reason"]
        assert "you already hold this" not in claim_grant["reason"]

        # The predicate this fix introduces must never widen to a
        # different session's holder, live or dead -- it only ever answers
        # "do I (the caller) hold this claim right now."
        from coordinator_core.pickup_assemble import _claim_already_self_held

        with pa_apply._session_identity(my_sid):
            assert _claim_already_self_held(repo, "memo", "m1.md") is False

    def test_full_two_phase_flow_partial_then_remaining_decisions_lands_terminal_directive(
        self, tmp_path
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        memo_path = _seed_memo_fyi(repo, "m1.md")
        sid = "sid-memo-two-phase"
        artifact_path = "cross-repo/inbox/m1.md"

        # Phase 1 -- partial decisions (none yet): grab mechanics land, the
        # memo's own frontmatter flips open -> in_progress, terminal write
        # stays gated on j-kind.
        phase1_exit, phase1_report = pa_apply.apply(artifact_path, session_id=sid, repo_root=repo)
        assert phase1_exit == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert phase1_report["landed"] == ["d1", "claim-memo-stamp"]
        mid_text = memo_path.read_text(encoding="utf-8")
        assert "status: in_progress" in mid_text
        assert f"picked_up_by: {sid}" in mid_text
        assert "status: actioned" not in mid_text

        # Phase 2 -- remaining judgment points resolved, SAME session,
        # re-invoked exactly as the operator does after the auto-fire hook
        # writes the recomputed decision object back.
        phase2_exit, phase2_report = pa_apply.apply(
            artifact_path,
            session_id=sid,
            repo_root=repo,
            decisions={"j-kind": {"disposition": "ack-nil", "actioned_note": "no impact here"}},
        )
        assert phase2_exit == pa_apply.APPLY_EXIT_OK
        assert phase2_report["landed"] == ["d1", "claim-memo-stamp", "d-action-memo"]

        final_text = memo_path.read_text(encoding="utf-8")
        assert "status: actioned" in final_text
        assert "actioned_note" in final_text


# ---------------------------------------------------------------------------
# Piece A (cross-repo/inbox/2026-08-04-example-market-data-repo-em-pickup-jgate-
# cleared-strands-gate-fields.md) — `jgate: cleared` now records via a new
# `gate-recheck` directive, sequenced strictly before `claim-handoff`.
# ---------------------------------------------------------------------------

def _seed_awaiting_gate_handoff(repo: Path, name: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: awaiting_gate\n"
        'gate_dependency: "peer campaign must settle first"\n'
        f"{extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


class TestGateRecheckVerbDispatch:
    """`archive-stamp-cli gate-recheck` — the new verb (`_dispatch_archive_
    stamp_cli`), unit-level: directive shape, RuntimeError-with-error
    propagation, path scoping."""

    def test_unrecognized_argument_count_raises(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        with pytest.raises(pa_apply.UnrecognizedDirective):
            pa_apply._dispatch_archive_stamp_cli(["gate-recheck", "state/handoffs/h1.md"], repo)

    def test_cleared_gate_recheck_lands_ready_to_fire(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_awaiting_gate_handoff(repo, "g1.md")

        result = pa_apply._dispatch_archive_stamp_cli(
            ["gate-recheck", "state/handoffs/g1.md", "2026-02-01"], repo
        )
        assert result == {
            "cli": "archive-stamp-cli",
            "verb": "gate-recheck",
            "handoff_path": "state/handoffs/g1.md",
        }
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text
        assert "gate_dependency:" not in text
        assert "last_gate_recheck: 2026-02-01" in text

    def test_refusal_on_unresolved_gate_evidence_raises_runtime_error(self, tmp_path):
        # A `kind: human` leg is "always indeterminate — permanent, by
        # construction" (reconcile/gate_eval.py) — never reduces to
        # `"freed"`, so `_gate_recheck`'s act-time re-verification refuses
        # the write (MutateAbort, no write) and this handler surfaces that
        # as a clean directive failure, not a silent pass.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_awaiting_gate_handoff(
            repo,
            "g2.md",
            extra=(
                "gate_evidence:\n"
                "  covers_prose: true\n"
                "  legs:\n"
                "    - leg_id: leg-1\n"
                "      kind: human\n"
            ),
        )

        # Review: staff-eng finding 6 — pin that the raised message is useful,
        # not merely that a RuntimeError happened.
        with pytest.raises(RuntimeError, match="gate_evidence.*not 'freed'"):
            pa_apply._dispatch_archive_stamp_cli(
                ["gate-recheck", "state/handoffs/g2.md", "2026-02-01"], repo
            )

        text = (repo / "state" / "handoffs" / "g2.md").read_text(encoding="utf-8")
        assert "deployment_state: awaiting_gate" in text


class TestGateRecheckOrderingBeforeClaim:
    """Piece A's load-bearing ordering property, exercised end-to-end
    through `brief()` + `apply()`: `d-gate-recheck` must land strictly
    before `d2` (claim-handoff) — `_gate_recheck` `MutateAbort`s outside
    `awaiting_gate`, so a wrong order would fail loud rather than silently
    reorder."""

    def test_cleared_disposition_lands_gate_recheck_then_claim_in_order(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_awaiting_gate_handoff(repo, "g1.md")

        exit_code, report = pa_apply.apply(
            "state/handoffs/g1.md",
            session_id="sid-gate-cleared",
            repo_root=repo,
            decisions={"jgate": {"disposition": "cleared"}},
        )

        assert exit_code == pa_apply.APPLY_EXIT_OK
        assert report["landed"] == ["d1", "d-gate-recheck", "d2"]
        text = hp.read_text(encoding="utf-8")
        # d2 (claim-handoff) landed last, so the final on-disk state is the
        # claim's, not gate-recheck's intermediate ready_to_fire.
        assert "deployment_state: in_flight" in text
        assert "status: claimed" in text
        assert "last_gate_recheck:" in text

    def test_not_cleared_disposition_leaves_both_directives_unfired(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_awaiting_gate_handoff(repo, "g2.md")

        exit_code, report = pa_apply.apply(
            "state/handoffs/g2.md",
            session_id="sid-gate-not-cleared",
            repo_root=repo,
            decisions={"jgate": {"disposition": "not-cleared"}},
        )

        assert exit_code == pa_apply.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == ["d1"]
        assert "d-gate-recheck" not in report["landed"]
        assert "d2" not in report["landed"]
        assert "jgate" in report["unresolved_judgment_points"]
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: awaiting_gate" in text

    # Review: coordinator:code-reviewer — `build_gate_recheck_directive`'s
    # docstring asserts both drop-recovery arms are safe (idempotent no-op)
    # after gate-recheck lands but `d2` (claim-handoff) then fails. Neither
    # arm was exercised anywhere in this diff; these two tests force that
    # exact partial-mutation sequence (gate-recheck succeeds, claim-handoff
    # raises) and then drive each recovery path against it.

    def test_drop_after_gate_recheck_partial_mutation_is_idempotent_no_op(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_awaiting_gate_handoff(repo, "g4.md")

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated claim-handoff failure")

        monkeypatch.setattr(pa_apply, "cs_claim_handoff", _boom)

        exit_code, report = pa_apply.apply(
            "state/handoffs/g4.md",
            session_id="sid-gate-partial-drop",
            repo_root=repo,
            decisions={"jgate": {"disposition": "cleared"}},
        )

        assert exit_code == pa_apply.APPLY_EXIT_PARTIAL_MUTATION
        assert report["landed"] == ["d1", "d-gate-recheck"]
        assert report["failed_directive"] == "d2"
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text

        drop_exit_code, drop_report = pa_apply.drop(
            "state/handoffs/g4.md",
            session_id="sid-gate-partial-drop",
            repo_root=repo,
        )

        assert drop_exit_code == pa_apply.APPLY_EXIT_OK
        assert drop_report["unclaimed"] is True
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: open" in text or "status: open" in text

    def test_rerun_of_apply_after_gate_recheck_partial_mutation_is_byte_identical(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_awaiting_gate_handoff(repo, "g5.md")

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated claim-handoff failure")

        monkeypatch.setattr(pa_apply, "cs_claim_handoff", _boom)

        exit_code, report = pa_apply.apply(
            "state/handoffs/g5.md",
            session_id="sid-gate-partial-rerun",
            repo_root=repo,
            decisions={"jgate": {"disposition": "cleared"}},
        )
        assert exit_code == pa_apply.APPLY_EXIT_PARTIAL_MUTATION
        assert report["landed"] == ["d1", "d-gate-recheck"]
        text_after_first = hp.read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text_after_first

        monkeypatch.undo()

        rerun_exit_code, rerun_report = pa_apply.apply(
            "state/handoffs/g5.md",
            session_id="sid-gate-partial-rerun",
            repo_root=repo,
            decisions={"jgate": {"disposition": "cleared"}},
        )

        assert rerun_exit_code == pa_apply.APPLY_EXIT_OK
        # Review: coordinator:code-reviewer / review-integrator — the
        # docstring's claim is that a re-run hits `_gate_recheck`'s own
        # already-`ready_to_fire` no-op arm. That is NOT what happens: by
        # the second `apply`, `deployment_state` is already `ready_to_fire`
        # (not `awaiting_gate`), so `brief()` never emits the `jgate`
        # judgment point or `d-gate-recheck` directive at all on this run —
        # `d-gate-recheck` is absent from `landed`, not re-landed
        # byte-identically. The end state still recovers correctly (`d2`
        # claims directly from `ready_to_fire`), but via a different
        # mechanism than the docstring describes. Escalated, not silently
        # reconciled — see review-integrator's completion report.
        assert rerun_report["landed"] == ["d1", "d2"]
        assert "d-gate-recheck" not in rerun_report["landed"]
        text_after_rerun = hp.read_text(encoding="utf-8")
        assert "deployment_state: in_flight" in text_after_rerun
        assert "status: claimed" in text_after_rerun

    def test_unresolved_gate_evidence_halts_the_claim_via_partial_mutation(self, tmp_path):
        # `jgate: cleared` is answered, but live gate_evidence re-resolution
        # refuses the recheck — `d2` must never fire on a refused recheck.
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_awaiting_gate_handoff(
            repo,
            "g3.md",
            extra=(
                "gate_evidence:\n"
                "  covers_prose: true\n"
                "  legs:\n"
                "    - leg_id: leg-1\n"
                "      kind: human\n"
            ),
        )

        exit_code, report = pa_apply.apply(
            "state/handoffs/g3.md",
            session_id="sid-gate-evidence-refused",
            repo_root=repo,
            decisions={"jgate": {"disposition": "cleared"}},
        )

        assert exit_code == pa_apply.APPLY_EXIT_PARTIAL_MUTATION
        assert report["landed"] == ["d1"]
        assert "d2" not in report["landed"]
        # Review: staff-eng finding 2 regression surface — the refusal reason
        # must reach the report, not just stderr.
        assert report["failed_directive"] == "d-gate-recheck"
        assert "gate_evidence" in report["error"]
        assert "not 'freed'" in report["error"]
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: awaiting_gate" in text
