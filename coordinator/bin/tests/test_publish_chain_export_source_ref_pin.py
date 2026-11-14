"""A publish.py running from percolate-round's chain export must pin `--source-ref`, not HEAD.

The export's own toplevel is the export dir; the round's pin key is the source repo's toplevel.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_chain_export_pin_under_test", "publish.py")


def _git(*args, cwd):
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _repo_with_two_commits(path: Path) -> "tuple[str, str]":
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    (path / "a.txt").write_text("1\n", encoding="utf-8")
    _git("add", "a.txt", cwd=path)
    _git("commit", "-q", "-m", "first", cwd=path)
    first = _git("rev-parse", "HEAD", cwd=path)
    (path / "a.txt").write_text("2\n", encoding="utf-8")
    _git("commit", "-q", "-am", "second", cwd=path)
    return first, _git("rev-parse", "HEAD", cwd=path)


@pytest.fixture
def chain_export(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    ref, head = _repo_with_two_commits(repo)
    assert ref != head
    export = tmp_path / "chain" / ref
    export.mkdir(parents=True)
    (export / ".git").write_text(f"gitdir: {(repo / '.git').as_posix()}\n", encoding="utf-8")
    monkeypatch.setattr(publish, "_REPO_ROOT", export)
    monkeypatch.setattr(publish, "_source_ref_override", ref)
    monkeypatch.setattr(publish, "_bootstrap_engine", lambda: None)
    publish._engine_toplevel_key.cache_clear()
    yield repo, ref, head
    publish._engine_toplevel_key.cache_clear()


def test_engine_key_of_a_chain_export_is_the_source_repo(chain_export):
    repo, _ref, _head = chain_export
    assert Path(publish._engine_toplevel_key()) == repo


def test_source_ref_pins_over_a_differing_head_from_the_export(chain_export):
    repo, ref, head = chain_export
    pins: "dict[str, str]" = {}
    got = publish._round_pin_source_sha(repo, pins)
    assert got == ref != head


def test_linked_worktree_gitdir_keeps_its_own_key(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    private = tmp_path / "main" / ".git" / "worktrees" / "wt"
    private.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {private.as_posix()}\n", encoding="utf-8")
    assert publish._chain_export_source_repo(wt) is None


def test_chain_export_carries_the_files_publish_loads_beside_itself():
    rnd = _load("percolate_round_chain_paths_under_test", "percolate-round.py")
    src = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    sibling_names = set(re.findall(r'Path\(__file__\)\.resolve\(\)\.parent / "([^"/]+)"', src))
    exported = {Path(p).name for p in rnd._CHAIN_EXPORT_PATHS if p.startswith("coordinator/bin/")}
    # publish-allowlist-generate.py etc. are exported; `lib/...` is a directory entry.
    missing = sorted(n for n in sibling_names if n not in exported and "." in n)
    assert missing == []
