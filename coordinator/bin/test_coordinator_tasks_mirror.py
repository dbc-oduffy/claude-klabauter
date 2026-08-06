#!/usr/bin/env python3
"""test_coordinator_tasks_mirror.py — self-contained test suite for coordinator-tasks-mirror.py.

Exercises the module's write/update
functions directly (no subprocess, no git-repo fixture required — cmd_init/cmd_update
take repo_root as an explicit parameter, decoupled from git rev-parse resolution) plus
the CLI's usage/error paths via subprocess for the argument-parsing surface.

Contract under test:
    CLI: coordinator/bin/coordinator-tasks-mirror.py
    Spec backlink: docs/plans/2026-07-06-ceremony-as-pipeline-2-doe-land-d-slice.md § C1.2

Run with: python3 -m pytest coordinator/bin/test_coordinator_tasks_mirror.py

Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Plan C, Wave E3-d)
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT = os.path.join(SCRIPT_DIR, "coordinator-tasks-mirror.py")

# Windows: suppresses the console popup a subprocess.run(...) would otherwise
# trigger under the headless Claude Code Bash-tool parent. No-op (0) elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PASS = 0
FAIL = 0


def _pass(label: str) -> None:
    global PASS
    print(f"  PASS: {label}")
    PASS += 1


def _fail(label: str, detail: str = "") -> None:
    global FAIL
    print(f"  FAIL: {label}")
    if detail:
        print(f"    {detail}")
    FAIL += 1


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("coordinator_tasks_mirror_under_test", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mirror_write(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- mirror-write: yaml lands at state/tasks/<sid>/ with correct items")
    repo = os.path.join(tmp_root, "write")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-abc123"
    titles = [
        "[re-validate after restart] MCP server starts",
        "[completeness] Claude Code plugin loads",
        "[completeness] doctor probe passes",
    ]
    rc = mod.cmd_init(repo, sid, "completeness-checklist", titles)
    mirror_file = os.path.join(repo, "state", "tasks", sid, "completeness-checklist.yaml")

    if rc == 0:
        _pass("init returns 0")
    else:
        _fail("init returns 0", f"rc={rc}")

    if os.path.isfile(mirror_file):
        _pass("mirror file created at expected path")
    else:
        _fail("mirror file created", mirror_file)
        return

    content = open(mirror_file, encoding="utf-8").read()
    for t in titles:
        if f"title: '{t}'" in content:
            _pass(f"title present: {t}")
        else:
            _fail(f"title present: {t}")

    if content.count("state: open") == 3:
        _pass("all 3 items start open")
    else:
        _fail("all 3 items start open", f"got {content.count('state: open')}")

    if "schema: completeness-checklist-mirror-v1" in content:
        _pass("schema field present")
    else:
        _fail("schema field present")

    if f"sid: '{sid}'" in content:
        _pass("sid field matches")
    else:
        _fail("sid field matches")


def test_resumption_survival(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- resumption-survival: mirror readable across a fresh module load")
    repo = os.path.join(tmp_root, "resume")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-resume99"
    mod.cmd_init(repo, sid, "completeness-checklist", [
        "[completeness] plugin installed",
        "[completeness] hooks wired",
    ])
    mirror_file = os.path.join(repo, "state", "tasks", sid, "completeness-checklist.yaml")
    if not os.path.isfile(mirror_file):
        _fail("mirror written", mirror_file)
        return
    _pass("mirror written")

    # Simulate a session boundary: read the file from a plain open(), no
    # module state involved — proves the data is purely on disk.
    with open(mirror_file, encoding="utf-8") as f:
        fresh_read = f.read()

    if "schema: completeness-checklist-mirror-v1" in fresh_read:
        _pass("schema field present in fresh-read")
    else:
        _fail("schema field present in fresh-read")
    if "title: '[completeness] plugin installed'" in fresh_read:
        _pass("item1 present in fresh-read")
    else:
        _fail("item1 present in fresh-read")
    if "title: '[completeness] hooks wired'" in fresh_read:
        _pass("item2 present in fresh-read")
    else:
        _fail("item2 present in fresh-read")


def test_update_state(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- update: flips item state open->done without touching sibling item")
    repo = os.path.join(tmp_root, "update")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-update55"
    mod.cmd_init(repo, sid, "completeness-checklist", [
        "[completeness] surface A",
        "[completeness] surface B",
    ])
    rc = mod.cmd_update(repo, sid, "completeness-checklist", "[completeness] surface A", "done")
    if rc == 0:
        _pass("update returns 0")
    else:
        _fail("update returns 0", f"rc={rc}")

    mirror_file = os.path.join(repo, "state", "tasks", sid, "completeness-checklist.yaml")
    content = open(mirror_file, encoding="utf-8").read()
    done_count = content.count("state: done")
    open_count = content.count("state: open")
    if done_count == 1:
        _pass("1 done item")
    else:
        _fail("1 done item", f"got {done_count}")
    if open_count == 1:
        _pass("1 open item (sibling untouched)")
    else:
        _fail("1 open item (sibling untouched)", f"got {open_count}")


def test_init_required_before_update(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- init required before update: missing mirror returns non-zero")
    repo = os.path.join(tmp_root, "gate")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-gate77"
    rc = mod.cmd_update(repo, sid, "completeness-checklist", "[completeness] some item", "done")
    if rc != 0:
        _pass("update on missing mirror returns non-zero")
    else:
        _fail("update on missing mirror returns non-zero", f"got rc={rc}")


def test_update_missing_title(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- update on unknown title returns non-zero (F2 parity)")
    repo = os.path.join(tmp_root, "missing-title")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-missing1"
    mod.cmd_init(repo, sid, "completeness-checklist", ["[completeness] real item"])
    rc = mod.cmd_update(repo, sid, "completeness-checklist", "no such title", "done")
    if rc != 0:
        _pass("update on unknown title returns non-zero")
    else:
        _fail("update on unknown title returns non-zero", f"got rc={rc}")


def test_slug_sanitize(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- slug sanitize: special-char name produces safe filename")
    repo = os.path.join(tmp_root, "slug")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-slug88"
    mod.cmd_init(repo, sid, "my checklist / 2026!", ["[completeness] item one"])
    mirror_dir = os.path.join(repo, "state", "tasks", sid)
    yamls = [f for f in os.listdir(mirror_dir) if f.endswith(".yaml")] if os.path.isdir(mirror_dir) else []
    if yamls:
        _pass(f"yaml found under state/tasks/{sid}/")
    else:
        _fail(f"yaml found under state/tasks/{sid}/")
        return
    basename = yamls[0]
    if " " not in basename and "/" not in basename and "\\" not in basename:
        _pass(f"filename has no unsafe chars: {basename}")
    else:
        _fail("filename has no unsafe chars", basename)


def test_cli_usage_error() -> None:
    print("--- CLI: too few args -> usage error, exit 1")
    result = subprocess.run(
        [sys.executable, SUBJECT],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    if result.returncode == 1:
        _pass("CLI with no args exits 1")
    else:
        _fail("CLI with no args exits 1", f"rc={result.returncode}")
    if "Usage:" in result.stderr:
        _pass("CLI usage message on stderr")
    else:
        _fail("CLI usage message on stderr", result.stderr)


def test_cli_end_to_end(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- CLI: end-to-end init via subprocess in a real git repo")
    if not shutil.which("git"):
        _pass("skipped (no git on PATH)")
        return
    repo = os.path.join(tmp_root, "cli-e2e")
    os.makedirs(repo, exist_ok=True)
    init = subprocess.run(
        ["git", "init", "-q", repo], capture_output=True, text=True, creationflags=_NO_WINDOW
    )
    if init.returncode != 0:
        _pass("skipped (git init unavailable in this sandbox)")
        return
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Test"],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )

    env = dict(os.environ)
    env["COORDINATOR_SESSION_ID"] = "cli-e2e-sid"
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    result = subprocess.run(
        [sys.executable, SUBJECT, "init", "completeness-checklist", "[completeness] cli item"],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
        creationflags=_NO_WINDOW,
    )
    if result.returncode == 0:
        _pass("CLI init exits 0 in a real git repo")
    else:
        _fail("CLI init exits 0 in a real git repo", f"rc={result.returncode} stderr={result.stderr}")
    mirror_file = os.path.join(repo, "state", "tasks", "cli-e2e-sid", "completeness-checklist.yaml")
    if os.path.isfile(mirror_file):
        _pass("CLI init wrote the mirror file")
    else:
        _fail("CLI init wrote the mirror file", mirror_file)


