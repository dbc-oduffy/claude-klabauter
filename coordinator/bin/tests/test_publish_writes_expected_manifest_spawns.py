"""The diff-scaled round's `.coordinator/expected-manifest.json` is built from a real dest repo's HEAD.

Run: python -m pytest coordinator/bin/tests/test_publish_writes_expected_manifest_spawns.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_publish_stamp_names_the_copied_tree as base  # noqa: E402
from test_publish_writes_expected_manifest import _blob  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

publish = base.publish
_MANIFEST_REL = ".coordinator/expected-manifest.json"


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _dest_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "dest-repo"
    dest.mkdir()
    _git(dest, "init", "-b", "main")
    for k, v in (("user.email", "t@t.test"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(dest, "config", k, v)
    (dest / "seed.txt").write_text("seed\n", encoding="utf-8")
    (dest / "gone.txt").write_text("gone\n", encoding="utf-8")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "seed")
    return dest


def _write(rel: str, data: bytes):
    publish._bootstrap_engine()
    from percolate.diff_commit import DestWrite  # type: ignore[import-not-found]

    return DestWrite(rel, data, publish._GIT_FILE_MODE)


def test_cold_manifest_is_the_head_tree_plus_the_round_writes(tmp_path):
    dest = _dest_repo(tmp_path)
    survivors = {"sub/b.txt": _write("sub/b.txt", b"hello\n")}

    raw = publish._expected_manifest_bytes(
        dest, survivors, ["gone.txt"], base._PINNED, head_paths=publish._head_tree_blob_shas(dest)
    )

    assert raw is not None and b"\r" not in raw
    doc = json.loads(raw)
    assert set(doc) == {"schema", "source_head", "paths"}
    assert doc["source_head"] == base._PINNED
    assert doc["paths"] == {"seed.txt": _blob(b"seed\n"), "sub/b.txt": _blob(b"hello\n")}
    assert _MANIFEST_REL not in doc["paths"]
    assert list(doc) == sorted(doc)


def test_warm_manifest_updates_the_committed_copy_by_the_round_writes(tmp_path):
    dest = _dest_repo(tmp_path)
    cold = publish._expected_manifest_bytes(
        dest, {}, [], base._PINNED, head_paths=publish._head_tree_blob_shas(dest)
    )
    (dest / ".coordinator").mkdir()
    (dest / _MANIFEST_REL).write_bytes(cold)
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "manifest")

    warm = publish._expected_manifest_bytes(
        dest, {"seed.txt": _write("seed.txt", b"changed\n")}, ["gone.txt"], "f" * 40, head_paths=None
    )

    doc = json.loads(warm)
    assert doc["source_head"] == "f" * 40
    assert doc["paths"] == {"seed.txt": _blob(b"changed\n")}


def test_manifest_is_the_committed_tree_with_written_byte_shas(tmp_path):
    dest = _dest_repo(tmp_path)

    doc = json.loads(
        publish._expected_manifest_bytes(
            dest, {}, [], base._PINNED, head_paths=publish._head_tree_blob_shas(dest)
        )
    )

    committed = {
        row.partition("\t")[2]: row.split()[2]
        for row in _git(dest, "ls-tree", "-r", "HEAD").splitlines()
    }
    assert doc["paths"] == committed
    assert doc["paths"]["seed.txt"] == _blob(b"seed\n")


def test_a_partially_published_root_gets_no_manifest(tmp_path):
    dest = _dest_repo(tmp_path)

    assert publish._root_fully_published(dest, ["row-a"], {dest: frozenset({"row-a"})})
    assert not publish._root_fully_published(dest, ["row-a"], {dest: frozenset({"row-a", "row-b"})})
