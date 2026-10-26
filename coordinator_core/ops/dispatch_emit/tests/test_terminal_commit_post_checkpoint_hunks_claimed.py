"""dispatch.terminal_commit lands a kept-review chunk's paths and refuses while a hunk the run's
kept.json records as post-checkpoint is dirty, outside the landing, and not withheld with a reason."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import kept_work, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.credit_gate import KEPT_REVIEW_CHUNK_ID
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c5"
HUNK_A = "rev/a.py"
HUNK_B = "rev/b.py"
_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1, "prep": {"slice_files": []},
    "wave_reviews": reviewed()["wave_reviews"],
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "rev").mkdir()
    for rel in (HUNK_A, HUNK_B):
        (root / rel).write_text("V = 0\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _stage(repo: Path, *, kept_review: tuple, recorded: tuple) -> None:
    """Dirty both hunks, record `recorded` in kept.json post_checkpoint, stage a request that
    carries a kept-review chunk over `kept_review`."""
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    for rel in (HUNK_A, HUNK_B):
        (repo / rel).write_text("REVIEW_EDIT = 1\n", encoding="utf-8")
    chunks = [ChunkCommit(id="C1", title="t1", paths=("a.py",))]
    if kept_review:
        chunks.append(ChunkCommit(id=KEPT_REVIEW_CHUNK_ID, title="kept post-checkpoint hunks", paths=kept_review))
    request = CommitRequest(chunks=tuple(chunks))
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    kept_work.write(
        run_dir, {}, None, {rel: kept_work.fingerprint(repo, rel) for rel in recorded},
    )
    (repo / "run.mjs").write_text(
        f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json\n", encoding="utf-8"
    )


def _fire(repo: Path, **extra) -> dict:
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF, **extra},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def _dirty(repo: Path, rel: str) -> bool:
    return bool(_git(["status", "--porcelain", "--", rel], repo).strip())


def test_kept_review_chunk_lands_both_hunks(repo):
    _stage(repo, kept_review=(HUNK_A, HUNK_B), recorded=(HUNK_A, HUNK_B))
    out = _fire(repo)
    assert out["committed"] is True, out
    assert {HUNK_A, HUNK_B} <= set(_committed(repo))
    assert not _dirty(repo, HUNK_A) and not _dirty(repo, HUNK_B)


def test_recorded_hunk_outside_the_landing_is_refused_by_name(repo):
    _stage(repo, kept_review=(HUNK_A,), recorded=(HUNK_A, HUNK_B))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _fire(repo)
    assert out.get("committed") is not True, out
    assert out["refused"] == "own-post-checkpoint-hunks-unclaimed"
    assert out["paths"] == [HUNK_B]
    assert HUNK_B in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_withheld_with_a_reason_commits_and_stays_dirty(repo):
    _stage(repo, kept_review=(HUNK_A,), recorded=(HUNK_A, HUNK_B))
    out = _fire(repo, withhold_paths=[{"path": HUNK_B, "reason": "operator holds the second review hunk"}])
    assert out["committed"] is True, out
    assert HUNK_A in _committed(repo)
    assert HUNK_B not in _committed(repo)
    assert _dirty(repo, HUNK_B)


def test_recorded_hunk_already_at_head_does_not_refuse(repo):
    _stage(repo, kept_review=(HUNK_A,), recorded=(HUNK_A, HUNK_B))
    (repo / HUNK_B).write_text("V = 0\n", encoding="utf-8")
    out = _fire(repo)
    assert out["committed"] is True, out
    assert HUNK_A in _committed(repo)
    assert not _dirty(repo, HUNK_B)


def _anchor_fire(repo: Path, **extra) -> dict:
    """A zero-chunk (all rows already checkpointed) landing: no marker, a PASS review with no
    product file, and a plan to anchor on."""
    _stage(repo, kept_review=(), recorded=(HUNK_A,))
    (repo / RUN_DIR_ROOT / "run1" / "commit-request.txt").write_text("", encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "p.md").write_text("# plan\n", encoding="utf-8")
    review = {**_REVIEWED, "delivery": {"verdict": "PASS", "product_files": 0}}
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF, "plan_path": "docs/plans/p.md", **extra},
        repo_root=repo / ".git",
    )


def test_anchor_only_landing_refuses_unclaimed_recorded_hunk(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    out = _anchor_fire(repo)
    assert out.get("committed") is not True, out
    assert out["refused"] == "own-post-checkpoint-hunks-unclaimed"
    assert out["paths"] == [HUNK_A]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_anchor_only_landing_with_hunk_withheld_or_clean_does_not_refuse(repo):
    out = _anchor_fire(repo, withhold_paths=[{"path": HUNK_A, "reason": "operator holds the review hunk"}])
    assert out.get("refused") != "own-post-checkpoint-hunks-unclaimed", out


def test_early_and_precommit_sites_refuse_identically(repo):
    early = _anchor_fire(repo)
    _git(["checkout", "--", "."], repo)
    _git(["clean", "-fdq"], repo)
    for rel in (HUNK_A, HUNK_B):
        (repo / rel).write_text("V = 0\n", encoding="utf-8")
    _stage(repo, kept_review=(), recorded=(HUNK_A,))
    pre = _fire(repo)
    for out in (early, pre):
        assert out["refused"] == "own-post-checkpoint-hunks-unclaimed", out
    assert early["paths"] == pre["paths"] == [HUNK_A]
    for out in (early, pre):
        assert HUNK_A in out["error"] and "recorded in" in out["error"]


def test_unreadable_snapshot_refuses_at_the_pre_commit_site(repo, monkeypatch):
    _stage(repo, kept_review=(HUNK_A,), recorded=(HUNK_A, HUNK_B))
    head = _git(["rev-parse", "HEAD"], repo)
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *a, **k: None)
    out = _fire(repo)
    assert out.get("committed") is not True, out
    assert out["refused"] == "own-post-checkpoint-hunks-unverifiable"
    assert out["paths"] == [HUNK_A, HUNK_B]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_unreadable_snapshot_refuses_at_the_early_site(repo, monkeypatch):
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *a, **k: None)
    out = _anchor_fire(repo)
    assert out.get("committed") is not True, out
    assert out["refused"] == "own-post-checkpoint-hunks-unverifiable"


def test_unreadable_snapshot_without_recorded_hunks_does_not_refuse(repo, monkeypatch):
    _stage(repo, kept_review=(), recorded=())
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *a, **k: None)
    out = _fire(repo)
    assert out.get("refused") != "own-post-checkpoint-hunks-unverifiable", out


def test_named_builder_covers_every_withheld_source_including_sibling_withheld():
    class _Art:
        withheld = {"art.py": "x"}

    named = terminal_commit._post_checkpoint_named(
        {"wh.py": "r"}, {"proc.py": "p"}, art_landable=_Art(),
        sibling_withheld={"sib.py": "run-9"}, left_out={"left.py": "l"}, held=["held.py"],
    )
    assert named == {"wh.py", "proc.py", "art.py", "sib.py", "left.py", "held.py"}
    assert terminal_commit._post_checkpoint_named() == set()


def test_no_recorded_hunks_leaves_landing_unchanged(repo):
    _stage(repo, kept_review=(), recorded=())
    out = _fire(repo)
    assert out["committed"] is True, out
    assert HUNK_A not in _committed(repo)
