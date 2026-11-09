"""Explicit originless scratch publication retains local baseline gates, without remote refresh."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PATH = Path(__file__).resolve().parents[1] / "publish.py"
_SPEC = importlib.util.spec_from_file_location("publish_originless_scratch_refresh", _PATH)
publish = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = publish
_SPEC.loader.exec_module(publish)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    live, scratch, unrelated = [tmp_path / name for name in ("live", "scratch", "unrelated")]
    for root in (live, scratch, unrelated):
        root.mkdir()
        git(root, "init", "-b", "main")
    # The scratch has an explicit local upstream; the patch must preserve it.
    git(scratch, "config", "branch.main.remote", ".")
    git(scratch, "config", "branch.main.merge", "refs/heads/baseline")
    monkeypatch.setattr(publish, "_DEST_ROOT_OVERRIDE", publish.DestRootOverride.of(live, scratch))
    return live, scratch, unrelated


def test_only_explicit_originless_scratch_skips_remote_refresh(repos):
    live, scratch, unrelated = repos
    assert publish._skip_remote_refresh_for_originless_scratch(scratch) is True
    assert publish._skip_remote_refresh_for_originless_scratch(live) is False
    assert publish._skip_remote_refresh_for_originless_scratch(unrelated) is False
    assert git(scratch, "config", "branch.main.remote") == "."
    assert git(scratch, "config", "branch.main.merge") == "refs/heads/baseline"


def test_origin_bearing_scratch_keeps_normal_refresh(repos):
    live, scratch, _ = repos
    git(scratch, "remote", "add", "origin", str(live))
    assert publish._skip_remote_refresh_for_originless_scratch(scratch) is False


def test_empty_configured_origin_is_not_absent(repos):
    _, scratch, _ = repos
    git(scratch, "config", "remote.origin.url", "")
    assert publish._skip_remote_refresh_for_originless_scratch(scratch) is False


def test_no_override_never_skips(repos, monkeypatch):
    _, scratch, _ = repos
    monkeypatch.setattr(publish, "_DEST_ROOT_OVERRIDE", None)
    assert publish._skip_remote_refresh_for_originless_scratch(scratch) is False


def test_same_resolved_live_and_override_cannot_bypass(repos, monkeypatch):
    live, _, _ = repos
    # Defend even against a manually constructed override, bypassing .of validation.
    monkeypatch.setattr(publish, "_DEST_ROOT_OVERRIDE", SimpleNamespace(clone_root=live / ".", live_root=live))
    assert publish._skip_remote_refresh_for_originless_scratch(live) is False


def test_subdirectory_and_nonrepo_cannot_bypass(repos, tmp_path, monkeypatch):
    live, scratch, _ = repos
    inner = scratch / "inner"
    inner.mkdir()
    monkeypatch.setattr(publish, "_DEST_ROOT_OVERRIDE", SimpleNamespace(clone_root=inner, live_root=live))
    assert publish._skip_remote_refresh_for_originless_scratch(inner) is False
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(publish, "_DEST_ROOT_OVERRIDE", SimpleNamespace(clone_root=plain, live_root=live))
    assert publish._skip_remote_refresh_for_originless_scratch(plain) is False


def test_unreadable_git_config_does_not_skip(repos):
    _, scratch, _ = repos
    with (scratch / ".git/config").open("a") as stream:
        stream.write("\n[broken section\n")
    assert publish._skip_remote_refresh_for_originless_scratch(scratch) is False
