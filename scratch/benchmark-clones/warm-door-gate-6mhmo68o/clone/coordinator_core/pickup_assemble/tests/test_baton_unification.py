"""
coordinator_core.pickup_assemble.tests.test_baton_unification

Purpose: proves C5 (docs/plans/2026-08-19-batons-unify-into-one-successor.md
§ C5, "route a second pickup into baton_assemble's multi-leg path, behind a
predicate" — shipped off, flipped ON at `c09345b56`) — THE ACTION HALF. `compute_baton_unification_verdict`
(C4) is consumed, never re-derived; this suite exercises the routing seam
(`route_baton_adoption`), the mint dispatch (`_unify_into_successor`), and
crash-resume (`_resume_pending_unification`) that sit on top of it.

`baton_assemble.apply` itself is MOCKED at its own module attribute
(`coordinator_core.baton_assemble.apply.apply`) — C5 imports it at CALL time
(circular-import precedent set by C4's own `compute_baton_unification_
verdict`), so patching the source attribute is picked up on every call. The
mock performs the same shape of write the real pipeline is responsible for
(mint a successor carrying `additional_predecessors`, stamp every parent
`deployment_state: continued` + `continued_into` in one pass) so this suite
can assert C5's OWN contract — routing, claim-ledger bookkeeping, and
crash-resume — without re-proving `baton_assemble`'s own d1/d6/d6* pipeline,
which is that module's own test surface.

Coverage:
  - predicate OFF is a TRUE no-op — no verdict computed, no mint, no claim
    move, no parent stamped; behaviour is byte-identical to pre-C5
  - sequential pickup (hold A, pick up B) unifies with the predicate ON
  - simultaneous `/pickup x y` (degraded ordering) unifies with the
    predicate ON
  - a `refuse` verdict writes NOTHING at all — not even the advisory append
  - unification failure surfaces (raises) rather than being swallowed the
    way `_adopt_into_baton`'s advisory append is
  - kill-and-resume at every inter-write boundary in step 3 (release
    parents' claims, then claim the successor), INCLUDING kill immediately
    after step 2 (parents already stamped) — each asserts the resume
    reaches the SAME successor rather than minting a second one (AC8)

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_baton_unification.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.baton_assemble.apply as ba_apply
import coordinator_core.pickup_assemble as pa
from coordinator_core.session import claims as claims_mod

# Spawns a real external git process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Minimal, self-contained git harness — same shape as the sibling
# test_baton_unification_decision.py (deliberately not cross-imported; see
# that file's own header comment for the convention this mirrors).
# ---------------------------------------------------------------------------


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
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


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    deployment_state: str | None = "active",
    baton_role: str | None = "work",
    scope: list[str] | None = None,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
    )
    if deployment_state is not None:
        fm += f"deployment_state: {deployment_state}\n"
    if baton_role is not None:
        fm += f"baton_role: {baton_role}\n"
    if scope:
        fm += "scope:\n" + "".join(f"  - {p}\n" for p in scope)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _make_ledger_claim(repo: Path, basename: str, holder_sid: str) -> Path:
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    return claim_dir


@pytest.fixture(autouse=True)
def _mock_ledger_liveness(monkeypatch):
    import coordinator_core.claim_state as claim_state_mod

    monkeypatch.setattr(claim_state_mod, "cs_claim_holder_live", lambda *a, **k: True)


SELF_SID = "sid-self"


@pytest.fixture(autouse=True)
def _as_self_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", SELF_SID)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        pa._liveness, "live_session_verdicts", lambda root: {SELF_SID: (True, "meta")}
    )


TARGET_FM: dict = {"predecessor": "none", "scope": ["coordinator_core/target.py"]}
TARGET_PATH = "state/handoffs/target.md"

SUCCESSOR_NAME = "2026-01-02-successor.md"
SUCCESSOR_REL = f"state/handoffs/{SUCCESSOR_NAME}"


def _fake_baton_assemble_apply_factory(repo: Path, call_log: list):
    """Stand-in for `baton_assemble.apply.apply`. Reproduces the SHAPE of
    what the real d1/d6/d6* transaction does — mint a successor carrying
    `additional_predecessors`, stamp every currently-held parent leg
    `continued` + `continued_into` — as ONE call, so C5's own contract
    (everything downstream of a successful mint) is exercised against a
    realistic result without re-running `baton_assemble`'s own pipeline."""

    def _fake(kind, artifact_path, *, session_id=None, repo_root=None, decisions=None, title=None, explicit_deliverable_id=None):
        call_log.append(1)
        assert kind == "handoff"
        assert artifact_path == ""
        root = repo_root
        import coordinator_core.baton_assemble as ba_pkg

        primary, additional, _degraded = ba_pkg._resolve_held_handoff_for_session(
            root, allow_standalone=True
        )
        parents = [p for p in ([primary] + list(additional)) if p]

        successor_path = root / SUCCESSOR_REL
        successor_path.parent.mkdir(parents=True, exist_ok=True)
        additional_fm = "".join(f'  - "{p}"\n' for p in parents[1:])
        successor_text = (
            "---\n"
            f'title: "Unified successor"\n'
            "created: 2026-01-02\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            f'predecessor: "{parents[0]}"\n'
            "additional_predecessors:\n" + additional_fm +
            "deployment_state: active\n"
            "---\n\n# Successor\n\nBody.\n"
        )
        successor_path.write_text(successor_text, encoding="utf-8")
        _git(root, "add", str(successor_path.relative_to(root)))

        for parent_rel in parents:
            parent_path = root / parent_rel
            text = parent_path.read_text(encoding="utf-8")
            text = text.replace(
                "deployment_state: active\n",
                f"deployment_state: continued\ncontinued_into: {SUCCESSOR_REL}\n",
            )
            parent_path.write_text(text, encoding="utf-8")
            _git(root, "add", str(parent_path.relative_to(root)))

        _git(root, "commit", "-m", "fake unify: mint successor, stamp parents")
        return (0, {"landed": ["d1", "d6"], "commit_sha": "deadbeef"})

    return _fake


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    # D-H (AC13): `session_baton.store` requires the per-session directory
    # to already exist and no-ops, observably, when it does not — pre-create
    # it so `_adopt_into_baton`'s advisory append is actually exercised
    # rather than silently declining for an unrelated reason.
    (r / ".git" / "coordinator-sessions" / SELF_SID).mkdir(parents=True, exist_ok=True)
    return r


def _baton_json_path(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions" / SELF_SID / "baton.json"


# ---------------------------------------------------------------------------
# The shipped default
# ---------------------------------------------------------------------------


# The shipped-default assertion lives in
# `test_unification_predicate_default.py`, NOT here: this whole module is
# `spawns_process` + `cadence`, so a check placed here would guard a
# box-wide behaviour default only at cadence gates. See that file's own
# docstring.


# ---------------------------------------------------------------------------
# Predicate OFF — true no-op
# ---------------------------------------------------------------------------


def test_predicate_off_is_true_no_op(repo, monkeypatch):
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: False)
    _seed_handoff(repo, "a.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", SELF_SID)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert called == []
    assert not (repo / SUCCESSOR_REL).exists()
    a_text = (repo / "state" / "handoffs" / "a.md").read_text(encoding="utf-8")
    assert "continued" not in a_text
    # Falls straight through to `_adopt_into_baton` — same advisory
    # append as pre-C5.
    baton = pa.read_baton(SELF_SID, cwd=str(repo))
    assert baton.get("adopted_artifacts") == [TARGET_PATH]


# ---------------------------------------------------------------------------
# Predicate ON — proceed unifies
# ---------------------------------------------------------------------------


def test_sequential_pickup_unifies_with_predicate_on(repo, monkeypatch):
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _seed_handoff(repo, "a.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", SELF_SID)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert len(called) == 1
    assert (repo / SUCCESSOR_REL).exists()
    a_text = (repo / "state" / "handoffs" / "a.md").read_text(encoding="utf-8")
    assert "deployment_state: continued" in a_text
    assert f"continued_into: {SUCCESSOR_REL}" in a_text

    # Step 3 landed: parent's claim released, successor claimed.
    assert not (repo / ".git" / "coordinator-sessions" / "handoff-claims" / "a.md").is_dir()
    successor_claim = (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    )
    assert successor_claim.is_dir()
    assert (successor_claim / "session_id").read_text(encoding="utf-8").strip() == SELF_SID


def test_simultaneous_pickup_degraded_ordering_unifies_with_predicate_on(repo, monkeypatch):
    """`/pickup x y` — two claims landing close enough to tie on every
    ordering leg (`degraded=True`) still unifies; `degraded` is a
    set-ordering signal C4 never treats as an input."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _seed_handoff(repo, "a.md", baton_role="work")
    _seed_handoff(repo, "b.md", baton_role="work")
    claim_a = _make_ledger_claim(repo, "a.md", SELF_SID)
    claim_b = _make_ledger_claim(repo, "b.md", SELF_SID)
    tied_mtime = 1_800_000_000.0
    os.utime(claim_a / "claimed_at", (tied_mtime, tied_mtime))
    os.utime(claim_b / "claimed_at", (tied_mtime, tied_mtime))

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert len(called) == 1
    assert (repo / SUCCESSOR_REL).exists()
    for name in ("a.md", "b.md"):
        text = (repo / "state" / "handoffs" / name).read_text(encoding="utf-8")
        assert "deployment_state: continued" in text
        assert not (
            repo / ".git" / "coordinator-sessions" / "handoff-claims" / name
        ).is_dir()
    successor_claim = (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    )
    assert successor_claim.is_dir()


# ---------------------------------------------------------------------------
# Refuse writes nothing
# ---------------------------------------------------------------------------


def test_refuse_verdict_writes_nothing(repo, monkeypatch):
    """`live-peer`/`live-unrelated` are, by D-F's own construction, NEVER
    reachable as THIS session's `_resolve_held_handoff_for_session` primary
    (that resolver is `list_claims_by_session(self_sid)`-scoped — see the
    plan's own anti-scope note and `test_baton_unification_decision.py`'s
    `test_arm_live_peer_refuses`/`test_arm_live_unrelated_refuses`, which
    exercise `_primary_held_disposition` directly for the same reason).
    A `"refuse"` verdict is therefore stubbed here rather than assembled
    from real held-set resolution — this test is about `route_baton_
    adoption`'s OWN handling of a `"refuse"` outcome, not about
    reproducing how C4 reaches one."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)

    refuse_verdict = {
        "target": TARGET_PATH,
        "verdict": "refuse",
        "reason": "live-peer",
        "held": {"primary": "state/handoffs/held.md", "additional": [], "degraded": False},
        "inheritable": ["state/handoffs/held.md"],
        "disposed_skipped": [],
        "unstamped_skipped": 0,
        "disposition": "live-peer",
        "message": "refusing to absorb a live peer",
    }
    monkeypatch.setattr(
        pa, "compute_baton_unification_verdict", lambda *a, **k: refuse_verdict
    )

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert called == []
    assert not (repo / SUCCESSOR_REL).exists()
    # No advisory append either — a refuse verdict writes NOTHING.
    assert not _baton_json_path(repo).exists()


# ---------------------------------------------------------------------------
# Failure surfaces, not swallowed
# ---------------------------------------------------------------------------


def test_unification_mint_failure_surfaces(repo, monkeypatch):
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _seed_handoff(repo, "a.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", SELF_SID)

    def _failing_apply(kind, artifact_path, **kwargs):
        return (3, {"error": "transport_fail"})  # APPLY_EXIT_TRANSPORT_FAIL

    monkeypatch.setattr(ba_apply, "apply", _failing_apply)

    with pytest.raises(RuntimeError, match="baton unification mint failed"):
        pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    # Nothing was left half-adopted — no advisory append landed either,
    # since the exception propagates out of `_unify_into_successor` before
    # `_adopt_into_baton` would ever be reached.
    assert not _baton_json_path(repo).exists()


# ---------------------------------------------------------------------------
# Kill-and-resume, AC8 — successor IDENTITY, not mere convergence
# ---------------------------------------------------------------------------


def _setup_two_parent_fixture(repo):
    _seed_handoff(repo, "a.md", baton_role="work")
    _seed_handoff(repo, "b.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", SELF_SID)
    _make_ledger_claim(repo, "b.md", SELF_SID)


def test_kill_after_parents_stamped_before_any_claim_release_resumes_same_successor(
    repo, monkeypatch
):
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _setup_two_parent_fixture(repo)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    real_finish = pa._finish_unification_claims

    def _boom(*a, **k):
        raise RuntimeError("simulated crash: step 3 never starts")

    monkeypatch.setattr(pa, "_finish_unification_claims", _boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert len(called) == 1  # mint landed exactly once

    # Restore the REAL step-3 implementation for the resume pass — only the
    # crash stub is reverted; session identity / liveness fixtures stay put.
    # A second mint call on this pass would be the orphan-successor bug
    # (AC8): the routing must RESUME, not re-brief.
    monkeypatch.setattr(pa, "_finish_unification_claims", real_finish)
    called2 = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called2)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert called2 == []  # RESUME path — never re-mints
    for name in ("a.md", "b.md"):
        assert not (
            repo / ".git" / "coordinator-sessions" / "handoff-claims" / name
        ).is_dir()
    successor_claim = (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    )
    assert successor_claim.is_dir()
    # SAME successor as the one the original mint produced — identity, not
    # merely "a successor exists".
    a_text = (repo / "state" / "handoffs" / "a.md").read_text(encoding="utf-8")
    assert f"continued_into: {SUCCESSOR_REL}" in a_text


def test_kill_after_successor_claimed_before_releasing_any_parent_resumes(
    repo, monkeypatch
):
    """`_finish_unification_claims` claims the successor FIRST, then
    releases parents — see that function's own docstring for why the order
    is load-bearing. This exercises the crash point immediately after the
    successor claim lands, before either parent's release has run."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _setup_two_parent_fixture(repo)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    real_release = claims_mod.release_artifact

    def _boom_release(*a, **k):
        raise RuntimeError("simulated crash: before any parent claim is released")

    monkeypatch.setattr(pa._claims, "release_artifact", _boom_release)

    with pytest.raises(RuntimeError, match="simulated crash"):
        pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert len(called) == 1
    successor_claim = (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    )
    assert successor_claim.is_dir()  # the claim DID land before the crash
    for name in ("a.md", "b.md"):
        assert (
            repo / ".git" / "coordinator-sessions" / "handoff-claims" / name
        ).is_dir()  # neither parent released yet

    # Restore the REAL release primitive for the resume pass — only the
    # crash stub is reverted. A second mint call on this pass would be the
    # orphan-successor bug (AC8); a second successor-claim attempt would
    # hit the same-session-reclaim rejection the idempotency guard exists
    # to avoid.
    monkeypatch.setattr(pa._claims, "release_artifact", real_release)

    called2 = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called2)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert called2 == []
    for name in ("a.md", "b.md"):
        assert not (
            repo / ".git" / "coordinator-sessions" / "handoff-claims" / name
        ).is_dir()
    assert successor_claim.is_dir()


def test_kill_after_releasing_one_parent_before_the_other_resumes(repo, monkeypatch):
    """The crash point PAST the point `test_kill_after_all_parents_released_
    before_successor_claimed` would have named under the OLD (release-then-
    claim) ordering: with claim-then-release, releasing every parent is the
    LAST step, so the only remaining partial state is one parent released,
    one not — the ledger still carries the un-released parent's `continued`
    + `continued_into` stamp, which is exactly `_resume_pending_unification`'s
    signal."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _setup_two_parent_fixture(repo)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    real_release = claims_mod.release_artifact
    release_calls = []

    def _release_then_crash(class_, basename, baton_repo_root="", cwd=None):
        release_calls.append(basename)
        if len(release_calls) == 1:
            return real_release(class_, basename, baton_repo_root, cwd=cwd)
        raise RuntimeError("simulated crash: mid claim-release loop")

    monkeypatch.setattr(pa._claims, "release_artifact", _release_then_crash)

    with pytest.raises(RuntimeError, match="simulated crash"):
        pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert len(called) == 1
    successor_claim = (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    )
    assert successor_claim.is_dir()
    released = [n for n in ("a.md", "b.md") if not (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / n
    ).is_dir()]
    assert len(released) == 1  # exactly one parent released before the crash

    # Restore the REAL release primitive for the resume pass — only the
    # crash stub is reverted.
    monkeypatch.setattr(pa._claims, "release_artifact", real_release)

    called2 = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called2)
    )

    pa.route_baton_adoption(repo, TARGET_PATH, dict(TARGET_FM))

    assert called2 == []
    successor_claim = (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    )
    assert successor_claim.is_dir()


# ---------------------------------------------------------------------------
# AC10's mutation half — `unify_run_batons`, the entry point
# `/mise-en-place` reaches this same routed path through. No artifact is
# being picked up on this path, so every assertion below is about the
# RUN-shaped call: one successor per run, the ledger (not the caller's
# leg list) deciding what gets stamped, and a live foreign holder refused.
# ---------------------------------------------------------------------------


def test_resume_refuses_a_dangling_continued_into(repo, monkeypatch):
    """The parent-stamp loop is one directive PER PARENT (`d6`, `d6-2`, …),
    so a failure part way through stamps some parents and not others; that
    run exits partial-mutation and d1's compensator then deletes the
    pristine successor scaffold. What survives is a parent stamped terminal
    pointing at a successor that is gone.

    Acting on that signal is strictly worse than ignoring it:
    `_finish_unification_claims` would claim the phantom basename and then
    RELEASE every real parent claim, costing the session both batons for a
    path nothing can resolve. Resume must refuse, loudly, with the claims
    untouched.
    """
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _seed_handoff(repo, "a.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", SELF_SID)

    # Parent stamped continued into a successor that was never left on disk.
    parent = repo / "state" / "handoffs" / "a.md"
    parent.write_text(
        parent.read_text(encoding="utf-8").replace(
            "deployment_state: active\n",
            "deployment_state: continued\ncontinued_into: state/handoffs/gone.md\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="does not resolve on disk"):
        pa._resume_pending_unification(repo)

    # The real claim survived the refusal; no phantom claim was taken.
    assert (repo / ".git" / "coordinator-sessions" / "handoff-claims" / "a.md").is_dir()
    assert not (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / "gone.md"
    ).is_dir()


def test_run_unification_predicate_off_mutates_nothing(repo, monkeypatch):
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: False)
    _setup_two_parent_fixture(repo)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    report = pa.unify_run_batons(repo, ["state/handoffs/a.md", "state/handoffs/b.md"])

    assert called == []
    assert report["unified"] is False
    assert report["reason"] == "routing-disabled"
    assert not (repo / SUCCESSOR_REL).exists()
    for name in ("a.md", "b.md"):
        text = (repo / "state" / "handoffs" / name).read_text(encoding="utf-8")
        assert "continued" not in text
        assert (
            repo / ".git" / "coordinator-sessions" / "handoff-claims" / name
        ).is_dir()


def test_run_over_several_batons_yields_one_successor_carrying_them_as_legs(
    repo, monkeypatch
):
    """AC10's own "done when": a run over several inheritable batons
    produces ONE successor carrying them as fan-in legs, once per run."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _setup_two_parent_fixture(repo)

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    report = pa.unify_run_batons(repo, ["state/handoffs/a.md", "state/handoffs/b.md"])

    assert len(called) == 1  # once per run, never once per baton
    assert report["unified"] is True
    assert report["successor"] == SUCCESSOR_REL

    successor_text = (repo / SUCCESSOR_REL).read_text(encoding="utf-8")
    assert "additional_predecessors:" in successor_text
    for name in ("a.md", "b.md"):
        assert name in successor_text  # both legs named on the successor
        text = (repo / "state" / "handoffs" / name).read_text(encoding="utf-8")
        assert "deployment_state: continued" in text
        assert f"continued_into: {SUCCESSOR_REL}" in text
        assert not (
            repo / ".git" / "coordinator-sessions" / "handoff-claims" / name
        ).is_dir()
    assert (
        repo / ".git" / "coordinator-sessions" / "handoff-claims" / SUCCESSOR_NAME
    ).is_dir()


def test_run_legs_are_reporting_only_never_the_stamping_authority(repo, monkeypatch):
    """A leg the RUN names but the claim ledger does not hold is carried
    into the report and is NOT stamped — the durable ledger stays the sole
    authority over what unification touches (AC7), so an inventory table
    can never stamp a baton this session does not hold."""
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    _seed_handoff(repo, "a.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", SELF_SID)
    unheld = _seed_handoff(repo, "stranger.md", baton_role="work")

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    report = pa.unify_run_batons(
        repo, ["state/handoffs/a.md", "state/handoffs/stranger.md"]
    )

    assert report["unified"] is True
    assert report["run_legs"] == ["state/handoffs/a.md", "state/handoffs/stranger.md"]
    assert report["parents"] == ["state/handoffs/a.md"]
    assert "continued" not in unheld.read_text(encoding="utf-8")


def test_run_unification_never_touches_a_foreign_held_baton(repo, monkeypatch):
    """A baton held by a LIVE foreign session is not refused on this path —
    it never enters the held set at all, because that set resolves through
    `list_claims_by_session(self_sid)`. The verdict is therefore
    `nothing-held`, and nothing is written. The four-arm refusal stays the
    documented backstop for the pickup path (and for a future resolver
    change); it is not what protects this one, and a reader should not
    infer from a green suite that it fired here.

    The verdict is also computed against an EMPTY target frontmatter, which
    is the safe direction — though not for the obvious reason.
    `_scopes_intersect` counts an empty list on either side as
    INTERSECTING (it cannot prove non-overlap), so a live holder that DID
    reach the disposition would read `live-peer`, not `live-unrelated`.
    Both are refusal arms, so the empty target buys a guaranteed refusal
    rather than a computed one — never a scope-overlap coin-flip.
    """
    monkeypatch.setattr(pa, "_baton_unification_routing_enabled", lambda: True)
    foreign = _seed_handoff(repo, "a.md", baton_role="work")
    _make_ledger_claim(repo, "a.md", "sid-someone-else")
    monkeypatch.setattr(
        pa._liveness,
        "live_session_verdicts",
        lambda root: {SELF_SID: (True, "meta"), "sid-someone-else": (True, "meta")},
    )

    called = []
    monkeypatch.setattr(
        ba_apply, "apply", _fake_baton_assemble_apply_factory(repo, called)
    )

    report = pa.unify_run_batons(repo, ["state/handoffs/a.md"])

    assert called == []
    assert report["unified"] is False
    assert report["reason"] == "nothing-held"
    assert report["parents"] == []
    assert not (repo / SUCCESSOR_REL).exists()
    assert "continued" not in foreign.read_text(encoding="utf-8")
    assert (repo / ".git" / "coordinator-sessions" / "handoff-claims" / "a.md").is_dir()
