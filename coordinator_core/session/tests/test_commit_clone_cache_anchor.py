"""Publish memos survive the per-sha commit clone: `durable_cache_dir` resolves to the source repo."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from coordinator_core.session import machinery_paths as mp

_COMMIT_CLONE = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "lib" / "commit_clone.py"


def _commit_clone():
    spec = importlib.util.spec_from_file_location("_commit_clone_under_test", _COMMIT_CLONE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_marker_names_match_the_clone_writer():
    cc = _commit_clone()
    assert mp.COMMIT_CLONE_MARKERS == (cc._MARKER, cc._LEGACY_MARKER)


def test_a_plain_repo_keeps_its_own_cache_dir(tmp_path):
    assert mp.durable_cache_dir(str(tmp_path)) == mp.cache_dir(str(tmp_path))


def test_a_commit_clone_resolves_to_its_source_repo(tmp_path):
    source, clone = tmp_path / "source", tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    source.mkdir()
    (clone / ".git" / mp.COMMIT_CLONE_MARKERS[0]).write_text(
        json.dumps({"sha": "abc", "source_root": str(source), "format": 2}), encoding="utf-8"
    )
    assert mp.durable_cache_dir(str(clone)) == mp.cache_dir(str(source))
    assert os.path.normcase(mp.durable_cache_dir(str(clone))) != os.path.normcase(mp.cache_dir(str(clone)))


def test_an_unreadable_marker_falls_back_to_the_clone(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / mp.COMMIT_CLONE_MARKERS[0]).write_text("not json", encoding="utf-8")
    assert mp.durable_cache_dir(str(tmp_path)) == mp.cache_dir(str(tmp_path))
