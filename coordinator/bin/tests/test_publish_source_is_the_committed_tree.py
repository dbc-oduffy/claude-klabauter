"""A publish ships the committed tree, never the working tree.

Builds a real git repo, commits, then dirties the working tree (edited,
untracked, deleted). The materialized source must carry the committed bytes
(CRLF kept exactly as committed) and, with `--source-ref` semantics, the bytes
of an older commit.

Run: python -m pytest coordinator/bin/tests/test_publish_source_is_the_committed_tree.py -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_source_is_committed_tree_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@claude-klabauter.test")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitattributes").write_bytes(b"* -text\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_bytes(b"VALUE = 1\n")
    (root / "pkg" / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (root / "run.sh").write_bytes(b"#!/bin/sh\necho one\n")
    (root / "gone.txt").write_bytes(b"committed\n")
    _git(root, "add", "-A")
    _git(root, "update-index", "--chmod=+x", "run.sh")
    _git(root, "commit", "-q", "-m", "one")
    first = _git(root, "rev-parse", "HEAD")
    (root / "pkg" / "mod.py").write_bytes(b"VALUE = 2\n")
    _git(root, "commit", "-q", "-am", "two")
    second = _git(root, "rev-parse", "HEAD")
    publish._MATERIALIZED_REF_CACHE.clear()
    return root, first, second


def _dirty(root: Path) -> None:
    (root / "pkg" / "mod.py").write_bytes(b"VALUE = 999  # uncommitted\n")
    (root / "pkg" / "crlf.txt").write_bytes(b"changed\n")
    (root / "untracked.txt").write_bytes(b"never committed\n")
    (root / "gone.txt").unlink()


def test_materialized_source_carries_committed_bytes_not_dirty_ones(repo):
    root, _first, _second = repo
    _dirty(root)

    shadow = publish._git_materialize_ref(root, ref="HEAD")

    assert (shadow / "pkg" / "mod.py").read_bytes() == b"VALUE = 2\n"
    assert (shadow / "pkg" / "crlf.txt").read_bytes() == b"a\r\nb\r\n"
    assert (shadow / "gone.txt").read_bytes() == b"committed\n"
    assert not (shadow / "untracked.txt").exists()
    publish._cleanup_shadow_roots((shadow,))


@pytest.mark.skipif(os.name == "nt", reason="extraction cannot set exec bits on Windows")
def test_materialized_source_preserves_executable_mode(repo):
    root, _first, _second = repo
    shadow = publish._git_materialize_ref(root, ref="HEAD")
    assert os.access(shadow / "run.sh", os.X_OK)
    assert not os.access(shadow / "pkg" / "mod.py", os.X_OK)
    publish._cleanup_shadow_roots((shadow,))


def test_source_ref_override_pins_the_named_commit(repo, monkeypatch):
    root, first, second = repo
    _dirty(root)
    pins: dict = {}

    monkeypatch.setattr(publish, "_source_ref_override", "")
    assert publish._round_pin_source_sha(root, pins, out=open(os.devnull, "w")) == second

    pins.clear()
    monkeypatch.setattr(publish, "_source_ref_override", first)
    pinned = publish._round_pin_source_sha(root, pins, out=open(os.devnull, "w"))
    assert pinned == first

    shadow = publish._git_materialize_ref(root, ref=pinned)
    assert (shadow / "pkg" / "mod.py").read_bytes() == b"VALUE = 1\n"
    publish._cleanup_shadow_roots((shadow,))


def test_unresolvable_source_ref_refuses_rather_than_falling_back(repo, monkeypatch):
    root, _first, _second = repo
    monkeypatch.setattr(publish, "_source_ref_override", "no-such-ref")
    with pytest.raises(publish.GitMaterializeError):
        publish._round_pin_source_sha(root, {}, out=open(os.devnull, "w"))
