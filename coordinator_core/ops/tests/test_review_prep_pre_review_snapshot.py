"""review.prep: the write-once pre-review snapshot of the product bytes it hashes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp

SESSION = "sess-snap"
BASE = "a" * 40


@pytest.fixture
def prep(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(rp, "run_git", boom)
    monkeypatch.setattr(rp, "freeze_diff", lambda *a, **k: {"diff_path": str(tmp_path / "w.diff"), "error": None})
    monkeypatch.setattr(rp, "diff_product_files", lambda p: ["src/a.py", "src/b.py"])
    monkeypatch.setattr(
        rp, "partition_slices", lambda *a, **k: {"verdict": "single-reviewer-ok", "slices": [], "error": None}
    )
    monkeypatch.setattr(rp, "foreign_claims", lambda *a, **k: [])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"a = 1\n")
    (tmp_path / "src" / "b.py").write_bytes(b"b = 1\n")
    (tmp_path / "plan.md").write_text("---\nplan_id: p\n---\n", encoding="utf-8")

    def call(prefix="pre-t", **kw):
        return rp.review_prep(
            tmp_path, base=BASE, paths=["src/a.py", "src/b.py"], slice_prefix=prefix,
            plan_path="plan.md", session_id=SESSION, **kw,
        )

    return call


def _manifest(tmp_path: Path, reply: dict) -> Path:
    return tmp_path / reply["pre_review_manifest"]


def test_first_call_writes_manifest_and_blobs_equal_to_the_files(tmp_path, prep):
    reply = prep()
    assert reply["error"] is None
    manifest = _manifest(tmp_path, reply)
    record = json.loads(manifest.read_text(encoding="utf-8"))
    assert record["base"] == BASE
    assert record["paths"] == ["src/a.py", "src/b.py"]
    assert set(record["files"]) == {"src/a.py", "src/b.py"}
    for rel, digest in record["files"].items():
        data = (tmp_path / rel).read_bytes()
        assert digest == hashlib.sha256(data).hexdigest() == reply["reviewed_sha256"][rel]
        assert (manifest.parent / digest).read_bytes() == data


def test_recompute_after_review_edit_leaves_the_manifest_untouched(tmp_path, prep):
    first = prep()
    manifest = _manifest(tmp_path, first)
    before = manifest.read_bytes()
    (tmp_path / "src" / "a.py").write_bytes(b"a = 2  # review edit\n")
    second = prep()
    assert second["reviewed_sha256"]["src/a.py"] == hashlib.sha256(b"a = 2  # review edit\n").hexdigest()
    assert manifest.read_bytes() == before
    old = json.loads(before)["files"]["src/a.py"]
    assert (manifest.parent / old).read_bytes() == b"a = 1\n"
    assert second["pre_review_manifest"] == first["pre_review_manifest"]


def test_served_reply_still_names_the_manifest(tmp_path, prep):
    first = prep()
    again = prep()
    assert again["pre_review_manifest"] == first["pre_review_manifest"]


def test_range_mode_writes_the_snapshot_review_reverify_reads(tmp_path, prep):
    reply = prep(head="b" * 40)
    assert reply["error"] is None
    record = json.loads(_manifest(tmp_path, reply).read_text(encoding="utf-8"))
    assert record["base"] == BASE
    assert set(record["files"]) == {"src/a.py", "src/b.py"}


def test_range_mode_snapshot_is_found_by_review_reverify(tmp_path, prep):
    from coordinator_core.ops import review_reverify as rr

    reply = prep(head="b" * 40)
    snapshot_dir = _manifest(tmp_path, reply).parent
    assert rr._read_manifest(snapshot_dir) is not None


def test_served_range_reply_still_names_the_manifest(tmp_path, prep):
    first = prep(head="b" * 40)
    again = prep(head="b" * 40)
    assert again["pre_review_manifest"] == first["pre_review_manifest"]


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


def test_manifest_is_created_exclusively(tmp_path, prep):
    first = prep()
    manifest = _manifest(tmp_path, first)
    manifest.write_text("sentinel", encoding="utf-8")
    prep(prefix="pre-t")
    assert manifest.read_text(encoding="utf-8") == "sentinel"


def test_manifest_head_is_the_repo_head_sha(tmp_path, prep):
    sha = "c" * 40
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    record = json.loads(_manifest(tmp_path, prep()).read_text(encoding="utf-8"))
    assert record["head"] == sha


def test_manifest_head_is_null_outside_a_git_repo(tmp_path, prep):
    record = json.loads(_manifest(tmp_path, prep()).read_text(encoding="utf-8"))
    assert record["head"] is None
