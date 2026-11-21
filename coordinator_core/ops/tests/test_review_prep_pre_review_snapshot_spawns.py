"""review.prep: the write-once pre-review snapshot of the product bytes it hashes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

SESSION = "sess-snap"
BASE = "a" * 40


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.strip()


@pytest.fixture
def real_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "src" / "a.py").write_bytes(b"a = 1\n")
    (repo / "plan.md").write_text("---\nplan_id: p\n---\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_bytes(b"a = 2\n")
    (repo / "src" / "b.py").write_bytes(b"b = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "range")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(rp, "freeze_diff", lambda *a, **k: {"diff_path": str(tmp_path / "w.diff"), "error": None})
    monkeypatch.setattr(rp, "diff_product_files", lambda p: ["src/a.py", "src/b.py"])
    monkeypatch.setattr(
        rp, "partition_slices", lambda *a, **k: {"verdict": "single-reviewer-ok", "slices": [], "error": None}
    )
    monkeypatch.setattr(rp, "foreign_claims", lambda *a, **k: [])
    return repo, base, head


def _range_prep(repo, base, head):
    return rp.review_prep(
        repo, base=base, head=head, paths=["src/a.py", "src/b.py"], slice_prefix="pre-r",
        plan_path="plan.md", session_id=SESSION,
    )


def test_range_snapshot_hashes_the_range_blob_not_a_later_worktree_edit(real_repo):
    repo, base, head = real_repo
    (repo / "src" / "a.py").write_bytes(b"a = 99  # edited after the range\n")
    (repo / "src" / "b.py").unlink()
    reply = _range_prep(repo, base, head)
    assert reply["error"] is None
    manifest = repo / reply["pre_review_manifest"]
    files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    assert files == {
        "src/a.py": hashlib.sha256(b"a = 2\n").hexdigest(),
        "src/b.py": hashlib.sha256(b"b = 2\n").hexdigest(),
    }
    assert (manifest.parent / files["src/a.py"]).read_bytes() == b"a = 2\n"


def test_range_snapshot_omits_a_path_absent_at_head(real_repo, monkeypatch):
    repo, base, head = real_repo
    (repo / "src" / "gone.py").write_bytes(b"x\n")
    monkeypatch.setattr(rp, "diff_product_files", lambda p: ["src/a.py", "src/gone.py"])
    reply = _range_prep(repo, base, head)
    files = json.loads((repo / reply["pre_review_manifest"]).read_text(encoding="utf-8"))["files"]
    assert set(files) == {"src/a.py"}


def test_range_snapshot_keeps_worktree_bytes_when_head_is_unreadable(real_repo):
    repo, base, _head = real_repo
    (repo / "src" / "a.py").write_bytes(b"a = 5\n")
    reply = _range_prep(repo, base, "f" * 40)
    files = json.loads((repo / reply["pre_review_manifest"]).read_text(encoding="utf-8"))["files"]
    assert files["src/a.py"] == hashlib.sha256(b"a = 5\n").hexdigest()


def test_worktree_mode_snapshot_still_hashes_worktree_bytes(real_repo):
    repo, base, head = real_repo
    (repo / "src" / "a.py").write_bytes(b"a = 7\n")
    reply = rp.review_prep(
        repo, base=base, paths=["src/a.py", "src/b.py"], slice_prefix="pre-w",
        plan_path="plan.md", session_id=SESSION,
    )
    files = json.loads((repo / reply["pre_review_manifest"]).read_text(encoding="utf-8"))["files"]
    assert files["src/a.py"] == hashlib.sha256(b"a = 7\n").hexdigest()
