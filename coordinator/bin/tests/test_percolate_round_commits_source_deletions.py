"""A source-side deletion a prior sync already removed from dest's disk is
named for removal by `_pathspec_from_manifest`, committed, and counted.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_commits_source_deletions.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_ID = ["-c", "user.email=a@b.c", "-c", "user.name=a"]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_commits_source_deletions", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **_NO_WINDOW)


def _seed(root: Path, files: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(["add", "-A"], root)
    _git([*_ID, "commit", "-q", "-m", "seed"], root)


def _fixture(tmp_path):
    files = {"row_a/keep.txt": "k", "row_a/gone.txt": "g", "row_a/residue.txt": "r"}
    source, dest = tmp_path / "source", tmp_path / "dest"
    _seed(source, files)
    _seed(dest, files)
    # source deletes gone.txt in history; the sync already removed it from dest disk
    (source / "row_a" / "gone.txt").unlink()
    _git(["add", "-A"], source)
    _git([*_ID, "commit", "-q", "-m", "delete"], source)
    (dest / "row_a" / "gone.txt").unlink()
    return source, dest


def _manifest(**kw):
    base = dict(
        round_id="r1",
        declared_payload=frozenset({"row_a/keep.txt"}),
        published_dest_dirs=frozenset({"row_a"}),
    )
    base.update(kw)
    return _mod._RoundManifest(**base)


def test_source_deleted_path_gone_from_dest_disk_is_committed_and_counted(tmp_path):
    source, dest = _fixture(tmp_path)
    # dest-local residue: tracked, gone from dest disk, still alive... never in source history as deleted
    (dest / "row_a" / "residue.txt").unlink()
    (source / "row_a" / "residue.txt").write_text("r")  # source still has it (dirty live file)

    pathspec, _ = _mod._pathspec_from_manifest(_manifest(), str(dest), source)
    assert [Path(p).name for p in pathspec] == ["gone.txt"]

    deletion_paths = _mod._partition_pathspec_for_commit(
        pathspec, str(dest), _mod._dest_head_tree(str(dest))
    )[1]
    subject = _mod._build_commit_subject(
        "t", [], pathspec, deletion_paths=deletion_paths, source_sha="", dest_name="dest",
        unchanged_paths=[], explained_paths=[],
    )
    assert "1 removed" in subject

    _git(["add", "--", *pathspec], dest)
    _git([*_ID, "commit", "-q", "-m", "round"], dest)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(dest), capture_output=True, text=True, **_NO_WINDOW
    ).stdout
    assert "gone.txt" not in status
    assert "residue.txt" in status  # untouched: no source-side deletion proof


def test_without_source_root_the_old_behaviour_holds(tmp_path):
    _source, dest = _fixture(tmp_path)
    pathspec, _ = _mod._pathspec_from_manifest(_manifest(), str(dest))
    assert pathspec == []


def test_path_outside_published_scope_is_never_named(tmp_path):
    source, dest = _fixture(tmp_path)
    pathspec, _ = _mod._pathspec_from_manifest(
        _manifest(published_dest_dirs=frozenset({"other_row"})), str(dest), source
    )
    assert pathspec == []


def test_live_source_path_is_never_named(tmp_path):
    source, dest = _fixture(tmp_path)
    (source / "row_a" / "gone.txt").write_text("back")
    pathspec, _ = _mod._pathspec_from_manifest(_manifest(), str(dest), source)
    assert pathspec == []
