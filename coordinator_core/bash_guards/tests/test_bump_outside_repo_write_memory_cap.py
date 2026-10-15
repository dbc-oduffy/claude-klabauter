"""A Bash write into a project memory dir is held to the memory-store file-count cap
by the outside-repo leg; under the cap it still passes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.testing.home_sandbox import sandbox_home
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _posix(p: Path) -> str:
    return p.as_posix()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_HOME", "MACHINE_LOCAL_REGISTRY_DIR", "TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)
    settings_home = tmp_path / "isolated-settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_GUARD-MEMORY-STORE-CAP", "strict")
    fake_temp = tmp_path / "not-the-real-system-temp"
    fake_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_temp))

    anchor = tmp_path / "anchor"
    anchor.mkdir()
    _git(anchor, "init", "-q")
    _git(anchor, "config", "user.email", "t@example.com")
    _git(anchor, "config", "user.name", "Test")
    (anchor / "src.txt").write_text("x\n", encoding="utf-8")
    _git(anchor, "add", "src.txt")
    _git(anchor, "commit", "-q", "-m", "init")

    home = tmp_path / "home"
    mem = home / ".claude" / "projects" / "-Some-project" / "memory"
    mem.mkdir(parents=True)
    sandbox_home(monkeypatch, home)
    session_start.write_session_start_record("sess-memcap", launch_cwd=str(anchor))
    return {"anchor": anchor, "mem": mem}


def _fill(mem: Path, count: int) -> None:
    for i in range(count):
        (mem / f"body-{i}.md").write_text("kept", encoding="utf-8")


def _check(env, cmd: str):
    return guard.check_bump_outside_repo_write(cmd, "sess-memcap", str(env["anchor"]), {})


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("shape", ["heredoc", "cp", "redirect"])
def test_new_file_at_cap_denies_with_reroute_text(env, shape):
    _fill(env["mem"], 5)
    dest = _posix(env["mem"] / "sixth.md")
    cmd = {
        "heredoc": f"cat > {dest} <<'EOF'\nbody\nEOF",
        "cp": f"cp {_posix(env['anchor'] / 'src.txt')} {dest}",
        "redirect": f"echo body > {dest}",
    }[shape]

    result = _check(env, cmd)

    assert result is not None
    reason = _reason(result)
    assert "[memory cap]" in reason
    assert "5/5 memories" in reason
    assert "state/lessons/" in reason


@pytest.mark.parametrize("shape", ["heredoc", "cp", "redirect"])
def test_new_file_under_cap_allows(env, shape):
    _fill(env["mem"], 4)
    dest = _posix(env["mem"] / "fifth.md")
    cmd = {
        "heredoc": f"cat > {dest} <<'EOF'\nbody\nEOF",
        "cp": f"cp {_posix(env['anchor'] / 'src.txt')} {dest}",
        "redirect": f"echo body > {dest}",
    }[shape]

    assert _check(env, cmd) is None


def test_rewrite_of_existing_file_at_cap_allows(env):
    _fill(env["mem"], 5)

    assert _check(env, f"echo body > {_posix(env['mem'] / 'body-0.md')}") is None


def test_index_file_at_cap_allows(env):
    _fill(env["mem"], 5)

    assert _check(env, f"echo '- row' > {_posix(env['mem'] / 'MEMORY.md')}") is None


def test_warn_level_does_not_deny(env, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_GUARD-MEMORY-STORE-CAP", "warn")
    _fill(env["mem"], 5)

    result = _check(env, f"echo body > {_posix(env['mem'] / 'sixth.md')}")

    assert result is None or result["hookSpecificOutput"].get("permissionDecision") != "deny"
