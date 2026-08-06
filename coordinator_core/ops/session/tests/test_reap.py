"""
coordinator_core.ops.session.tests.test_reap

Tests for the session.reap op (Class-B cadence-gated reaper).

Class B means the substrate is untracked .git/coordinator-sessions/; NO git commit
assertions are made here.  The test matrix verifies directory-level mutations
(session/agent dirs moved to .archive/, claim dirs rm'd) under the Class-B safety
contract.

Import guard: coordinator_core.ops.session.reap MUST be imported at module load time
to fire the @register_op("session.reap") side-effect and populate _REGISTRY.
Without this import the decorator has not run and any completeness test passes
vacuously over an empty registry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) stale-session-reaped — session with last_activity > 24h and not in live_sids
                              → moved to .archive/<sid>-YYYY-MM-DD/
  (b) live-session-preserved — session present in live_sids (from resolve_live_session_ids)
                               → NOT moved; directory still present
  (c) toctou-claim-reread   — first cs_claim_holder_live returns False (dead),
                              second returns True (concurrent pickup) → claim NOT reaped
  (d) cadence-gate-skip     — .last-reap marker is recent (< 12h) → cadence_gated=True,
                              no sub-reap runs, all dirs preserved
  (e) fail-closed-to-keep   — resolve_live_session_ids raises an exception → ALL
                              sessions deferred (kept), exit_code:0, deferred[] non-empty
  (f) idempotent-replay     — archive dest already exists → re-run skips gracefully
                              (per-record idempotency), exit_code:0, nothing new moved
  (g) stale-agent-reaped    — agent dir with touched.txt mtime > 24h → moved to
                              .archive/_agents-<aid>-YYYYMMDD/
  (h) partial-failure       — claim rm fails (shutil.rmtree raises + dir still exists)
                              → exit_code:2, failed[] non-empty
  (i) claim-liveness-guard  — cs_claim_holder_live returns True on first check →
                              claim NOT touched (kept)

Spec backlinks:
  - Plan C4/C8: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C4
  - AC3: per-record idempotency; recency liveness; TOCTOU re-read; fail-closed-to-keep
  - AC6: import-to-register positive floor + test matrix per pcore-11 pattern
  - session-state-contract.md § Negative-Spec: .last-reap is mtime-persistence (not flock)

Negative-spec:
  - Does NOT assert git status clean — Class B makes no git commits (no tracked files move).
  - Does NOT test DR-211 git bounds (D3/D4 scoped pathspec, private index, archive_and_commit)
    — those apply only to Class-A fleet ops.
  - Does NOT patch raw pid calls (kill -0 / psutil) — the op itself forbids them (anti-scope).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.session.reap  # noqa: F401 — fires @register_op("session.reap")

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.session.reap import _handler

# Positive floor assertion: the handler must be registered before any test runs.
# This assertion fires at import time — a vacuously-green completeness check
# on an empty registry is the failure mode this guards against.
_OP_NAME = "session.reap"
_session_registered_ops = [k for k in _REGISTRY if k.startswith("session.")]
assert len(_session_registered_ops) >= 1, (
    "import guard failed: no session.* ops in _REGISTRY — "
    "check coordinator_core.ops.session.reap import and @register_op side-effect"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.reap @register_op('session.reap') did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# Patch targets — name-bound in reap.py at import time.
_LIVE_SIDS_PATCH = "coordinator_core.ops.session.reap.resolve_live_session_ids"
_CLAIM_LIVE_PATCH = "coordinator_core.ops.session.reap.cs_claim_holder_live"
_SHUTIL_RMTREE_PATCH = "coordinator_core.ops.session.reap.shutil.rmtree"


def _call(session_repo, *, force: bool = True) -> dict:
    """Invoke _handler with force=True (bypass cadence gate) unless told otherwise.

    Using force=True in most tests keeps them cadence-independent — the cadence
    gate is tested explicitly in test_cadence_gate_skip.
    """
    return _run(
        _handler({"force": force}, repo_root=session_repo.common_dir)
    )


# ---------------------------------------------------------------------------
# (a) stale-session-reaped
# ---------------------------------------------------------------------------


def test_stale_session_reaped(session_repo):
    """Session with last_activity > 24h and not in live_sids is moved to .archive/."""
    session_repo.seed_session("sid-stale-01", hours_ago=48)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _call(session_repo)

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert "sid-stale-01" in result["reaped_sessions"], (
        "stale session (48h inactive) must appear in reaped_sessions"
    )
    assert not (session_repo.sessions_dir / "sid-stale-01").exists(), (
        "stale session dir must be removed from coordinator-sessions/"
    )
    assert session_repo.archive_dest_exists("sid-stale-01"), (
        "stale session must be moved to .archive/<sid>-YYYY-MM-DD/"
    )


def test_recent_session_preserved(session_repo):
    """Session with last_activity < 24h is kept regardless of live_sids status."""
    session_repo.seed_session("sid-recent-01", hours_ago=2)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _call(session_repo)

    assert result["exit_code"] == 0
    assert "sid-recent-01" not in result["reaped_sessions"], (
        "recently-active session (2h) must NOT be reaped"
    )
    assert (session_repo.sessions_dir / "sid-recent-01").exists(), (
        "recent session dir must still exist"
    )


# ---------------------------------------------------------------------------
# (b) live-session-preserved
# ---------------------------------------------------------------------------


def test_live_session_preserved(session_repo):
    """Session ID present in live_sids is kept even if last_activity is old."""
    session_repo.seed_session("sid-live-01", hours_ago=48)

    # resolve_live_session_ids returns this session as live — must NOT be reaped.
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset({"sid-live-01"})):
        result = _call(session_repo)

    assert "sid-live-01" not in result["reaped_sessions"], (
        "session in live_sids must NOT be reaped even if last_activity is old"
    )
    assert (session_repo.sessions_dir / "sid-live-01").exists(), (
        "live session dir must still exist"
    )


def test_mixed_stale_and_live(session_repo):
    """Stale sessions are reaped; live sessions are kept in the same run."""
    session_repo.seed_session("sid-stale-mix", hours_ago=48)
    session_repo.seed_session("sid-live-mix", hours_ago=48)

    # Only sid-live-mix is live.
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset({"sid-live-mix"})):
        result = _call(session_repo)

    assert "sid-stale-mix" in result["reaped_sessions"]
    assert "sid-live-mix" not in result["reaped_sessions"]
    assert not (session_repo.sessions_dir / "sid-stale-mix").exists()
    assert (session_repo.sessions_dir / "sid-live-mix").exists()


# ---------------------------------------------------------------------------
# (c) TOCTOU re-read honored (holder goes live between first and second check)
# ---------------------------------------------------------------------------


def test_toctou_claim_reread_honored(session_repo):
    """TOCTOU: first liveness check dead, second check live → claim NOT reaped.

    Mirrors shell cs_reap_stale_claims :855/:861 double-check discipline.
    A concurrent session pickup that happens between the two calls must be
    honoured — the claim must be kept (fail-closed-to-keep semantics).
    """
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-toctou-01")

    # side_effect list: first call → False (dead); second call → True (concurrent pickup).
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, side_effect=[False, True]):
            result = _call(session_repo)

    assert claim_dir.exists(), (
        "claim dir must NOT be removed when TOCTOU second check returns live"
    )
    claim_id = "handoff-claims/hclaim-toctou-01"
    assert claim_id not in result["reaped_claims"], (
        "TOCTOU-detected live claim must not appear in reaped_claims"
    )


# ---------------------------------------------------------------------------
# (d) cadence-gate-skip
# ---------------------------------------------------------------------------


def test_cadence_gate_skip(session_repo):
    """12h cadence gate: when .last-reap is recent, all sub-reaps are skipped.

    The gate fires when the marker mtime is less than 12h ago.  force=False
    lets the gate logic run; .last-reap touched 'now' (0h ago) → gate fires.
    """
    session_repo.seed_session("sid-cadence-01", hours_ago=48)
    # Simulate a very recent last-reap (gate should fire).
    session_repo.touch_last_reap(hours_ago=0.0)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _run(_handler({"force": False}, repo_root=session_repo.common_dir))

    assert result["cadence_gated"] is True, (
        "cadence gate must fire when .last-reap is recent (< 12h ago)"
    )
    assert result["reaped_sessions"] == [], "cadence-gated run must not reap sessions"
    assert result["reaped_agents"] == [], "cadence-gated run must not reap agents"
    assert result["reaped_claims"] == [], "cadence-gated run must not reap claims"
    # Session dir must still exist — nothing was moved.
    assert (session_repo.sessions_dir / "sid-cadence-01").exists()


def test_cadence_gate_elapsed_allows_reap(session_repo):
    """When .last-reap is > 12h ago, the gate does not fire and reaping proceeds."""
    session_repo.seed_session("sid-cadence-elapsed", hours_ago=48)
    # Set last-reap to 13h ago — beyond the 12h gate threshold.
    session_repo.touch_last_reap(hours_ago=13.0)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _run(_handler({"force": False}, repo_root=session_repo.common_dir))

    assert result["cadence_gated"] is False, (
        "gate must not fire when .last-reap is 13h ago (> 12h threshold)"
    )
    assert "sid-cadence-elapsed" in result["reaped_sessions"]


# ---------------------------------------------------------------------------
# (e) fail-closed-to-keep — liveness resolution error
# ---------------------------------------------------------------------------


def test_fail_closed_on_liveness_error(session_repo):
    """When resolve_live_session_ids raises, ALL sessions are deferred (kept).

    Untracked-substrate removal is irreversible; fail-closed-to-keep is mandatory
    when liveness is ambiguous (mirrors shell L392 "bias to life").
    """
    session_repo.seed_session("sid-failclosed-01", hours_ago=48)
    session_repo.seed_session("sid-failclosed-02", hours_ago=72)

    with patch(_LIVE_SIDS_PATCH, side_effect=RuntimeError("bash bridge failed")):
        result = _call(session_repo)

    # exit_code must be 0 — deferral is not a failure, it is a keep-decision.
    assert result["exit_code"] == 0, (
        "liveness error → deferred (not failed); exit_code must be 0"
    )
    assert result["reaped_sessions"] == [], (
        "no sessions must be reaped when liveness resolution fails"
    )
    # Both sessions must appear in deferred.
    deferred_ids = {d["id"] for d in result["deferred"]}
    assert "sid-failclosed-01" in deferred_ids, (
        "sid-failclosed-01 must be in deferred[] after liveness error"
    )
    assert "sid-failclosed-02" in deferred_ids, (
        "sid-failclosed-02 must be in deferred[] after liveness error"
    )
    # Session dirs must still exist.
    assert (session_repo.sessions_dir / "sid-failclosed-01").exists()
    assert (session_repo.sessions_dir / "sid-failclosed-02").exists()


def test_fail_closed_claim_liveness_error(session_repo):
    """When cs_claim_holder_live raises on the FIRST check, claim is deferred (not reaped)."""
    claim_dir = session_repo.seed_claim("memo-claims", "mc-err-01")

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, side_effect=OSError("claim liveness bridge error")):
            result = _call(session_repo)

    assert claim_dir.exists(), (
        "claim must NOT be removed when cs_claim_holder_live raises"
    )
    claim_id = "memo-claims/mc-err-01"
    assert claim_id not in result["reaped_claims"]
    deferred_ids = {d["id"] for d in result["deferred"]}
    assert claim_id in deferred_ids, (
        "claim must appear in deferred[] when liveness check raises"
    )


# ---------------------------------------------------------------------------
# (f) idempotent-replay — archive dest already exists
# ---------------------------------------------------------------------------


def test_idempotent_replay_session(session_repo):
    """Per-record idempotency: re-run after stale session is archived → skipped, exit_code:0.

    When the .archive dest already exists the session is already archived;
    the second run must skip silently (no second move attempted, no error).
    Class B: no git-commit assertion (there is no git commit for this substrate).
    """
    session_repo.seed_session("sid-idem-01", hours_ago=48)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        first = _call(session_repo)

    assert first["exit_code"] == 0
    assert "sid-idem-01" in first["reaped_sessions"]
    assert session_repo.archive_dest_exists("sid-idem-01")

    # Re-run: session dir is gone; archive dest exists.
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        second = _call(session_repo)

    assert second["exit_code"] == 0, (
        "idempotent replay must return exit_code:0 (already-archived is not a failure)"
    )
    assert "sid-idem-01" not in second["reaped_sessions"], (
        "already-archived session must not appear in reaped_sessions on replay"
    )
    # Archive dest must still be there — not double-moved.
    assert session_repo.archive_dest_exists("sid-idem-01")


# ---------------------------------------------------------------------------
# (g) stale-agent-reaped
# ---------------------------------------------------------------------------


def test_stale_agent_reaped(session_repo):
    """Agent dir with touched.txt mtime > 24h is moved to .archive/_agents-<aid>-YYYYMMDD/."""
    session_repo.seed_agent("agent-stale-01", hours_ago=48)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _call(session_repo)

    assert result["exit_code"] == 0
    assert "agent-stale-01" in result["reaped_agents"], (
        "stale agent (48h mtime) must appear in reaped_agents"
    )
    assert not (session_repo.sessions_dir / ".agents" / "agent-stale-01").exists(), (
        "stale agent dir must be removed from .agents/"
    )
    assert session_repo.agent_archive_exists("agent-stale-01"), (
        "stale agent must be moved to .archive/_agents-<aid>-YYYYMMDD/"
    )


def test_recent_agent_preserved(session_repo):
    """Agent dir with touched.txt mtime < 24h is NOT reaped."""
    session_repo.seed_agent("agent-recent-01", hours_ago=2)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _call(session_repo)

    assert "agent-recent-01" not in result["reaped_agents"], (
        "recently-active agent (2h mtime) must NOT be reaped"
    )
    assert (session_repo.sessions_dir / ".agents" / "agent-recent-01").exists()


# ---------------------------------------------------------------------------
# (h) partial-failure — exit_code:2
# ---------------------------------------------------------------------------


def test_partial_failure_claim_rm_exit_code_2(session_repo):
    """When claim rm fails AND claim dir still exists: exit_code:2, failed[] non-empty.

    shutil.rmtree is mocked to raise OSError, simulating a permission/lock failure.
    The claim dir is still present after the mock raises (it was never actually removed),
    so the reaper records the failure in failed[] → exit_code:2.
    """
    claim_dir = session_repo.seed_claim("plan-claims", "pc-fail-01")

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, return_value=False):
            with patch(_SHUTIL_RMTREE_PATCH, side_effect=OSError("permission denied")):
                result = _call(session_repo)

    assert result["exit_code"] == 2, (
        "exit_code must be 2 (DETERMINATE-PARTIAL) when a rm fails and dir still exists"
    )
    assert len(result["failed"]) >= 1, "failed[] must be non-empty on partial failure"
    failed_ids = {f["id"] for f in result["failed"]}
    assert "plan-claims/pc-fail-01" in failed_ids, (
        "failed claim must appear in failed[] with its subdir/name id"
    )
    # Dir must still be present (rm was mocked to fail).
    assert claim_dir.exists()


# ---------------------------------------------------------------------------
# (i) claim-liveness-guard — live holder on first check
# ---------------------------------------------------------------------------


def test_claim_liveness_guard_first_check(session_repo):
    """Live claim (cs_claim_holder_live=True on first check) is NOT touched."""
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-live-01")

    # Both checks return True — the reaper must skip on the first check result.
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, return_value=True):
            result = _call(session_repo)

    assert claim_dir.exists(), "live claim must not be removed"
    claim_id = "handoff-claims/hclaim-live-01"
    assert claim_id not in result["reaped_claims"]
    assert claim_id not in {f["id"] for f in result["failed"]}


# ---------------------------------------------------------------------------
# (j) cadence-decoupled claim reap — sub-reap (iii) runs on the fast path,
#     .last-reap only advances on a full run.
# ---------------------------------------------------------------------------


def test_fast_path_reaps_claims_without_advancing_last_reap(session_repo):
    """Fast path (cadence NOT elapsed, force=False): sub-reap (iii) still runs,
    but .last-reap mtime is NOT advanced (Decision D1 shape (a) — C4).

    Repeated fast-path runs must leave .last-reap untouched while still rm'ing
    orphaned claim dirs — otherwise the 12h gate for sub-reaps (i)/(ii) would
    silently reset on every boot and stale sessions/agents would never reap.
    """
    marker = session_repo.touch_last_reap(hours_ago=1.0)  # cadence NOT elapsed (< 12h)
    mtime_before = marker.stat().st_mtime
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-fastpath-01")

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, return_value=False):
            result = _run(_handler({"force": False}, repo_root=session_repo.common_dir))

    assert result["cadence_gated"] is True, (
        "fast path: (i)/(ii) skipped due to cadence gate"
    )
    claim_id = "handoff-claims/hclaim-fastpath-01"
    assert claim_id in result["reaped_claims"], (
        "sub-reap (iii) must run on the fast path even though (i)/(ii) are gated"
    )
    assert not claim_dir.exists(), "orphaned claim must be rm'd on the fast path"
    assert marker.stat().st_mtime == mtime_before, (
        ".last-reap mtime must NOT advance on the fast (iii)-only path"
    )

    # A second fast-path run must behave identically — .last-reap still untouched.
    claim_dir_2 = session_repo.seed_claim("handoff-claims", "hclaim-fastpath-02")
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, return_value=False):
            result2 = _run(_handler({"force": False}, repo_root=session_repo.common_dir))

    assert "handoff-claims/hclaim-fastpath-02" in result2["reaped_claims"]
    assert not claim_dir_2.exists()
    assert marker.stat().st_mtime == mtime_before, (
        "repeated fast-path runs must not advance .last-reap mtime"
    )


def test_full_run_advances_last_reap(session_repo):
    """A full run (force=True or cadence elapsed) DOES advance .last-reap mtime."""
    marker = session_repo.touch_last_reap(hours_ago=1.0)
    mtime_before = marker.stat().st_mtime

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _run(_handler({"force": True}, repo_root=session_repo.common_dir))

    assert result["cadence_gated"] is False
    assert marker.stat().st_mtime > mtime_before, (
        "a full run (force=True) must advance the .last-reap marker"
    )


def test_orphaned_claim_reaped(session_repo):
    """Claim with dead holder (both liveness checks return False) is rm'd."""
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-dead-01")

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, return_value=False):
            result = _call(session_repo)

    claim_id = "handoff-claims/hclaim-dead-01"
    assert claim_id in result["reaped_claims"], (
        "orphaned claim (both liveness checks False) must appear in reaped_claims"
    )
    assert not claim_dir.exists(), "orphaned claim dir must be removed"


def test_orphaned_handoff_claim_reconciles_frontmatter(session_repo):
    """Reap/ship bug fix 1 (g4-execute-pipeline-two-repo-rebuild.md § M3a): the
    automated reaper shares clear_claim_if_dead's frontmatter-drift bug — the
    handoff's own status:claimed -> open flip must ALSO happen here, not just
    the lock-dir rm."""
    session_repo.seed_claim("handoff-claims", "h1.md")
    hpath = session_repo.root / "state" / "handoffs" / "h1.md"
    hpath.parent.mkdir(parents=True, exist_ok=True)
    hpath.write_text(
        "---\n"
        "title: t\n"
        "created: 2026-07-24\n"
        "branch: work/t/2026-07-24\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: test fixture handoff\n"
        "status: claimed\n"
        "deployment_state: in_flight\n"
        "claimed_by: holder-sid\n"
        "claimed_at: 2000-01-01T00:00:00Z\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        with patch(_CLAIM_LIVE_PATCH, return_value=False):
            result = _call(session_repo)

    assert "handoff-claims/h1.md" in result["reaped_claims"]
    text = hpath.read_text(encoding="utf-8")
    assert "status: open" in text
    assert "claimed_by" not in text


# ---------------------------------------------------------------------------
# (k) C5 — per-repo claim-reap PRIMITIVE + session-touched-baton-repos default
#
# Spec backlink: docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md
# § C5. Op scope "none" (fleet-generic, explicit target_root/target_roots param,
# NOT common_dir) — session.reap_claims_for_repos.
# ---------------------------------------------------------------------------

import coordinator_core.ops.session.reap as _reap_mod  # noqa: E402 — see import guard note below

_OP_NAME_REPOS = "session.reap_claims_for_repos"
assert _OP_NAME_REPOS in _REGISTRY, (
    f"import guard failed: {_OP_NAME_REPOS!r} not in _REGISTRY; "
    "coordinator_core.ops.session.reap @register_op('session.reap_claims_for_repos') did not fire"
)


def _call_reap_claims_for_repos(target_roots) -> dict:
    """Invoke the session.reap_claims_for_repos handler with a list of target roots."""
    return _run(
        _reap_mod._handler_reap_claims_for_repos({"target_roots": target_roots})
    )


def test_reap_claims_for_target_primitive_reaps_dead_holder(session_repo):
    """The per-repo primitive reaps a dead-holder claim dir under an explicit target_root."""
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-repo-dead-01")

    with patch(_CLAIM_LIVE_PATCH, return_value=False):
        reaped, deferred, failed = _reap_mod._reap_claims_for_target(session_repo.root)

    claim_id = "handoff-claims/hclaim-repo-dead-01"
    assert claim_id in reaped, "dead-holder claim under target_root must be reaped"
    assert not claim_dir.exists()
    assert failed == []


def test_reap_claims_for_target_primitive_preserves_live_holder(session_repo):
    """A live holder's claim under target_root is NEVER reaped by the primitive."""
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-repo-live-01")

    with patch(_CLAIM_LIVE_PATCH, return_value=True):
        reaped, deferred, failed = _reap_mod._reap_claims_for_target(session_repo.root)

    claim_id = "handoff-claims/hclaim-repo-live-01"
    assert claim_id not in reaped, "live holder's claim must never be reaped"
    assert claim_dir.exists()


def test_reap_claims_for_target_no_sessions_dir_is_noop(tmp_path):
    """A target_root with no coordinator-sessions substrate yet returns ([], [], []) — not an error."""
    import subprocess

    repo_root = tmp_path / "bare-repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_root), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "x@test"], cwd=str(repo_root), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "X"], cwd=str(repo_root), capture_output=True, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(repo_root), capture_output=True, check=True)
    (repo_root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "chore: init"], cwd=str(repo_root), capture_output=True, check=True)

    reaped, deferred, failed = _reap_mod._reap_claims_for_target(repo_root)

    assert (reaped, deferred, failed) == ([], [], [])


def test_session_touched_baton_repos_default_reaps_only_touched_repos(session_repo, tmp_path):
    """session-touched-baton-repos default: reaps exactly the touched repos, no others.

    Two separate repos each get a dead-holder claim; only ONE is named in
    target_roots (simulating a session that touched only that repo's
    BATON_REPO via a foreign /pickup). Only the named repo's claim is reaped —
    the untouched sibling repo's claim must be left alone (claude-klabauter does NOT
    enumerate working-repos.yaml / walk into repos this session never touched).
    """
    import subprocess

    touched_claim = session_repo.seed_claim("handoff-claims", "hclaim-touched-01")

    untouched_root = tmp_path / "untouched-repo"
    untouched_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(untouched_root), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "x@test"], cwd=str(untouched_root), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "X"], cwd=str(untouched_root), capture_output=True, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(untouched_root), capture_output=True, check=True)
    (untouched_root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(untouched_root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "chore: init"], cwd=str(untouched_root), capture_output=True, check=True)

    untouched_claims_dir = untouched_root / ".git" / "coordinator-sessions" / "handoff-claims" / "hclaim-untouched-01"
    untouched_claims_dir.mkdir(parents=True)
    (untouched_claims_dir / "claim.json").write_text("{}", encoding="utf-8")

    with patch(_CLAIM_LIVE_PATCH, return_value=False):
        per_repo = _reap_mod._reap_session_touched_repos([session_repo.root])

    assert str(session_repo.root) in per_repo
    assert "handoff-claims/hclaim-touched-01" in per_repo[str(session_repo.root)]["reaped_claims"]
    assert not touched_claim.exists()

    # The untouched repo was never named — its claim must be left untouched,
    # and it must not even appear in the per-repo results dict.
    assert str(untouched_root) not in per_repo
    assert untouched_claims_dir.exists(), (
        "an untouched sibling repo's claim dir must NOT be reaped by the session-scoped default"
    )


def test_reap_claims_for_repos_op_handler_end_to_end(session_repo):
    """session.reap_claims_for_repos op handler: end-to-end via target_roots param."""
    claim_dir = session_repo.seed_claim("memo-claims", "mclaim-op-dead-01")

    with patch(_CLAIM_LIVE_PATCH, return_value=False):
        result = _call_reap_claims_for_repos([str(session_repo.root)])

    assert result["exit_code"] == 0
    repo_key = str(session_repo.root)
    assert repo_key in result["results"]
    assert "memo-claims/mclaim-op-dead-01" in result["results"][repo_key]["reaped_claims"]
    assert not claim_dir.exists()
    assert result["errors"] == []


def test_reap_claims_for_repos_op_handler_rejects_empty_target_roots():
    """Missing/empty target_roots → exit_code:1 setup error, not a silent no-op."""
    result = _call_reap_claims_for_repos([])

    assert result["exit_code"] == 1
    assert result["results"] == {}
    assert len(result["errors"]) == 1


# Review: code-reviewer F2 (2026-07-14 slice2) — no prior test exercised the
# errors[] shape for a rejected target_root entry. Covers the F1 fix directly:
# a blank/whitespace-only string and a non-existent path must both land in
# errors[] (never silently dropped, never raise), and a valid sibling entry in
# the same call is still processed normally.
def test_reap_claims_for_repos_rejects_blank_and_nonexistent_target_roots(session_repo):
    """A blank/whitespace target_root and a non-resolvable target_root both land
    in errors[]; a valid sibling target_root in the same call is unaffected."""
    claim_dir = session_repo.seed_claim("handoff-claims", "hclaim-mixed-batch-01")
    nonexistent_root = str(session_repo.root.parent / "does-not-exist-anywhere")

    with patch(_CLAIM_LIVE_PATCH, return_value=False):
        result = _call_reap_claims_for_repos(
            ["   ", nonexistent_root, str(session_repo.root)]
        )

    assert result["exit_code"] == 2, f"a per-entry error must yield exit_code:2; {result!r}"
    assert len(result["errors"]) == 2, f"both bad entries must be reported; got {result['errors']!r}"
    error_targets = {e["target_root"] for e in result["errors"]}
    assert "   " in error_targets
    assert nonexistent_root in error_targets

    repo_key = str(session_repo.root)
    assert repo_key in result["results"], "the valid sibling target_root must still be processed"
    assert "handoff-claims/hclaim-mixed-batch-01" in result["results"][repo_key]["reaped_claims"]
    assert not claim_dir.exists()
