"""Tests for the G-EM home absolute-path guard and its pre-commit hook."""

from __future__ import annotations

import subprocess
import sys

from coordinator_core.group_em import gem_home_path_guard as g
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

if TYPE_CHECKING:
    from subprocess import CompletedProcess

# Fixtures are assembled from fragments so no literal machine path sits in this source.
_DRIVE_BS = "C" + ":" + "\\" + "Users" + "\\" + "x" + "\\" + "a.md"
_DRIVE_FS = "e" + ":" + "/" + "proj/x"
_MAC = "/" + "Users" + "/bob/f"
_LINUX = "/" + "home" + "/al/f"
_NO_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(cwd, *args) -> CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, creationflags=_NO_FLAGS)


def _run_hook(hook, cwd) -> CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(hook)], cwd=cwd, capture_output=True, text=True, creationflags=_NO_FLAGS
    )


def test_install_refuses_non_repo_and_foreign_hook(tmp_path: Path) -> None:
    assert g.install(tmp_path) is None
    _git(tmp_path, "init", "-q")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert g.install(tmp_path) is None
    assert "mine" in hook.read_text(encoding="utf-8")


def test_installed_hook_is_python_advisory_and_never_denies(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    hook = g.install(tmp_path)
    assert hook is not None and hook.name == "pre-commit"
    src = hook.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    assert "bash" not in src
    assert g.install(tmp_path) == hook

    (tmp_path / "n.md").write_text(f"root {_DRIVE_BS}\n", encoding="utf-8")
    _git(tmp_path, "add", "n.md")
    r = _run_hook(hook, tmp_path)
    assert r.returncode == 0
    assert "n.md:1" in r.stderr and _DRIVE_BS in r.stderr

    (tmp_path / "n.md").write_text("clean\n", encoding="utf-8")
    _git(tmp_path, "add", "n.md")
    r = _run_hook(hook, tmp_path)
    assert r.returncode == 0 and r.stderr == ""
