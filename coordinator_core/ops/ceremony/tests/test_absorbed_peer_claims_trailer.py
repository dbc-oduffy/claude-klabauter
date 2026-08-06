"""
coordinator_core.ops.ceremony.tests.test_absorbed_peer_claims_trailer

Tests for `commit_pipeline._derive_absorbed_peer_claims_trailer` and its
wiring into `run_commit_pipeline` -- cross-repo ruling SC-DR-019 (example-doctrine-repo,
`coordinator/docs/wiki/scoped-safety-commits.md` @ bdc0aa697): a DERIVED
`Absorbed-peer-claims:` trailer, recorded straight off
`coordinator_core.ops.session.safe_commit_offer.compute_offer`'s own
`excluded` narration for the pipeline's own commit pathspec -- never
author-supplied prose.

Coverage:
  - a peer-claimed staged path produces the trailer, naming the claiming
    session id.
  - no peer claim on the commit pathspec produces no trailer, and no empty
    `Absorbed-peer-claims:` header.
  - caller-supplied `trailers` are preserved verbatim and the derived block
    is APPENDED, never replacing them.
  - a raising/degraded `compute_offer` still lands the commit, with no
    trailer (fail-open, DR-256 warn-only-compatible).
  - multi-path ordering is deterministic (sorted by path).
  - the live-peer precondition gate (`_live_peer_exists`): with no live peer,
    `compute_offer` is not called AT ALL (the skipped walk is the point, not
    merely the absent trailer); with a live peer the derivation proceeds; a
    raising liveness enumeration yields no trailer, no `compute_offer` call,
    and a landed commit.
  - AC5/AC6 (plan "touched.txt sibling-path escape and the suppressed
    absorbed-peer-claims trailer", P2): a live-peer-claimed path absorbed
    through `ceremony.scoped_git_commit`'s OWN OP HANDLER -- the path that
    mints `scoped-git-commit-<uuid4>` in production -- rather than through
    `run_commit_pipeline` with a hand-`core.init`ed id, as every test above
    does. That id never gets a session dir, and P2 hid for this whole file's
    lifetime because nothing before this exercised the minting path.
  - AC8: the three ordered gates `_derive_absorbed_peer_claims_trailer`
    resolves through, pinned directly against the function so the
    could-not-determine marker's firing boundary is asserted, not assumed:
    gate (a) no live peer -> `""`, no marker; gate (b) a live peer exists but
    `session_id` is empty or names a session with no session dir -> the
    marker; gate (c) a resolved real session dir, whether `compute_offer`
    raises or genuinely finds nothing -> `""`, marker must NOT fire.

Spec backlink: coordinator_core/ops/ceremony/commit_pipeline.py,
`_derive_absorbed_peer_claims_trailer`.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import coordinator_core.ops.ceremony.commit_pipeline as commit_pipeline_mod
from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline
from coordinator_core.session import core, scope


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _commit_message_at_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def test_peer_claimed_path_produces_trailer(tmp_path):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    (repo / "shared.py").write_text("shared content", encoding="utf-8")
    scope.touch(peer, "shared.py", cwd=str(repo))

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: shared",
        stage_paths=["shared.py"],
        caller_paths={"shared.py"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    message = _commit_message_at_head(repo)
    assert "Absorbed-peer-claims:" in message
    assert f"  shared.py: session {peer} (claimed, live)" in message


def test_no_peer_claim_no_trailer(tmp_path):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    core.init(mine, cwd=str(repo))

    (repo / "mine.py").write_text("mine content", encoding="utf-8")
    scope.touch(mine, "mine.py", cwd=str(repo))

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: mine",
        stage_paths=["mine.py"],
        caller_paths={"mine.py"},
    )

    assert result.commit_failed is False, result.diagnostics
    message = _commit_message_at_head(repo)
    assert "Absorbed-peer-claims:" not in message


def test_caller_trailers_preserved_and_block_appended(tmp_path):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    (repo / "shared.py").write_text("shared content", encoding="utf-8")
    scope.touch(peer, "shared.py", cwd=str(repo))

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: shared",
        stage_paths=["shared.py"],
        caller_paths={"shared.py"},
        trailers="Nature: fix",
    )

    assert result.commit_failed is False, result.diagnostics
    message = _commit_message_at_head(repo)
    assert "Nature: fix" in message
    assert "Absorbed-peer-claims:" in message
    # Appended, not replacing -- caller trailer comes first, verbatim.
    nature_idx = message.index("Nature: fix")
    absorbed_idx = message.index("Absorbed-peer-claims:")
    assert nature_idx < absorbed_idx
    assert f"  shared.py: session {peer} (claimed, live)" in message


def test_degraded_compute_offer_still_commits_no_trailer(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    core.init(mine, cwd=str(repo))

    (repo / "shared.py").write_text("shared content", encoding="utf-8")

    def _boom(session_id, cwd=None):
        raise RuntimeError("simulated compute_offer failure")

    monkeypatch.setattr(commit_pipeline_mod, "compute_offer", _boom)

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: shared",
        stage_paths=["shared.py"],
        caller_paths={"shared.py"},
    )

    # Fail-open: the commit lands exactly as it would have with no
    # derivation attempted at all -- never raises into the commit path.
    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    message = _commit_message_at_head(repo)
    assert "Absorbed-peer-claims:" not in message


def _spy_compute_offer(monkeypatch):
    """Wrap `compute_offer` with a call recorder; returns the calls list."""
    calls: list = []
    real = commit_pipeline_mod.compute_offer

    def _recording(session_id, cwd=None):
        calls.append((session_id, cwd))
        return real(session_id, cwd)

    monkeypatch.setattr(commit_pipeline_mod, "compute_offer", _recording)
    return calls


def test_no_live_peer_skips_compute_offer_entirely(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    core.init(mine, cwd=str(repo))

    (repo / "mine.py").write_text("mine content", encoding="utf-8")
    scope.touch(mine, "mine.py", cwd=str(repo))

    calls = _spy_compute_offer(monkeypatch)

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: mine",
        stage_paths=["mine.py"],
        caller_paths={"mine.py"},
    )

    assert result.commit_failed is False, result.diagnostics
    # The COST is the point: with no live peer the walk must not run at all.
    assert calls == []
    assert "Absorbed-peer-claims:" not in _commit_message_at_head(repo)


def test_live_peer_present_pays_the_walk_and_emits_trailer(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    (repo / "shared.py").write_text("shared content", encoding="utf-8")
    scope.touch(peer, "shared.py", cwd=str(repo))

    calls = _spy_compute_offer(monkeypatch)

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: shared",
        stage_paths=["shared.py"],
        caller_paths={"shared.py"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert len(calls) == 1
    message = _commit_message_at_head(repo)
    assert f"  shared.py: session {peer} (claimed, live)" in message


def test_raising_liveness_yields_no_trailer_and_no_walk(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    (repo / "shared.py").write_text("shared content", encoding="utf-8")
    scope.touch(peer, "shared.py", cwd=str(repo))

    def _boom(cwd=None):
        raise RuntimeError("simulated liveness enumeration failure")

    monkeypatch.setattr(commit_pipeline_mod, "live_session_ids", _boom)
    calls = _spy_compute_offer(monkeypatch)

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: shared",
        stage_paths=["shared.py"],
        caller_paths={"shared.py"},
    )

    # Degraded liveness fails toward "no trailer" -- it must NOT fall through
    # to compute_offer, and the commit still lands.
    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert calls == []
    assert "Absorbed-peer-claims:" not in _commit_message_at_head(repo)


def test_multi_path_ordering_is_deterministic(tmp_path):
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer_z = f"peer-z-{uuid.uuid4().hex[:8]}"
    peer_a = f"peer-a-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer_z, cwd=str(repo))
    core.init(peer_a, cwd=str(repo))

    (repo / "zzz.py").write_text("z", encoding="utf-8")
    (repo / "aaa.py").write_text("a", encoding="utf-8")
    scope.touch(peer_z, "zzz.py", cwd=str(repo))
    scope.touch(peer_a, "aaa.py", cwd=str(repo))

    result = run_commit_pipeline(
        repo,
        session_id=mine,
        subject="feature: multi",
        stage_paths=["zzz.py", "aaa.py"],
        caller_paths={"zzz.py", "aaa.py"},
    )

    assert result.commit_failed is False, result.diagnostics
    message = _commit_message_at_head(repo)
    aaa_idx = message.index("aaa.py: session")
    zzz_idx = message.index("zzz.py: session")
    assert aaa_idx < zzz_idx


def _call_op_handler(params: dict) -> dict:
    return asyncio.run(scoped_git_commit._handler(params, repo_root=None))


def test_op_handler_absorbs_live_peer_claim(tmp_path, monkeypatch):
    """AC5 + AC6: drive `ceremony.scoped_git_commit`'s OP HANDLER -- the path
    that actually mints `scoped-git-commit-<uuid4>` in production -- rather
    than `run_commit_pipeline` with a hand-`core.init`ed id, as every test
    above this one does. Every one of those hides P2: the id `core.init`
    creates always has a `started_at`, which is exactly what the op
    handler's minted lock-only id never gets. The `session_id` param here is
    the override `resolve_session_id`/`_handler` both honor ahead of the
    ambient-environment tiers (see `_handler`'s own "params.get('session_id')
    mirrors..." comment) -- it names the ACTUAL committing session `mine`,
    kept deliberately separate from the lock-key id `_handler` mints
    internally.
    """
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    (repo / "shared.py").write_text("shared content", encoding="utf-8")
    scope.touch(peer, "shared.py", cwd=str(repo))

    monkeypatch.setattr(
        scoped_git_commit, "_assert_paths_in_session_scope", lambda *a, **k: (True, "")
    )

    response = _call_op_handler({
        "worktree_root": str(repo),
        "paths": ["shared.py"],
        "message": "feature: shared",
        "session_id": mine,
    })

    assert response["committed"] is True, response
    message = _commit_message_at_head(repo)
    assert "Absorbed-peer-claims:" in message
    assert f"  shared.py: session {peer} (claimed, live)" in message


def test_gate_a_no_live_peer_yields_empty_no_marker(tmp_path):
    """AC8 gate (a): no live peer -> `""`, never the marker."""
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    core.init(mine, cwd=str(repo))

    result = commit_pipeline_mod._derive_absorbed_peer_claims_trailer(
        mine, ["mine.py"], str(repo)
    )
    assert result == ""


def test_gate_b_empty_session_id_with_live_peer_fires_marker(tmp_path):
    """AC8 gate (b): a live peer exists, but `session_id` is empty -- the
    could-not-determine marker fires, distinct from an ordinary `""`."""
    repo = _init_repo(tmp_path)
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(peer, cwd=str(repo))

    result = commit_pipeline_mod._derive_absorbed_peer_claims_trailer(
        "", ["shared.py"], str(repo)
    )
    assert result == commit_pipeline_mod._ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER


def test_gate_b_unresolvable_session_id_with_live_peer_fires_marker(tmp_path):
    """AC8 gate (b): a live peer exists, but `session_id` names no existing
    session dir (P2's own shape -- a lock-only id, never registered) -- the
    marker fires, exactly as the empty-string case above does."""
    repo = _init_repo(tmp_path)
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(peer, cwd=str(repo))
    unregistered = f"scoped-git-commit-{uuid.uuid4().hex}"

    result = commit_pipeline_mod._derive_absorbed_peer_claims_trailer(
        unregistered, ["shared.py"], str(repo)
    )
    assert result == commit_pipeline_mod._ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER


def test_gate_c_resolved_session_degraded_compute_offer_no_marker(tmp_path, monkeypatch):
    """AC8 gate (c): a resolved real session dir, but `compute_offer` raises
    -- `""`, and the marker must NOT fire (distinguishing this from gate
    (b)'s unresolvable-id case is the whole point of the marker existing)."""
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    def _boom(session_id, cwd=None):
        raise RuntimeError("simulated compute_offer failure")

    monkeypatch.setattr(commit_pipeline_mod, "compute_offer", _boom)

    result = commit_pipeline_mod._derive_absorbed_peer_claims_trailer(
        mine, ["shared.py"], str(repo)
    )
    assert result == ""
    assert result != commit_pipeline_mod._ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER


def test_gate_c_resolved_session_finds_nothing_no_marker(tmp_path):
    """AC8 gate (c): a resolved real session dir, `compute_offer` genuinely
    finds nothing claimed under this commit's pathspec -- `""`, no marker."""
    repo = _init_repo(tmp_path)
    mine = _unique_session_id()
    peer = f"peer-{uuid.uuid4().hex[:8]}"
    core.init(mine, cwd=str(repo))
    core.init(peer, cwd=str(repo))

    (repo / "mine.py").write_text("mine content", encoding="utf-8")
    scope.touch(mine, "mine.py", cwd=str(repo))

    result = commit_pipeline_mod._derive_absorbed_peer_claims_trailer(
        mine, ["mine.py"], str(repo)
    )
    assert result == ""
    assert result != commit_pipeline_mod._ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER
