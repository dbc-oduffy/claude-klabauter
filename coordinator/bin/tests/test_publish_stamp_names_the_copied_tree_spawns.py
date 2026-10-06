"""Real-git falsifier split out of test_publish_stamp_names_the_copied_tree.py: the mirror bytes
come from the pinned commit, not the HEAD that moved mid-round.

Run: python -m pytest coordinator/bin/tests/test_publish_stamp_names_the_copied_tree_spawns.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_stamp_names_copied_tree_spawns_under_test", "publish.py")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )
    return proc.stdout.strip()


def test_mirror_bytes_equal_pinned_commit_after_head_moves(monkeypatch, tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.invalid")
    _git(src, "config", "user.name", "t")
    _git(src, "config", "commit.gpgsign", "false")
    _git(src, "config", "core.autocrlf", "false")
    tracked = src / "coordinator_core" / "contract.txt"
    tracked.parent.mkdir()
    tracked.write_bytes(b"CONTRACT_VERSION 9.3.0\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "A")
    sha_a = _git(src, "rev-parse", "HEAD")

    monkeypatch.setattr(publish, "_REPO_ROOT", src)
    publish._engine_toplevel_key.cache_clear()
    pins: dict[str, str] = {}
    assert publish._round_pin_source_sha(src, pins) == sha_a

    tracked.write_bytes(b"CONTRACT_VERSION 9.4.0\n")
    _git(src, "commit", "-q", "-am", "B")
    sha_b = _git(src, "rev-parse", "HEAD")
    assert sha_a != sha_b

    publish._MATERIALIZED_REF_CACHE.clear()
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda _top, _sha: ("coordinator_core",))
    shadow = publish._git_materialize_ref(src, pins[next(iter(pins))])
    try:
        mirror = tmp_path / "mirror"
        dest_dir = mirror / "coordinator_core"
        dest_dir.mkdir(parents=True)
        _git(mirror, "init", "-q")
        _git(mirror, "config", "user.email", "t@example.invalid")
        _git(mirror, "config", "user.name", "t")
        _git(mirror, "config", "commit.gpgsign", "false")
        _git(mirror, "config", "core.autocrlf", "false")
        (dest_dir / "seed.txt").write_bytes(b"seed\n")
        _git(mirror, "add", "-A")
        _git(mirror, "commit", "-q", "-m", "seed")
        (dest_dir / "contract.txt").write_bytes((shadow / "coordinator_core" / "contract.txt").read_bytes())

        ok = publish._commit_published_dests(
            {mirror: {dest_dir}},
            succeeded_row_names=["row-a"],
            round_pinned_shas=pins,
            rows_feeding_root={mirror: frozenset({"row-a"})},
        )
        assert ok is True

        subject = _git(mirror, "log", "-1", "--format=%s")
        assert f"[source-head {sha_a[:12]}]" in subject
        assert sha_b[:12] not in subject
        committed = _git(mirror, "show", "HEAD:coordinator_core/contract.txt")
        assert committed == _git(src, "show", f"{sha_a}:coordinator_core/contract.txt")
        assert committed != _git(src, "show", f"{sha_b}:coordinator_core/contract.txt")
    finally:
        publish._cleanup_shadow_roots((shadow,))
        publish._MATERIALIZED_REF_CACHE.clear()
        publish._engine_toplevel_key.cache_clear()
