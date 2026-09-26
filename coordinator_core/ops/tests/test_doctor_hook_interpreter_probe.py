"""Tests for the doctor's hook-command interpreter resolvability layer (item 7.1).

Calls `_check_hook_interpreter_resolvability` directly rather than driving the CLI as a
subprocess (contrast `test_doctor.py`): the layer's own contract is "no spawn — only
`shutil.which` and a file read", and a subprocess-per-test would obscure that by adding
the very spawn the layer is built to avoid. `PATH` is monkeypatched per test so a bareword
interpreter's resolvability is fully controlled, never dependent on what happens to be
installed on the box running the suite.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from coordinator_core.ops import doctor


def _write_hooks_json(doe_root: Path, command: str) -> None:
    hooks_dir = doe_root / "coordinator" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
                {"type": "command", "command": command}]}]}},
            indent=2,
        )
    )


@pytest.fixture
def doe_root(tmp_path: Path) -> Path:
    root = tmp_path / "DoE-claude"
    (root / "coordinator" / "hooks").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _pin_doe_root(doe_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    # No settings.json hooks block in these fixtures — hooks.json alone is the surface
    # under test, so point settings.json at an empty, isolated config dir rather than
    # whatever the running machine actually has under ~/.claude.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(doe_root.parent / "claude-config-empty"))


def test_bareword_interpreter_absent_from_path_is_reported_fail(doe_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The plan's own fixture case: a bareword interpreter absent from PATH gives FAIL, and
    the finding names the consequence (its hooks fail open) rather than only the binary."""
    _write_hooks_json(doe_root, "totally-not-a-real-interpreter ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_path_dir))

    layer = doctor._check_hook_interpreter_resolvability()

    assert layer.status == "broken"
    assert any(
        "totally-not-a-real-interpreter" in f.message and "fails open" in f.message
        for f in layer.findings
    ), layer.findings


def test_interpreter_present_on_path_is_ok_and_quiet(doe_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A present interpreter gives OK — the plan's other fixture half — and the layer stays
    quiet, matching this module's house rule that a clean layer emits nothing to scroll past."""
    _write_hooks_json(doe_root, "findable-interpreter ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    path_dir = tmp_path / "fake-bin"
    path_dir.mkdir()
    interpreter_path = path_dir / "findable-interpreter"
    interpreter_path.write_text("#!/bin/sh\nexit 0\n")
    interpreter_path.chmod(interpreter_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(path_dir))

    layer = doctor._check_hook_interpreter_resolvability()

    assert layer.status == "ok"
    assert layer.findings == []


def test_no_readable_hooks_doc_is_unknown_not_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Absence of a check must not read as the check passing — this module's own stated rule.
    A DoE-claude root that resolves but carries no on-disk hooks.json, and no settings.json
    hooks block, must report UNKNOWN rather than a silent OK.

    Pins `REPO_DOE_CLAUDE` to an empty throwaway tree (never deletes/unsets it) — an unset
    var falls through to this box's own sibling-resolution rungs, which would find the real
    checked-out DoE-claude repo next to claude-klabauter and defeat the "no readable doc" case
    this test means to exercise."""
    empty_doe_root = tmp_path / "DoE-claude-empty"
    empty_doe_root.mkdir()
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(empty_doe_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config-empty"))

    layer = doctor._check_hook_interpreter_resolvability()

    assert layer.status == "unknown"
    assert layer.findings, "an unknown layer must name why, not report silently"
