"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_ownership

Tests for the C4c sink-side ownership-scope gate wired into
`ceremony.scoped_git_commit`'s `_handler`
(docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md,
AC17/AC18) -- NOT `test_scoped_git_commit.py`, which covers this op's
pre-existing commit-pipeline mechanics and bypasses this gate entirely (see
that module's `_bypass_ownership_gate` fixture).

REVERTED 2026-08-03 (P1 security fix, same-day reversal of a same-day
amendment -- see `coordinator_core.ops.session.scope_report`'s own module
docstring, "REVERTED 2026-08-03", for the full incident and rationale): the
gate is back to composing `coordinator_core.ops.session.scope_report
.assert_paths_in_session_scope` -- the SAME strict allow-list the C4b
guard-side check uses. A separate, deny-list-shaped `assert_paths_not_
foreign_owned` was tried in between and found unsound: its orphan/race-
window pass-through was not scoped to the CALLING session at all, so any
caller -- including a fabricated `session_id` -- could sweep a genuinely
live peer's in-flight path, or a dead session's leftover work, into its own
commit. That predicate has been deleted from `scope_report.py` entirely.
This module's tests were updated in lockstep, back to strict polarity:

  - a path in the caller's own scope still PASSES (no change from either
    polarity).
  - a POSITIVELY foreign-owned path (another live session's touch list
    actually claims it) still REJECTS -- AC18's core property.
  - an unattributable orphan, and the "freshly dispatched sub-agent,
    backpointer not written yet" race window, now REJECT again -- the
    permissive pass-through for both shapes is exactly what reopened the
    hole; the accepted cost is that a caller genuinely racing its own
    agent's backpointer must fall back to an explicit `git add`/
    `git commit` (EM-available, not subagent-available).
  - a caller whose `session_id` is provably unrelated to (or fabricated
    relative to) a race-window agent's path is DENIED -- the regression
    test for the hole
    (`test_fabricated_session_id_cannot_claim_a_live_peers_race_window_path`
    below, the reviewer's Finding-2 test).
  - a path whose OWNING session's claims are unreadable still REJECTS --
    fail-closed is unchanged.
  - a sweeping pathspec (e.g. `.`) still REJECTS regardless of ownership --
    checked independently of the ownership predicate (`_reject_sweeping_
    pathspec`).

AC18 (the point of this module): the C4b guard-side check inspects command
TEXT and structurally cannot hold against an obfuscated payload
(`subprocess.run(['g'+'it','com'+'mit'])` defeats any text matcher). Every
test here calls `scoped_git_commit._handler` DIRECTLY, in-process, the same
way such a payload would reach the op -- bypassing the PreToolUse guard
seam entirely -- to demonstrate the property only the sink can provide.

Uses the REAL `assert_paths_in_session_scope` predicate throughout (no
mocking of the allow/deny decision itself) by seeding real
`.git/coordinator-sessions/<sid>/touched.txt` and
`.git/coordinator-sessions/.agents/<aid>/touched.txt` ownership records in
throwaway repos, so these tests exercise the actual wiring, not a stand-in
for it. `test_scope_report.py` (C4a) already covers the predicate's own
edge cases in isolation; this module covers the sink's USE of it.

All git operations run against a throwaway repo created fresh under
`tmp_path` -- never the working repo.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.ops.ceremony import scoped_git_commit


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _seed_session_scope(repo: Path, session_id: str, owned_paths: list[str]) -> None:
    """Seed a real `.git/coordinator-sessions/<session_id>/touched.txt` claim
    record -- the same on-disk shape `track_touched_files` writes -- so
    `compute_scope`'s Step 1 candidate set (and therefore `compute_offer`'s
    `safe_paths`) genuinely includes `owned_paths` for `session_id`, with no
    mocking of the predicate itself.
    """
    sdir = repo / ".git" / "coordinator-sessions" / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(owned_paths) + ("\n" if owned_paths else ""), encoding="utf-8"
    )


def _seed_agent_touched_no_backpointer(repo: Path, agent_id: str, touched_paths: list[str]) -> None:
    """Seed `.git/coordinator-sessions/.agents/<agent_id>/touched.txt` with
    NO sibling `em-session-id.txt` -- the exact on-disk shape of the live
    EM-commit incident: a dispatched sub-agent's own writes land
    synchronously, while the PostToolUse hook that writes the back-pointer
    to its owning EM session fires only once the whole agent turn returns.
    """
    agent_dir = repo / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "touched.txt").write_text(
        "\n".join(touched_paths) + ("\n" if touched_paths else ""), encoding="utf-8"
    )


def _push_started_at_to_future(repo: Path, session_id: str) -> None:
    """Set the session's `started_at` to an hour in the future so
    `compute_scope`'s Step-2 mtime fallback does NOT pick up a file created
    after this call as a candidate — the non-candidate shape
    `test_include_orphans_true_denies_agent_race_window_path` needs to
    actually exercise Step 5's `agent_race_paths` arm (now
    `ScopeResult.indeterminate`) rather than Step 4's pre-existing
    touched_set arm (see that test's own docstring for why this matters)."""
    sdir = repo / ".git" / "coordinator-sessions" / session_id
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )


def _porcelain(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _call(params: dict) -> dict:
    return asyncio.run(scoped_git_commit._handler(params, repo_root=None))


def test_own_scope_paths_pass_through_unchanged(tmp_path):
    """No regression to the normal commit path: a session committing paths
    genuinely inside its own scope is unaffected by the C4c gate.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")
    _seed_session_scope(repo, "own-session", ["tasks/feature/todo.md"])

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
            "session_id": "own-session",
        }
    )

    assert result["committed"] is True
    assert result["sha"]
    assert "error" not in result


def test_direct_handler_call_rejects_foreign_owned_path(tmp_path):
    """AC18: an obfuscated Bash payload
    (`subprocess.run(['g'+'it','com'+'mit'])`) cannot be caught by the C4b
    guard-side text matcher, but it necessarily still resolves to a direct,
    in-process call of `ceremony.scoped_git_commit`'s `_handler` -- exactly
    what this test does. A path claimed by ANOTHER LIVE session's touched.txt
    is rejected, and nothing is ever staged.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "peer's own work")
    _seed_session_scope(repo, "peer-session", ["notes/alpha.md"])
    _seed_session_scope(repo, "attacker-session", [])  # owns nothing

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "sweep it in",
            "session_id": "attacker-session",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert result["pushed"] is None
    assert "error" in result

    # Nothing was ever staged, nothing committed -- the rejection happens
    # BEFORE `run_commit_pipeline` is ever called.
    assert _head_sha(repo) == head_before
    # git collapses an untracked directory to one porcelain line.
    assert _porcelain(repo) == ["?? notes/"]


def test_direct_handler_call_denies_unattributable_orphan_path(tmp_path):
    """Strict-polarity restoration: a wholly unclaimed (orphan) dirty path --
    nobody's touched.txt names it, and no other session claims it either --
    is DENIED, not allowed. Absence of a foreign claim is NOT evidence of
    ownership; that permissive read is exactly the hole the reverted
    `assert_paths_not_foreign_owned` amendment reopened (see this module's
    own docstring).
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "orphan/nobody.md", "nobody's claim")
    _seed_session_scope(repo, "some-session", [])  # owns nothing

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["orphan/nobody.md"],
            "message": "commit the orphan",
            "session_id": "some-session",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result
    assert _head_sha(repo) == head_before


def test_direct_handler_call_denies_freshly_dispatched_agent_race_window_path(tmp_path):
    """Strict-polarity restoration: a path written by a freshly dispatched
    sub-agent, whose `.agents/<aid>/em-session-id.txt` back-pointer has not
    been written yet, is DENIED to a caller whose OWN session_id has no
    stated relationship to that agent -- the exact shape the reverted
    permissive amendment let through unconditionally (see this module's own
    docstring, and `test_fabricated_session_id_cannot_claim_a_live_peers_
    race_window_path` below for the adversarial-identity variant).
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "state/fresh-agent-output.md", "written by a dispatched agent")
    _seed_agent_touched_no_backpointer(
        repo, "agent-just-dispatched", ["state/fresh-agent-output.md"]
    )

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["state/fresh-agent-output.md"],
            "message": "commit my own agent's fresh output",
            "session_id": "own-session",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result


def test_fabricated_session_id_cannot_claim_a_live_peers_race_window_path(tmp_path):
    """Reviewer Finding 2 -- the regression test for the hole the reverted
    `assert_paths_not_foreign_owned` amendment opened: a `session_id` with NO
    relationship whatsoever to a race-window agent's path (here, one that
    never resolves to any real session directory at all) must be DENIED the
    same as any other caller, never granted a "safe to pass" verdict merely
    because `compute_offer`'s orphan/race-window classification is not
    scoped to who is asking.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "state/fresh-agent-output.md", "written by a dispatched agent")
    _seed_agent_touched_no_backpointer(
        repo, "agent-just-dispatched", ["state/fresh-agent-output.md"]
    )

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["state/fresh-agent-output.md"],
            "message": "sweep it in",
            "session_id": "totally-fabricated-session-id-no-such-session-ever-existed",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result
    assert _head_sha(repo) == head_before
    assert _porcelain(repo) == ["?? state/"]


def test_owning_sessions_unreadable_claims_still_rejects(tmp_path):
    """Fail-closed is unchanged: a genuinely degraded read (another
    session's touched.txt exists but cannot be read) still blocks every
    uncontested candidate this call.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/dirty.md", "content")
    _seed_session_scope(repo, "some-session", ["notes/dirty.md"])

    broken = repo / ".git" / "coordinator-sessions" / "broken-session"
    broken.mkdir(parents=True)
    broken_touched = broken / "touched.txt"
    broken_touched.write_text("whatever\n", encoding="utf-8")
    os.chmod(str(broken_touched), 0o000)
    try:
        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["notes/dirty.md"],
                "message": "sweep it in",
                "session_id": "some-session",
            }
        )
    finally:
        os.chmod(str(broken_touched), 0o644)

    assert result["committed"] is False
    assert "error" in result


def test_sweeping_pathspec_rejects_even_when_nothing_is_foreign_owned(tmp_path):
    """A sweeping pathspec element is rejected on its own terms, independent
    of ownership -- checked BEFORE the ownership gate even runs. Even a
    caller with a genuinely clean bill of ownership (everything it names is
    its own) cannot pass `.` through; a sweeping pathspec matches whatever is
    CURRENTLY on disk at commit time, including a peer's file added after
    this call computed its intent.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "mine.md", "content")
    _seed_session_scope(repo, "own-session", ["mine.md"])

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["."],
            "message": "sweep everything",
            "session_id": "own-session",
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert "sweeping pathspec" in result["error"]
    assert _head_sha(repo) == head_before


def test_unresolvable_identity_rejects(tmp_path, monkeypatch):
    """No explicit `session_id` param, and the ambient session identity
    cannot be resolved either -- fails closed, never falls through to an
    unscoped commit.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "content")

    monkeypatch.setattr(scoped_git_commit.session_core, "resolve_session_id", lambda *a, **k: "")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert "outside session scope" in result["error"] or "unimportable" in result["error"]


def test_unreadable_scope_rejects(tmp_path):
    """`worktree_root` names a directory with no git repo at all -- scope
    resolution fails, so the call is rejected rather than treated as
    "no constraint applies".
    """
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    result = _call(
        {
            "worktree_root": str(not_a_repo),
            "paths": ["foo.md"],
            "message": "add foo",
            "session_id": "some-session",
        }
    )

    assert result["committed"] is False
    assert "error" in result


def test_orphan_path_default_mode_rejects_with_offer(tmp_path):
    """Orphan-aware ownership gate (sizing:
    2026-08-03-ownership-gate-cannot-distinguish-an-orp.yaml): a dirty path
    claimed by no session at all is still rejected by default -- but the
    error is an OFFER, naming the remedy (`include_orphans: true`) rather
    than a bare deny.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "orphan/nobody.md", "nobody's claim")
    _seed_session_scope(repo, "some-session", [])

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["orphan/nobody.md"],
            "message": "commit the orphan",
            "session_id": "some-session",
        }
    )

    assert result["committed"] is False
    assert "include_orphans" in result["error"]


def test_orphan_path_include_orphans_true_denies_without_positive_caller_evidence(tmp_path):
    """Review: staff-eng F3 -- RE-PINNED (was
    `test_orphan_path_include_orphans_true_commits`, which asserted this
    commits and enshrined the wrong contract). `_seed_session_scope` seeds
    only a bare `touched.txt` -- no `meta.json`, the artifact
    `coordinator_core.session.core.init` writes and every session that ever
    went through the real touch-tracked hot path already has. That is NOT
    positive evidence of a real calling session (see
    `assert_paths_in_session_scope`'s own docstring, F2/F3) -- a caller
    shaped exactly like a fabricated `session_id` gets the same strict
    denial `allow_orphans=False` would give it, never a wider one.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "orphan/nobody.md", "nobody's claim")
    _seed_session_scope(repo, "some-session", [])

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["orphan/nobody.md"],
            "message": "commit the orphan",
            "session_id": "some-session",
            "include_orphans": True,
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result
    assert _head_sha(repo) == head_before


def test_orphan_path_include_orphans_true_commits_now_that_r1_is_closed(tmp_path):
    """Inverted per this test's own prior docstring: staff-eng R1
    (2026-08-03) is now closed and `scope_report._ORPHAN_ADOPTION_ENABLED` is
    flipped to `True` (see that constant's current negative spec). A caller
    that clears the F2/F3 positive-evidence check -- a real session
    initialized via `core.init`, not a bare seeded directory -- now commits
    the orphan.

    Asserted at the op boundary rather than only at the helper so a future
    caller cannot reach adoption by routing around `assert_paths_in_session_scope`.
    """
    from coordinator_core.session import core as session_core_module

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    session_core_module.init("real-session", cwd=str(repo))
    _seed_file(repo, "orphan/nobody.md", "nobody's claim")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["orphan/nobody.md"],
            "message": "commit the orphan",
            "session_id": "real-session",
            "include_orphans": True,
        }
    )

    assert result["committed"] is True
    assert result["sha"]
    assert "error" not in result


def test_include_orphans_true_denies_peer_claim_with_unreadable_touched_txt(tmp_path):
    """Review: staff-eng F1 regression test -- a peer's genuinely-claimed,
    in-flight dirty file, with the peer's OWN `touched.txt` unreadable
    (chmod 000), must still deny under `include_orphans: true`. Before the
    fix, this candidate landed in BOTH `result.skipped` (owner "unknown
    (claims unreadable: ...)") AND raw `result.orphans` (Step 5: absent from
    `my_scope`, and `other_owner.get(dfile)` is `None` since the claim was
    never actually resolved, only indeterminate) -- `compute_offer` returned
    `result.orphans` verbatim, so the degraded-read withhold was
    indistinguishable from a genuine orphan and `allow_orphans` admitted it.
    """
    from coordinator_core.session import core as session_core_module

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    session_core_module.init("mine", cwd=str(repo))
    peer_sdir = repo / ".git" / "coordinator-sessions" / "peer-session"
    peer_sdir.mkdir(parents=True)
    peer_touched = peer_sdir / "touched.txt"
    peer_touched.write_text("peer.md\n", encoding="utf-8")
    _seed_file(repo, "peer.md", "peer's own in-flight work")
    os.chmod(str(peer_touched), 0o000)

    head_before = _head_sha(repo)
    try:
        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["peer.md"],
                "message": "sweep it in",
                "session_id": "mine",
                "include_orphans": True,
            }
        )
    finally:
        os.chmod(str(peer_touched), 0o644)

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result
    assert _head_sha(repo) == head_before


def test_include_orphans_true_denies_fabricated_session_id(tmp_path):
    """Review: staff-eng F2 regression test -- a `session_id` that never
    named a real session (never created via `core.init`, no session
    directory ever existed) must deny under `include_orphans: true`, not
    adopt every dirty orphan in the tree. Before the fix,
    `assert_paths_in_session_scope` resolved this identity to an empty
    `safe_paths` and an untouched `orphans` set with no check that the
    identity itself was ever real.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "victim.md", "somebody's dirty file")

    head_before = _head_sha(repo)
    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["victim.md"],
            "message": "sweep it in",
            "session_id": "ghost-session-never-existed",
            "include_orphans": True,
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result
    assert _head_sha(repo) == head_before


def test_include_orphans_true_denies_agent_race_window_path(tmp_path):
    """Review: staff-eng regression test (agent-race non-candidate shape,
    `ScopeResult.indeterminate`) -- a freshly dispatched sub-agent's own
    dirty output, whose `.agents/<aid>/em-session-id.txt` back-pointer has
    not been written yet, must deny under `include_orphans: true` the same
    as default mode -- never adoptable as an orphan merely because this
    call's own `touched_set` never happened to include it.

    Re-review (staff-eng, pass 2, R2): the ORIGINAL fixture here called
    `core.init` and only THEN created the file, so the file's mtime
    postdated `started_at` and `compute_scope`'s Step-2 mtime fallback
    picked it up as a CANDIDATE -- this test actually exercised Step 4's
    pre-existing `agent_race_paths` arm (a candidate already in
    `touched_set`), not the non-candidate shape the docstring describes.
    Fixed here (rather than rewriting the docstring to match the weaker
    test) by pushing `started_at` an hour into the future via
    `_push_started_at_to_future`, so the file's mtime predates it and the
    path never enters `touched_set` at all -- the only way to reach it is
    via `ScopeResult.indeterminate` (agent_race_paths non-empty this call),
    which is the shape this regression exists to pin.
    """
    from coordinator_core.session import core as session_core_module

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    session_core_module.init("own-session", cwd=str(repo))
    _push_started_at_to_future(repo, "own-session")
    _seed_file(repo, "state/fresh-agent-output.md", "written by a dispatched agent")
    _seed_agent_touched_no_backpointer(
        repo, "agent-just-dispatched", ["state/fresh-agent-output.md"]
    )

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["state/fresh-agent-output.md"],
            "message": "commit my own agent's fresh output",
            "session_id": "own-session",
            "include_orphans": True,
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result


def test_peer_claimed_path_still_rejects_with_include_orphans_true(tmp_path):
    """`include_orphans: true` never relaxes the peer-claimed case -- the
    incident guard (62e9a1f73) this gate exists to preserve.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "peer's own work")
    _seed_session_scope(repo, "peer-session", ["notes/alpha.md"])
    _seed_session_scope(repo, "attacker-session", [])

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "sweep it in",
            "session_id": "attacker-session",
            "include_orphans": True,
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert _head_sha(repo) == head_before


def test_mixed_pathspec_include_orphans_true_denies_with_no_offer(tmp_path):
    """A pathspec mixing an orphan and a peer-claimed path, with
    `include_orphans: true`, still denies -- the peer-claimed rejection wins
    and no orphan-adoption OFFER is made (re-invoking with `include_orphans`
    would not resolve the peer-claimed half anyway).

    Post-SC-DR-019 (`assert_paths_in_session_scope` now enumerates every
    denied path, not just the first -- see that function's own docstring):
    the message DOES now name that the orphan half's adoption request was
    ignored (honest per-path enumeration, not an "offer"), so this no longer
    asserts blanket absence of the substring `include_orphans` -- it asserts
    the stronger, still-true invariant that nothing in the message reads as
    an offer/grant of orphan adoption.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "peer's own work")
    _seed_session_scope(repo, "peer-session", ["notes/alpha.md"])
    _seed_file(repo, "orphan/nobody.md", "nobody's claim")
    _seed_session_scope(repo, "attacker-session", [])

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md", "orphan/nobody.md"],
            "message": "sweep it in",
            "session_id": "attacker-session",
            "include_orphans": True,
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert "include_orphans ignored" in result["error"]
    assert "include_orphans: true" not in result["error"]
    assert "include_orphans adopted" not in result["error"]
    assert "'notes/alpha.md'" in result["error"]
    assert "'orphan/nobody.md'" in result["error"]


def test_deletion_of_a_path_claimed_by_a_live_peer_is_still_refused(tmp_path):
    """2026-08-04 fix (defect A/B follow-up, item 3 -- a deletion must be
    subject to the SAME ownership rules as a modification, never a side
    door around the scope gate). A path claimed by a live peer session, then
    DELETED from the worktree by the calling session, must still be denied
    -- deleting a peer's file is no safer to sweep than modifying it.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "notes/alpha.md", "peer's own work")
    _git(["add", "--", "notes/alpha.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_session_scope(repo, "peer-session", ["notes/alpha.md"])
    _seed_session_scope(repo, "attacker-session", [])
    _git(["rm", "-q", "notes/alpha.md"], repo)

    head_before = _head_sha(repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "sweep the deletion in",
            "session_id": "attacker-session",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "error" in result
    assert _head_sha(repo) == head_before
    # Peer's own staged deletion is untouched -- still staged, not reverted.
    status_lines = _porcelain(repo)
    assert any(line.startswith("D") and line.endswith("notes/alpha.md") for line in status_lines)


def test_own_deletion_commits_through_the_ownership_gate(tmp_path):
    """The positive pair to the test above: a session deleting a path
    genuinely inside its OWN scope is unaffected by the C4c gate, exactly
    like a modification.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "notes/mine.md", "my own work")
    _git(["add", "--", "notes/mine.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_session_scope(repo, "own-session", ["notes/mine.md"])
    (repo / "notes" / "mine.md").unlink()

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/mine.md"],
            "message": "remove my own file",
            "session_id": "own-session",
        }
    )

    assert result["committed"] is True
    assert result["sha"]
    assert "error" not in result
    assert not (repo / "notes" / "mine.md").exists()


def test_helper_unimportable_rejects(tmp_path, monkeypatch):
    """`_assert_paths_in_session_scope` degrades to `None` at import time
    (the wrapped-import pattern this module's docstring describes) whenever
    the import beneath it fails -- simulated directly here without
    re-importing the module, since the wrapping happens once at module load.
    The handler must hard-deny, not treat a missing helper as an absent
    constraint. Uses a genuinely DIRTY path so the clean-pathspec bypass
    (see `scoped_git_commit._handler`'s own comment) does not short-circuit
    past the ownership gate before this test can exercise it.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "content")
    _seed_session_scope(repo, "own-session", ["notes/alpha.md"])

    monkeypatch.setattr(scoped_git_commit, "_assert_paths_in_session_scope", None)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
            "session_id": "own-session",
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert "unimportable" in result["error"]


class TestOwnershipVerdictIsObservable:
    """The ALLOW decision must leave a trace (2026-08-04, example-cockpit-repo-em's
    live consumer-side probe): the op ran to completion and emitted exactly
    one line, `committed 8c246dfadf98 (pushed)`. Only the DENY path produced
    any text, so a consumer could not tell whether the gate had evaluated at
    all -- it could only infer an allow backwards from a commit having
    happened, which is precisely the evidence
    `docs/plans/2026-08-04-ownership-inherited-at-dispatch.md` AC9 asks for
    and the engine could not produce.

    Visibility only. Every test here asserts the OUTCOME it asserted before
    (committed True / committed False + `error`) alongside the new record --
    see `scoped_git_commit`'s module docstring, § Ownership-gate
    observability, for the negative spec these pin.
    """

    def test_allow_path_reports_the_verdict_and_the_owner_session_id(self, tmp_path):
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        _seed_file(repo, "tasks/feature/todo.md", "content")
        _seed_session_scope(repo, "own-session", ["tasks/feature/todo.md"])

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["tasks/feature/todo.md"],
                "message": "add feature todo",
                "session_id": "own-session",
            }
        )

        assert result["committed"] is True
        assert result["sha"]
        gate = result["ownership_gate"]
        assert gate["verdict"] == scoped_git_commit.OWNERSHIP_VERDICT_ALLOWED
        assert gate["owner_session_id"] == "own-session"
        assert gate["include_orphans"] is False

    def test_include_orphans_in_effect_is_visible_on_the_allow_path(self, tmp_path):
        """AC9's closing evidence has to say WHICH predicate allowed: an
        orphan-adopting allow and a strict-scope allow are different
        decisions, and a record that cannot distinguish them certifies less
        than it appears to.
        """
        from coordinator_core.session import core as session_core_module

        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        session_core_module.init("real-session", cwd=str(repo))
        _seed_file(repo, "orphan/nobody.md", "nobody's claim")

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["orphan/nobody.md"],
                "message": "commit the orphan",
                "session_id": "real-session",
                "include_orphans": True,
            }
        )

        assert result["committed"] is True
        gate = result["ownership_gate"]
        assert gate["verdict"] == scoped_git_commit.OWNERSHIP_VERDICT_ALLOWED
        assert gate["owner_session_id"] == "real-session"
        assert gate["include_orphans"] is True

    def test_deny_outcome_and_error_text_are_unchanged(self, tmp_path):
        """The negative spec, pinned at the sink: a peer-claimed path still
        denies, with the SAME error prose, and nothing lands. The verdict
        record rides along additively -- it never becomes a denial condition,
        and nothing gates on it.
        """
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        _seed_file(repo, "notes/alpha.md", "peer's own work")
        _seed_session_scope(repo, "peer-session", ["notes/alpha.md"])
        _seed_session_scope(repo, "attacker-session", [])

        head_before = _head_sha(repo)

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["notes/alpha.md"],
                "message": "sweep it in",
                "session_id": "attacker-session",
            }
        )

        assert result["committed"] is False
        assert result["sha"] is None
        assert result["pushed"] is None
        assert "path(s) outside session scope" in result["error"]
        assert _head_sha(repo) == head_before
        assert result["ownership_gate"]["verdict"] == (
            scoped_git_commit.OWNERSHIP_VERDICT_DENIED
        )

    def test_unimportable_helper_is_distinguishable_from_an_allow(
        self, tmp_path, monkeypatch
    ):
        """"The predicate never ran" and "the predicate ran and allowed" are
        different facts. Collapsing the fail-closed branch into either
        `allowed` or `denied` re-creates the unobservability this record
        exists to remove -- a consumer would be unable to tell a working gate
        from a gate whose helper failed to import.
        """
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        _seed_file(repo, "notes/alpha.md", "content")
        _seed_session_scope(repo, "own-session", ["notes/alpha.md"])

        monkeypatch.setattr(scoped_git_commit, "_assert_paths_in_session_scope", None)

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["notes/alpha.md"],
                "message": "add notes",
                "session_id": "own-session",
            }
        )

        assert result["committed"] is False
        assert "unimportable" in result["error"]
        gate = result["ownership_gate"]
        assert gate["verdict"] == (
            scoped_git_commit.OWNERSHIP_VERDICT_HELPER_UNIMPORTABLE
        )
        assert gate["verdict"] != scoped_git_commit.OWNERSHIP_VERDICT_ALLOWED
        assert gate["verdict"] != scoped_git_commit.OWNERSHIP_VERDICT_DENIED
        assert gate["owner_session_id"] is None

    def test_mixed_dirty_and_clean_pathspec_commits_both_paths(self, tmp_path):
        """The regression test for the live incident: a pathspec mixing one
        dirty (in-scope) path with one already-clean-at-HEAD path must
        SUCCEED, committing both -- the clean-pathspec bypass (SC-DR-019) is
        per-path, not all-or-nothing over the whole pathspec.
        """
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _seed_file(repo, "clean.md", "already committed")
        _git(["add", "--", "README.md", "clean.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        _seed_file(repo, "tasks/feature/todo.md", "content")
        _seed_session_scope(repo, "own-session", ["tasks/feature/todo.md"])

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["tasks/feature/todo.md", "clean.md"],
                "message": "add feature todo alongside a clean path",
                "session_id": "own-session",
            }
        )

        assert result["committed"] is True
        assert result["sha"]
        assert "error" not in result

    def test_mixed_dirty_and_clean_pathspec_out_of_scope_dirty_still_denies(self, tmp_path):
        """Half 1 must not widen the gate into a bypass: when the DIRTY half
        of a mixed pathspec is genuinely out of scope (peer-claimed), the
        call still refuses -- and the refusal names ONLY the dirty path, not
        the clean one alongside it.
        """
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _seed_file(repo, "clean.md", "already committed")
        _git(["add", "--", "README.md", "clean.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        _seed_file(repo, "notes/alpha.md", "peer's own work")
        _seed_session_scope(repo, "peer-session", ["notes/alpha.md"])
        _seed_session_scope(repo, "attacker-session", [])

        head_before = _head_sha(repo)

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["notes/alpha.md", "clean.md"],
                "message": "sweep it in",
                "session_id": "attacker-session",
            }
        )

        assert result["committed"] is False
        assert result["sha"] is None
        assert "error" in result
        assert "notes/alpha.md" in result["error"]
        assert "clean.md" not in result["error"]
        assert _head_sha(repo) == head_before

    def test_clean_pathspec_bypass_reports_not_evaluated_never_allowed(self, tmp_path):
        """The clean-pathspec bypass (see `_handler`'s own comment) skips the
        gate entirely: an already-committed, idempotent re-invocation is not
        an ALLOW and must not certify as one.
        """
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)

        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["README.md"],
                "message": "re-commit an already-clean path",
                "session_id": "own-session",
            }
        )

        assert result["committed"] is False
        gate = result["ownership_gate"]
        assert gate["verdict"] == scoped_git_commit.OWNERSHIP_VERDICT_NOT_EVALUATED
        assert gate["owner_session_id"] is None
