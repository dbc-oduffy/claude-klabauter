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


def test_range_mode_writes_no_snapshot(tmp_path, prep):
    reply = prep(head="b" * 40)
    assert reply["error"] is None
    assert "pre_review_manifest" not in reply
    assert not list(tmp_path.rglob("*.pre-review"))


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
