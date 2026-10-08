"""The DR-445 throwaway route commits `.coordinator/expected-manifest.json` against a real dest repo.

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


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _dr445_round(tmp_path, *, rows_feeding):
    """Real dest repo + `git clone` throwaway driven through `_swap_all_rows_into_dest` (the DR-445 route)."""
    dest = tmp_path / "dest-repo"
    dest.mkdir()
    _git(dest, "init", "-b", "main")
    for k, v in (("user.email", "t@t.test"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(dest, "config", k, v)
    (dest / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "seed")
    throwaway = tmp_path / "throwaway"
    _git(tmp_path, "clone", "--local", str(dest), str(throwaway))
    (throwaway / "sub").mkdir()
    (throwaway / "sub" / "b.txt").write_bytes(b"hello\n")
    target = type("T", (), {"name": "row-a", "dest_dir": dest})()
    staged = type("S", (), {"staging_dir": tmp_path / "unused"})()
    outcomes = publish._swap_all_rows_into_dest(
        {dest: [(target, staged)]},
        {dest: throwaway},
        commit_now=True,
        succeeded_row_names=["row-a"],
        round_pinned_shas={str(publish._REPO_ROOT): base._PINNED},
        rows_feeding_root={dest: rows_feeding},
    )
    assert outcomes == {"row-a": None}
    return dest


def test_dr445_round_commits_the_manifest(tmp_path):
    dest = _dr445_round(tmp_path, rows_feeding=frozenset({"row-a"}))
    assert ".coordinator/expected-manifest.json" in _git(dest, "ls-files").split()
    doc = json.loads(_git(dest, "show", "HEAD:.coordinator/expected-manifest.json"))
    assert set(doc) == {"schema", "source_head", "paths"}
    assert doc["source_head"] == base._PINNED
    assert doc["paths"]["sub/b.txt"] == _blob(b"hello\n")
    assert ".coordinator/expected-manifest.json" not in doc["paths"]
    assert _git(dest, "status", "--porcelain") == ""


def test_dr445_partial_root_commits_no_manifest(tmp_path):
    dest = _dr445_round(tmp_path, rows_feeding=frozenset({"row-a", "row-b"}))
    assert ".coordinator/expected-manifest.json" not in _git(dest, "ls-files").split()


def test_dr445_manifest_is_the_committed_tree_with_written_byte_shas(tmp_path):
    dest = _dr445_round(tmp_path, rows_feeding=frozenset({"row-a"}))
    doc = json.loads(_git(dest, "show", "HEAD:.coordinator/expected-manifest.json"))
    committed = {
        row.partition("\t")[2]: row.split()[2]
        for row in _git(dest, "ls-tree", "-r", "HEAD").splitlines()
    }
    committed.pop(".coordinator/expected-manifest.json")
    assert doc["paths"] == committed
    assert doc["paths"]["seed.txt"] == _blob(b"seed\n")
