"""A fully published mirror root gets `.coordinator/expected-manifest.json` staged into the round's commit.

Run: python -m pytest coordinator/bin/tests/test_publish_writes_expected_manifest.py -q
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_publish_stamp_names_the_copied_tree as base  # noqa: E402

publish = base.publish


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _run(monkeypatch, tmp_path, *, rows_feeding):
    from coordinator_core.git import commit as commit_mod

    seen = base._stub_commit_surface(monkeypatch)
    captured: dict = {}

    def _fake(root, paths, message, **kwargs):
        captured["paths"] = list(paths)
        return commit_mod.CommitOutcome(sha="a" * 40, staged_preferred=(), worktree_over_staged=())

    monkeypatch.setattr(commit_mod, "commit_paths", _fake)
    repo_root, dest_dir = base._dest(tmp_path)
    (dest_dir / "a.py").write_bytes(b"print(1)\n")
    (dest_dir / "sub").mkdir()
    (dest_dir / "sub" / "b.txt").write_bytes(b"")
    ok = publish._commit_published_dests(
        {repo_root: {dest_dir}},
        succeeded_row_names=["row-a"],
        round_pinned_shas={str(publish._REPO_ROOT): base._PINNED},
        rows_feeding_root={repo_root: rows_feeding},
    )
    assert ok is True
    return repo_root, captured, seen


def test_manifest_written_with_blob_shas_and_pinned_head(monkeypatch, tmp_path):
    repo_root, captured, _ = _run(monkeypatch, tmp_path, rows_feeding=frozenset({"row-a"}))
    raw = (repo_root / ".coordinator" / "expected-manifest.json").read_bytes()
    assert b"\r" not in raw
    doc = json.loads(raw)
    assert set(doc) == {"schema", "source_head", "paths"}
    assert doc["schema"] == 1
    assert doc["source_head"] == base._PINNED != base._LIVE
    assert doc["paths"] == {
        "coordinator_core/a.py": _blob(b"print(1)\n"),
        "coordinator_core/sub/b.txt": _blob(b""),
    }
    assert ".coordinator/expected-manifest.json" not in doc["paths"]
    assert list(doc) == sorted(doc)
    assert ".coordinator/expected-manifest.json" in captured["paths"]


def test_partial_root_gets_no_manifest(monkeypatch, tmp_path):
    repo_root, captured, _ = _run(monkeypatch, tmp_path, rows_feeding=frozenset({"row-a", "row-b"}))
    assert not (repo_root / ".coordinator").exists()
    assert ".coordinator/expected-manifest.json" not in captured["paths"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
