"""Pins check-mcp-namespace-registration.py: warns by name on unregistered namespaces, exits 0."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT = Path(__file__).resolve().parent.parent / "check-mcp-namespace-registration.py"
_COORD = SCRIPT.parent.parent


def _run(
    tmp_path: Path, registered: bool, extra_tools: str = "", argv: tuple = (), env_extra: dict | None = None,
    subject_is_cwd: bool = True,
) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "coordinator" / "bin").mkdir(parents=True)
    (repo / "coordinator" / "agents").mkdir()
    lib = repo / "coordinator" / "lib" / "frontmatter_scan.py"
    lib.parent.mkdir(parents=True)
    lib.write_text((_COORD / "lib" / "frontmatter_scan.py").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "coordinator" / "bin" / SCRIPT.name).write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "coordinator" / "agents" / "fix.md").write_text(
        '---\nname: fix\ntools: ["Read", "mcp__nonexistent-srv__x", "mcp__claude_ai_Foo__y"'
        + extra_tools
        + "]\n---\nbody mcp__ignored-in-body__z\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    servers = {"nonexistent-srv": {}} if registered else {}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    (home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"context7@m": True, "off@m": False}}), encoding="utf-8"
    )
    env = {"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home), "PATH": ""}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(repo / "coordinator" / "bin" / SCRIPT.name), *argv],
        capture_output=True, text=True, env=env, check=False,
        cwd=str(repo if subject_is_cwd else tmp_path),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_unregistered_namespace_warned_by_name(tmp_path):
    r = _run(tmp_path, registered=False)
    assert r.returncode == 0
    assert "fix.md" in r.stdout and "nonexistent-srv" in r.stdout
    assert "ignored-in-body" not in r.stdout


def test_registered_namespace_silent(tmp_path):
    r = _run(tmp_path, registered=True)
    assert r.returncode == 0
    assert "nonexistent-srv" not in r.stdout


def test_claude_ai_is_unverifiable_never_missing(tmp_path):
    r = _run(tmp_path, registered=True)
    assert "WARN" not in r.stdout
    assert "unverifiable" in r.stdout and "claude_ai_Foo" in r.stdout


def test_plugin_namespace_needs_truthy_enabled_plugin(tmp_path):
    r = _run(tmp_path, True, ', "mcp__plugin_context7_context7__q", "mcp__plugin_off_off__q"')
    assert "plugin_context7_context7" not in r.stdout
    assert "plugin_off_off" in r.stdout


def test_subject_repo_comes_from_the_caller_not_the_script_location(tmp_path):
    """The script copy sits in `repo`, but the subject is a different repo named by arg or env."""
    subject = tmp_path / "subject"
    (subject / ".git").mkdir(parents=True)
    (subject / "coordinator" / "agents").mkdir(parents=True)
    (subject / "coordinator" / "agents" / "other.md").write_text(
        '---\nname: other\ntools: ["mcp__subject-only-srv__x"]\n---\n', encoding="utf-8"
    )
    by_arg = _run(tmp_path, True, argv=("--repo-root", str(subject)), subject_is_cwd=False)
    assert "other.md" in by_arg.stdout and "subject-only-srv" in by_arg.stdout
    assert "fix.md" not in by_arg.stdout
    by_env = _run(
        tmp_path / "e", True, env_extra={"COORDINATOR_SUBJECT_REPO_ROOT": str(subject)}, subject_is_cwd=False
    )
    assert "other.md" in by_env.stdout and "fix.md" not in by_env.stdout
