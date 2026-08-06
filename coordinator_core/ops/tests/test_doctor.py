"""Tests for the out-of-harness doctor command.

The doctor exists because five occurrences in two days of the same incident class left the
agent unable to repair the break, since the break removed Write, Edit and Bash. Its value is
therefore entirely in being *right* about a broken machine while a human reads it in a plain
terminal — a doctor that reports OK on a broken layer is worse than no doctor, because it
converts "something is wrong" into "something is wrong and the tool says it isn't".

These drive the real CLI as a subprocess rather than calling the op directly. Invocation is
part of what is under test: the trampoline resolves its own claude-klabauter root and the whole point is
that it runs with no Claude Code process involved.

Fixtures point `REPO_EXAMPLE_DOCTRINE_REPO` at a throwaway tree. Never at the live one — a health check
tested against live shared config is the thing it is meant to catch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_DOCTOR = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin" / "doctor.py"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _write_hooks(doe_root: Path, command: str) -> None:
    hooks_dir = doe_root / "coordinator" / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
                {"type": "command", "command": command}]}]}},
            indent=2,
        )
    )


def _run_doctor(doe_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_DOCTOR), *args],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
        # Both roots pinned explicitly. Otherwise the sibling-resolution layer reports
        # BROKEN under pytest (the machine-local registry is not reachable there) and every
        # assertion about the hook layer ends up hostage to an unrelated one.
        env=dict(
            os.environ,
            REPO_EXAMPLE_DOCTRINE_REPO=str(doe_root),
            REPO_CLAUDE_KLABAUTER=str(_CLAUDE_KLABAUTER_ROOT),
            CLAUDE_KLABAUTER_ROOT=str(_CLAUDE_KLABAUTER_ROOT),
        ),
    )


@pytest.fixture
def doe_root(tmp_path: Path) -> Path:
    root = tmp_path / "example-doctrine-repo"
    (root / "coordinator" / "hooks" / "scripts").mkdir(parents=True)
    return root


def test_registration_pointing_at_a_missing_script_is_reported_broken(doe_root: Path):
    """The exact 2026-07-29 incident: a registration outliving the script it names. Every
    on-disk consistency check passed while this was true, which is why the doctor has to be
    the thing that catches it."""
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/deleted-by-a-peer.py")

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "BROKEN" in result.stdout
    assert "deleted-by-a-peer.py" in result.stdout, "the report must name the missing script"
    assert "Hook registration" in result.stdout


def test_a_healthy_registration_is_quiet(doe_root: Path):
    """Quiet on clean. A check that fires on benign states is muted within a week, and this
    guard family already has members that went inert exactly that way."""
    script = doe_root / "coordinator" / "hooks" / "scripts" / "real.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    result = _run_doctor(doe_root)

    assert "BROKEN" not in result.stdout, result.stdout
    assert "registered script missing" not in result.stdout


def test_a_bare_registration_is_flagged_as_not_fail_open(doe_root: Path):
    """A present script that is nonetheless registered bare is a latent instance of the same
    incident — it works until the day the path stops resolving."""
    script = doe_root / "coordinator" / "hooks" / "scripts" / "real.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    result = _run_doctor(doe_root)

    assert "bare" in result.stdout.lower(), result.stdout


def test_exit_code_distinguishes_broken_from_healthy(doe_root: Path):
    """A caller gating on this must be able to tell the two apart without parsing prose."""
    script = doe_root / "coordinator" / "hooks" / "scripts" / "real.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")
    healthy = _run_doctor(doe_root).returncode

    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/gone.py")
    broken = _run_doctor(doe_root).returncode

    assert broken == 1
    assert healthy != broken


def test_reports_rather_than_raises_on_an_unreadable_hooks_document(doe_root: Path):
    """A layer it cannot evaluate must say so. Absence of a check must never be
    indistinguishable from the check passing — that is the pathology the whole plan is
    about."""
    hooks_dir = doe_root / "coordinator" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text("{ not valid json")

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "Traceback" not in result.stderr, "must report, not crash: " + result.stderr
