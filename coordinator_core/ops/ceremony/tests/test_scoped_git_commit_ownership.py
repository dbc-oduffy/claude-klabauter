"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_ownership

Tests for the O(pathspec) ownership gate re-added to `ceremony.
scoped_git_commit` by C2 of
docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md.

Composes `coordinator_core.session.claim_index.lookup` (C1) +
`coordinator_core.session.liveness.session_live` (C3) via `scoped_git_
commit._check_claim_conflicts` — never `compute_scope`/`compute_offer`/
`assert_paths_in_session_scope` (the excised gate's O(dirty tree) walk;
see that module's own docstring for why re-adding it would recreate the
outage this plan exists to remove).

Peers are synthesized entirely by WRITING session dirs (`touched.txt` +
`meta.json`) under a throwaway repo's `.git/coordinator-sessions/` —
never by spawning a real process (this repo's CLAUDE.md counts
fixture-spawned processes against shared-machine load; only real `git`
CLI calls are spawned here, the same fixture shape
`test_scoped_git_commit.py` already uses).

The excised original at `de27716^` is a starting point for the allow/deny
shape only, not for the walk it performed.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import guard_unlock_sentinel

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_and_commit_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-q", "-m", "seed"], repo)


def _dirty_file(repo: Path, rel_path: str, content: str) -> None:
    """Modify (or create) *rel_path* so it shows dirty, without staging it."""
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_touched(repo: Path, sid: str, lines: list[str]) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _write_meta(repo: Path, sid: str, *, live: bool) -> None:
    """Write a Layer-2 (recency-only) meta.json for *sid* — no `stable_pid`,
    so `liveness.session_live` falls straight to the recency fallback:
    `last_activity` inside the 30-minute window reads live, well outside it
    reads dead. Simplest correct fixture shape for this gate's own tests —
    it exercises `session_live`'s already-tested contract, not a re-test of
    it (see `coordinator_core/session/tests/test_liveness.py` for that).
    """
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = (
        session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    )
    (sdir / "meta.json").write_text(
        '{"pid": 1, "last_activity": "%s"}\n' % last_activity,
        encoding="utf-8",
    )


def _touch_line(verb: str, path: str) -> str:
    return "%s 2026-08-08T10:00:00.000000Z %s" % (verb, path)


def _call(params: dict) -> dict:
    # _handler is a plain sync function (2026-08-07 transport-hang fix) --
    # no asyncio.run wrapper needed or possible.
    return scoped_git_commit._handler(params, repo_root=None)


# ---------------------------------------------------------------------------
# Allow: caller holds the path itself
# ---------------------------------------------------------------------------


def test_allow_when_caller_holds_the_claim(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "owned.txt", "v1\n")
    _dirty_file(repo, "owned.txt", "v2\n")

    _write_touched(repo, "sess-caller", [_touch_line("T", "owned.txt")])
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["owned.txt"],
        "message": "commit my own file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result


def test_allow_when_path_is_unclaimed(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "free.txt", "v1\n")
    _dirty_file(repo, "free.txt", "v2\n")

    result = _call({
        "worktree_root": str(repo),
        "paths": ["free.txt"],
        "message": "commit an unclaimed file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result


# ---------------------------------------------------------------------------
# Deny: a LIVE peer holds the path, named individually (AC4)
# ---------------------------------------------------------------------------


def test_refuse_path_claimed_by_live_peer(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "steal a live peer's file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "peers.txt" in result["error"]
    assert "sess-peer" in result["error"]


def test_refusal_names_who_claims_path_instrument_and_plane_distinction(tmp_path):
    """The refusal must tell a reader THREE things: this is a path-touch
    claim (not an artifact claim), `who-claims-path` is the instrument to
    inspect it, and `list-claims-by-session` answers a different question —
    see this module's `_CLAIM_CONFLICT_REMEDY`."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "steal a live peer's file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "who-claims-path" in result["error"]
    assert "path-touch claim" in result["error"]
    assert "list-claims-by-session" in result["error"]


def test_refusal_offers_no_rank_based_escape(tmp_path):
    """The remedy must not name an escape the gate cannot honour.

    `_check_claim_conflicts` has no override parameter and reads no caller
    rank, so "ask an EM to re-issue" was a remedy no EM could execute --
    a reader who goes looking for it finds the bypass instead. The refusal
    must instead name the two things that DO work: wait for the holder's
    session to end, or narrow the pathspec.
    """
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "steal a live peer's file",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "re-issue" not in result["error"]
    assert "clears when that session ends" in result["error"]
    assert "drop the affected path(s) from the pathspec" in result["error"]


def test_refusal_is_per_path_not_per_pathspec(tmp_path):
    """AC4: a live peer claim on ONE path must not deny a sibling path in
    the same call."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _seed_and_commit_file(repo, "free.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")
    _dirty_file(repo, "free.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt", "free.txt"],
        "message": "mixed pathspec",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "peers.txt" in result["error"]
    assert "free.txt" not in result["error"]


# ---------------------------------------------------------------------------
# Allow: a NOT-live peer's claim is not a conflict
# ---------------------------------------------------------------------------


def test_allow_when_holder_is_not_live(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "orphaned.txt", "v1\n")
    _dirty_file(repo, "orphaned.txt", "v2\n")

    _write_touched(repo, "sess-dead", [_touch_line("T", "orphaned.txt")])
    _write_meta(repo, "sess-dead", live=False)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["orphaned.txt"],
        "message": "commit an orphaned claim",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result


# ---------------------------------------------------------------------------
# Unanswerable path: fails closed PER PATH, sibling proceeds
# ---------------------------------------------------------------------------


def test_unanswerable_path_fails_closed_sibling_proceeds(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "cannot-answer.txt", "v1\n")
    _seed_and_commit_file(repo, "free.txt", "v1\n")
    _dirty_file(repo, "cannot-answer.txt", "v2\n")
    _dirty_file(repo, "free.txt", "v2\n")
    _write_meta(repo, "sess-caller", live=True)

    real_lookup = claim_index.lookup

    def _fake_lookup(paths, sessions_dir=None, cwd=None):
        result = real_lookup(paths, sessions_dir=sessions_dir, cwd=cwd)
        if "cannot-answer.txt" in result:
            result["cannot-answer.txt"] = [claim_index.UNANSWERABLE]
        return result

    monkeypatch.setattr(scoped_git_commit.claim_index, "lookup", _fake_lookup)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["cannot-answer.txt", "free.txt"],
        "message": "one path unanswerable",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "cannot-answer.txt" in result["error"]
    assert "free.txt" not in result["error"]


# ---------------------------------------------------------------------------
# AC10a — forged session_id presented as the caller
# ---------------------------------------------------------------------------


def test_forged_session_id_against_live_peer_claim_is_refused(tmp_path):
    """A `session_id` that names no real session dir at all gains no
    advantage over an honest one when a REAL live peer holds the target
    path — refused exactly as an honest caller with no claim would be."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "guarded.txt", "v1\n")
    _dirty_file(repo, "guarded.txt", "v2\n")

    _write_touched(repo, "sess-real-holder", [_touch_line("T", "guarded.txt")])
    _write_meta(repo, "sess-real-holder", live=True)
    # Deliberately no session dir at all for the forged id.

    result = _call({
        "worktree_root": str(repo),
        "paths": ["guarded.txt"],
        "message": "forged identity attempt",
        "session_id": "sess-forged-does-not-exist",
    })

    assert result["committed"] is False
    assert "guarded.txt" in result["error"]


def test_forged_session_id_degrades_to_unanswerable_not_allow_when_peer_not_live(
    tmp_path,
):
    """AC10a: an unresolvable caller identity must never fall through to
    'no conflict, proceed' just because the conflicting peer happens not
    to be live -- it degrades to the same fail-closed policy as an
    unanswerable index path."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "guarded.txt", "v1\n")
    _dirty_file(repo, "guarded.txt", "v2\n")

    _write_touched(repo, "sess-real-holder", [_touch_line("T", "guarded.txt")])
    _write_meta(repo, "sess-real-holder", live=False)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["guarded.txt"],
        "message": "forged identity, peer not live",
        "session_id": "sess-forged-does-not-exist",
    })

    assert result["committed"] is False
    assert "guarded.txt" in result["error"]


# ---------------------------------------------------------------------------
# session_live invoked only for MATCHED claimants (C3)
# ---------------------------------------------------------------------------


def test_session_live_called_only_for_matched_claimants(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    calls: list[str] = []
    real_session_live = scoped_git_commit.session_liveness.session_live

    def _tracking_session_live(sid, cwd=None):
        calls.append(sid)
        return real_session_live(sid, cwd)

    monkeypatch.setattr(
        scoped_git_commit.session_liveness, "session_live", _tracking_session_live
    )

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "check liveness call pattern",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert calls == ["sess-peer"]  # never the caller, never an enumeration


# ---------------------------------------------------------------------------
# include_orphans (AC9) — accepted, no effect on outcome
# ---------------------------------------------------------------------------


def test_include_orphans_has_no_effect_on_not_live_holder_allow(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "orphaned.txt", "v1\n")
    _dirty_file(repo, "orphaned.txt", "v2\n")

    _write_touched(repo, "sess-dead", [_touch_line("T", "orphaned.txt")])
    _write_meta(repo, "sess-dead", live=False)
    _write_meta(repo, "sess-caller", live=True)

    without = _call({
        "worktree_root": str(repo),
        "paths": ["orphaned.txt"],
        "message": "no include_orphans",
        "session_id": "sess-caller",
    })
    assert without["committed"] is True

    # Re-dirty the same file for a second, otherwise-identical call with
    # include_orphans set -- must reach the identical outcome.
    _dirty_file(repo, "orphaned.txt", "v3\n")
    with_flag = _call({
        "worktree_root": str(repo),
        "paths": ["orphaned.txt"],
        "message": "with include_orphans",
        "session_id": "sess-caller",
        "include_orphans": True,
    })
    assert with_flag["committed"] is True


def test_include_orphans_does_not_relax_live_peer_refusal(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "include_orphans against a live peer",
        "session_id": "sess-caller",
        "include_orphans": True,
    })

    assert result["committed"] is False
    assert "peers.txt" in result["error"]


def test_include_orphans_does_not_relax_unanswerable_path(tmp_path, monkeypatch):
    """Review: code-reviewer -- Finding [P2], 2026-08-08. The plan body
    explicitly rejected wiring `include_orphans` to relax the UNANSWERABLE
    branch, since an index that could not answer is not the same thing as
    a claim that resolved to "orphaned" (positive/negative asymmetry rule).
    `include_orphans` is fully unused today (AC9), so this cannot currently
    fire -- this test exists to catch a future re-wiring that reaches the
    unanswerable branch."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "cannot-answer.txt", "v1\n")
    _dirty_file(repo, "cannot-answer.txt", "v2\n")
    _write_meta(repo, "sess-caller", live=True)

    real_lookup = claim_index.lookup

    def _fake_lookup(paths, sessions_dir=None, cwd=None):
        result = real_lookup(paths, sessions_dir=sessions_dir, cwd=cwd)
        if "cannot-answer.txt" in result:
            result["cannot-answer.txt"] = [claim_index.UNANSWERABLE]
        return result

    monkeypatch.setattr(scoped_git_commit.claim_index, "lookup", _fake_lookup)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["cannot-answer.txt"],
        "message": "include_orphans against an unanswerable path",
        "session_id": "sess-caller",
        "include_orphans": True,
    })

    assert result["committed"] is False
    assert "cannot-answer.txt" in result["error"]


# ---------------------------------------------------------------------------
# C10 -- a caller-only positive claimant under an INCOMPLETE walk must not
# authorize a write: the walk may have aborted before reaching a live
# peer's claim on the SAME path.
#
# docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md, folded in
# mid-execution from a peer EM's defect report (claude-klabauter-em, session
# 31106a01), independently verified at HEAD before this brief was written.
# ---------------------------------------------------------------------------


def _fake_lookup_caller_only_incomplete(caller_sid: str, path: str):
    """Build a `claim_index.lookup` replacement whose return value has the
    exact shape the C10 defect composes over: a POSITIVE claimant list
    containing nothing but *caller_sid* for *path*, paired with
    `.complete = False` -- the signature of a walk that read the caller's
    own `touched.txt` and then aborted (wall-clock cap / unreadable claim
    source) before it could have reached a live peer's claim on the same
    path. Never spawns a rebuild -- returns a canned `_LookupResult`
    directly, so the fixture needs no real peer session dir at all."""

    def _fake_lookup(paths, sessions_dir=None, cwd=None):
        result = claim_index._LookupResult()
        for p in paths:
            result[p] = [caller_sid] if p == path else []
        result.complete = False
        return result

    return _fake_lookup


def test_incomplete_walk_caller_only_claimant_is_refused_not_allowed(
    tmp_path, monkeypatch
):
    """The repro this chunk's brief required be written and run BEFORE any
    fix: pre-C10 code (`others = claimants - {caller_sid}`, no completeness
    check at all) allowed this composition unconditionally. Post-fix, this
    same call must refuse -- the walk that produced the caller-only
    positive answer never confirmed no OTHER claimant exists behind it."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "race.txt", "v1\n")
    _dirty_file(repo, "race.txt", "v2\n")
    _write_meta(repo, "sess-caller", live=True)

    monkeypatch.setattr(
        scoped_git_commit.claim_index,
        "lookup",
        _fake_lookup_caller_only_incomplete("sess-caller", "race.txt"),
    )

    result = _call({
        "worktree_root": str(repo),
        "paths": ["race.txt"],
        "message": "commit under an incomplete walk that only saw my own claim",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "race.txt" in result["error"]


def test_incomplete_walk_dead_peer_only_claimant_still_allowed(
    tmp_path, monkeypatch
):
    """Guard against the REJECTED blanket form ("any positive under
    complete=False becomes unanswerable"): a walk that resolved a path to
    a claimant OTHER than the caller must still go through the ordinary
    live/dead liveness gate, unaffected by walk completeness -- a dead
    peer's stale claim stays allowed even when the walk that found it was
    incomplete, exactly as it was allowed when the walk was complete."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "orphaned.txt", "v1\n")
    _dirty_file(repo, "orphaned.txt", "v2\n")
    _write_meta(repo, "sess-dead", live=False)
    _write_meta(repo, "sess-caller", live=True)

    def _fake_lookup(paths, sessions_dir=None, cwd=None):
        result = claim_index._LookupResult()
        for p in paths:
            result[p] = ["sess-dead"] if p == "orphaned.txt" else []
        result.complete = False
        return result

    monkeypatch.setattr(scoped_git_commit.claim_index, "lookup", _fake_lookup)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["orphaned.txt"],
        "message": "dead peer's claim under an incomplete walk",
        "session_id": "sess-caller",
    })

    assert result["committed"] is True
    assert "error" not in result


def test_incomplete_walk_live_peer_claim_still_refused(tmp_path, monkeypatch):
    """A claimant list already containing a live OTHER session refuses
    through the pre-existing conflict path regardless of walk completeness
    -- this chunk's added conjunct must never be the thing that makes this
    case refuse OR allow; it was already refusing before C10."""
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    def _fake_lookup(paths, sessions_dir=None, cwd=None):
        result = claim_index._LookupResult()
        for p in paths:
            result[p] = ["sess-peer"] if p == "peers.txt" else []
        result.complete = False
        return result

    monkeypatch.setattr(scoped_git_commit.claim_index, "lookup", _fake_lookup)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "live peer claim under an incomplete walk",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert "peers.txt" in result["error"]


# ---------------------------------------------------------------------------
# DR-260 break-past (B5/B6/B7): docs/plans/2026-08-11-kill-on-staleness-and-
# a-way-past-the-gate.md, mechanism-gate spike record
# docs/research/spike-verdicts/2026-08-11-dr-260-unlock-at-the-op-level-
# claim-gate.md.
#
# `_unique_sid` mints a fresh, unlikely-to-collide session id per test --
# `guard_unlock_sentinel.sentinel_path()` resolves under the REAL, SHARED
# platform temp directory (never `tmp_path`), and this repo's own CLAUDE.md
# names this box as carrying 50-70 concurrent LLM sessions: a fixed literal
# session id here would risk colliding with a live peer's own sentinel.
# ---------------------------------------------------------------------------


def _unique_sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _cleanup_sentinel(session_id: str, guard_name: str) -> None:
    """Best-effort removal of a sentinel this test minted, in case the
    unlock under test was never actually consumed (e.g. an assertion failed
    first) -- the shared temp dir has no per-test isolation, so a leaked
    sentinel would silently grant a LATER, unrelated call for the same
    session id."""
    try:
        guard_unlock_sentinel.sentinel_path(session_id, guard_name).unlink()
    except OSError:
        pass


def test_unlock_grant_is_consumed_with_owner_session_id_never_the_pipeline_nonce(
    tmp_path, monkeypatch
):
    """B5's whole point (mechanism-gate spike Finding 1): the id handed to
    `guard_unlock_sentinel.consume()` must be `owner_session_id` -- the
    caller's real, resolvable identity already in hand at this gate --
    NEVER the `scoped-git-commit-<uuid4>` nonce minted later, purely for
    `run_commit_pipeline`'s own bookkeeping, which is fresh every call and
    could never be pre-minted against by an operator. A test that only
    checks the outcome (denied without a sentinel, allowed with one) passes
    even when this identity is wired wrong -- see the spike record's own
    warning. This test asserts the identity itself.
    """
    repo = _init_repo(tmp_path)
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, "sess-peer", [_touch_line("T", "peers.txt")])
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    captured: list[tuple] = []
    real_consume = guard_unlock_sentinel.consume

    def _tracking_consume(session_id, guard_name):
        captured.append((session_id, guard_name))
        return real_consume(session_id, guard_name)

    monkeypatch.setattr(scoped_git_commit.guard_unlock_sentinel, "consume", _tracking_consume)

    result = _call({
        "worktree_root": str(repo),
        "paths": ["peers.txt"],
        "message": "no sentinel minted -- consume() still called, still denies",
        "session_id": "sess-caller",
    })

    assert result["committed"] is False
    assert captured == [("sess-caller", scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME)]


def test_unlock_grant_clears_the_refusal_and_proceeds(tmp_path):
    """A real, hand-minted DR-260 sentinel for `(owner_session_id,
    _CLAIM_CONFLICT_GUARD_NAME)` clears a live-claimant refusal and lets the
    commit land -- end to end, no mocking of `consume()` itself."""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    guard_name = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME
    sentinel = guard_unlock_sentinel.sentinel_path(sid_caller, guard_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        result = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "break past a live claimant via DR-260 unlock",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_caller, guard_name)

    assert result["committed"] is True
    assert "error" not in result
    assert not sentinel.exists()  # consumed (unlinked), not merely read


def test_unlock_is_one_shot_second_attempt_re_refuses(tmp_path):
    """AC3/(one-shot, DR-260): the same sentinel does not grant twice. A
    retry after the grant was already spent meets the identical refusal."""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    guard_name = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME
    sentinel = guard_unlock_sentinel.sentinel_path(sid_caller, guard_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        first = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "first attempt -- consumes the sentinel",
            "session_id": sid_caller,
        })
        assert first["committed"] is True

        # Re-dirty the same path so the claim conflict recurs for a SECOND,
        # otherwise-identical call -- the sentinel is already spent.
        _dirty_file(repo, "peers.txt", "v3\n")
        second = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "second attempt -- no sentinel left, re-refused",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_caller, guard_name)

    assert second["committed"] is False
    assert "peers.txt" in second["error"]


def test_unlock_ignores_a_sentinel_minted_for_a_different_session(tmp_path):
    """Cross-key isolation (spike Q3, leg 6): a sentinel minted for a
    DIFFERENT session id must not grant this caller's own refusal."""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_other = _unique_sid("sess-someone-else")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    guard_name = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME
    sentinel = guard_unlock_sentinel.sentinel_path(sid_other, guard_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        result = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "a peer's own unlock does not grant mine",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_other, guard_name)

    assert result["committed"] is False
    assert "peers.txt" in result["error"]


def test_unlock_ignores_a_sentinel_minted_for_a_different_guard(tmp_path):
    """Cross-key isolation (spike Q3, leg 5): a sentinel minted for the SAME
    session but a DIFFERENT guard name must not grant this guard."""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    other_guard = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME + "-not-this-one"
    sentinel = guard_unlock_sentinel.sentinel_path(sid_caller, other_guard)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        result = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "a different guard's own unlock does not grant this one",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_caller, other_guard)

    assert result["committed"] is False
    assert "peers.txt" in result["error"]


def test_unlock_grant_does_not_relax_a_sibling_unanswerable_path(
    tmp_path, monkeypatch
):
    """A grant clears only `conflicted` -- the UNANSWERABLE-path fail-closed
    policy is a different concern (claim-index health, not claim
    ownership) that DR-260's unlock has no bearing on. 'One grant clears
    one guard once -- it never disables the gate for the invocation'
    (B5 spec)."""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _seed_and_commit_file(repo, "cannot-answer.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")
    _dirty_file(repo, "cannot-answer.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    real_lookup = claim_index.lookup

    def _fake_lookup(paths, sessions_dir=None, cwd=None):
        result = real_lookup(paths, sessions_dir=sessions_dir, cwd=cwd)
        if "cannot-answer.txt" in result:
            result["cannot-answer.txt"] = [claim_index.UNANSWERABLE]
        return result

    monkeypatch.setattr(scoped_git_commit.claim_index, "lookup", _fake_lookup)

    guard_name = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME
    sentinel = guard_unlock_sentinel.sentinel_path(sid_caller, guard_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        result = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt", "cannot-answer.txt"],
            "message": "grant clears the live conflict, not the unanswerable path",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_caller, guard_name)

    assert result["committed"] is False
    assert "cannot-answer.txt" in result["error"]
    assert "peers.txt" not in result["error"]


def test_unlock_grant_leaves_a_durable_override_record(tmp_path):
    """B7: `consume()` writes nothing and the sentinel is gone the moment it
    succeeds (mechanism-gate spike, Finding 3) -- so the grant must leave
    its own record, naming the overridden path(s) and the overriding
    session, or the override is invisible after the fact."""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    guard_name = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME
    sentinel = guard_unlock_sentinel.sentinel_path(sid_caller, guard_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        result = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "grant leaves a durable record",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_caller, guard_name)

    assert result["committed"] is True

    record_path = (
        repo / "state" / "subagent-share" / sid_caller / "claim-conflict-overrides.jsonl"
    )
    assert record_path.is_file()
    lines = [ln for ln in record_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["guard"] == guard_name
    assert record["session"] == sid_caller
    assert record["overridden_paths"] == ["peers.txt"]
    assert "at" in record


def test_override_record_write_failure_does_not_convert_grant_into_refusal(
    tmp_path, monkeypatch
):
    """HARD CONSTRAINT (DR-260's own negative spec): whatever records the
    grant must not be able to raise inside `consume()`'s caller. A crash in
    the recording path must never turn an already-decided grant back into a
    refusal -- that would fail a hard-deny guard OPEN via a path meant only
    to add observability, which is the one direction it must never fail
    in. (It is also never allowed to fabricate a record it could not
    actually write -- asserted below via the record's absence.)"""
    repo = _init_repo(tmp_path)
    sid_caller = _unique_sid("sess-caller")
    sid_peer = _unique_sid("sess-peer")
    _seed_and_commit_file(repo, "peers.txt", "v1\n")
    _dirty_file(repo, "peers.txt", "v2\n")

    _write_touched(repo, sid_peer, [_touch_line("T", "peers.txt")])
    _write_meta(repo, sid_peer, live=True)
    _write_meta(repo, sid_caller, live=True)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated record-write failure")

    monkeypatch.setattr(scoped_git_commit.json, "dumps", _boom)

    guard_name = scoped_git_commit._CLAIM_CONFLICT_GUARD_NAME
    sentinel = guard_unlock_sentinel.sentinel_path(sid_caller, guard_name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        result = _call({
            "worktree_root": str(repo),
            "paths": ["peers.txt"],
            "message": "record write fails -- grant must still proceed",
            "session_id": sid_caller,
        })
    finally:
        _cleanup_sentinel(sid_caller, guard_name)

    assert result["committed"] is True
    record_path = (
        repo / "state" / "subagent-share" / sid_caller / "claim-conflict-overrides.jsonl"
    )
    assert not record_path.exists()
