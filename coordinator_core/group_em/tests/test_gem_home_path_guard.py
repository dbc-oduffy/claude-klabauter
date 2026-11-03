"""Tests for the G-EM home absolute-path guard and its pre-commit hook."""

from __future__ import annotations

import subprocess
import sys

from coordinator_core.group_em import gem_home_path_guard as g

# Fixtures are assembled from fragments so no literal machine path sits in this source.
_DRIVE_BS = "C" + ":" + "\\" + "Users" + "\\" + "x" + "\\" + "a.md"
_DRIVE_FS = "e" + ":" + "/" + "proj/x"
_MAC = "/" + "Users" + "/bob/f"
_LINUX = "/" + "home" + "/al/f"
_NO_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, creationflags=_NO_FLAGS)


def _run_hook(hook, cwd):
    return subprocess.run(
        [sys.executable, str(hook)], cwd=cwd, capture_output=True, text=True, creationflags=_NO_FLAGS
    )


def test_scan_flags_machine_paths_and_passes_clean(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text(f"see {_DRIVE_BS}\nalso {_DRIVE_FS}\nmac {_MAC}\nlinux {_LINUX}\n", encoding="utf-8")
    ok = tmp_path / "ok.md"
    ok.write_text("https://example.com/a and ~/home/x and $HOME/Users/y\nrel/path/here\n", encoding="utf-8")
    hits = g.scan([bad, ok])
    assert [h[1] for h in hits] == [1, 2, 3, 4]
    assert all(h[0] == str(bad) for h in hits)
    assert hits[0][2] == _DRIVE_BS
    assert g.scan([ok]) == []


def test_scan_skips_binary_and_missing(tmp_path):
    b = tmp_path / "b.bin"
    b.write_bytes(_DRIVE_BS.encode() + b"\x00")
    assert g.scan([b, tmp_path / "nope"]) == []


def test_install_refuses_non_repo_and_foreign_hook(tmp_path):
    assert g.install(tmp_path) is None
    _git(tmp_path, "init", "-q")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert g.install(tmp_path) is None
    assert "mine" in hook.read_text(encoding="utf-8")


def test_installed_hook_is_python_advisory_and_never_denies(tmp_path):
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
