"""bump_outside_repo_write under coordinator.feature.out_of_repo_write_block:
a marker or a lowered level never opens it, the deny is the flat in-repo
refusal, interpreter payload readings are judged, and sanctioned destinations
and flag-off consumers keep passing."""

from __future__ import annotations

import ntpath
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import _out_of_repo_allowlist as allowlist
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.bash_guards._write_bump_marker import marker_basename, resolve_gitdir
from coordinator_core.session import machinery_paths
from coordinator_core.testing.home_sandbox import sandbox_home
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_FLAG_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK"
_LEVEL_ENV = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL"
_SID = "sess-author-hard"


def _posix(p) -> str:
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags())


def _set_flag(monkeypatch, value: str) -> None:
    monkeypatch.setenv(_FLAG_ENV, value)
    machine_profile.reset_cache()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in (
        "CLAUDE_PROJECT_DIR", "CLAUDE_HOME", "HOME", "USERPROFILE",
        "MACHINE_LOCAL_REGISTRY_DIR", "TMPDIR", "TEMP", "TMP",
    ):
        monkeypatch.delenv(var, raising=False)
    settings_home = tmp_path / "isolated-settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    fake_temp = tmp_path / "not-the-real-system-temp"
    fake_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_temp))

    anchor = tmp_path / "anchor"
    anchor.mkdir()
    _git(str(anchor), "init", "-q")
    _git(str(anchor), "config", "user.email", "t@example.com")
    _git(str(anchor), "config", "user.name", "Test")
    (anchor / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(anchor), "add", "README.md")
    _git(str(anchor), "commit", "-q", "-m", "init")
    outside = tmp_path / "outside-scratch"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    sandbox_home(monkeypatch, home)
    session_start.write_session_start_record(_SID, launch_cwd=str(anchor))
    _set_flag(monkeypatch, "on")
    yield {"anchor": anchor, "outside": outside, "home": home, "settings": settings_home, "temp": fake_temp}
    machine_profile.reset_cache()


def _touch_marker(env) -> None:
    gitdir = resolve_gitdir(str(env["anchor"]))
    assert gitdir is not None
    (gitdir / marker_basename(_SID)).touch()


def _check(env, cmd, payload=None, cwd=None):
    return guard.check_bump_outside_repo_write(cmd, _SID, cwd or str(env["anchor"]), payload or {})


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_python_heredoc_outside_repo_denied_despite_marker_and_warn_level(env, monkeypatch):
    _touch_marker(env)
    monkeypatch.setenv(_LEVEL_ENV, "warn")
    dest = env["outside"] / "made.txt"
    cmd = f"python3 - <<'EOF'\nopen('{_posix(dest)}','w').write('x')\nEOF\n"

    result = _check(env, cmd)

    assert result is not None
    reason = _reason(result)
    assert _posix(env["anchor"]) + "/scratch/" in reason.replace("\\", "/")
    assert "marker" not in reason.lower()


def test_redirect_outside_repo_denied_despite_marker_and_off_level(env, monkeypatch):
    _touch_marker(env)
    monkeypatch.setenv(_LEVEL_ENV, "off")

    result = _check(env, f"echo hi > {_posix(env['outside'] / 'a.txt')}")

    assert result is not None


def test_node_heredoc_outside_repo_denied(env):
    dest = env["outside"] / "n.txt"
    cmd = f"node - <<'EOF'\nconst fs = require('fs');\nfs.writeFileSync('{_posix(dest)}','x');\nEOF\n"

    result = _check(env, cmd)

    assert result is not None


@pytest.mark.skipif(os.name != "nt", reason="cwd-drive reading is a Windows-host reading")
def test_cwd_drive_reading_of_slash_letter_literal_is_judged(env):
    cwd_drive = ntpath.splitdrive(str(env["anchor"]))[0][0].lower()
    other = next(c for c in "zyxw" if c != cwd_drive)
    cmd = f"python3 - <<'EOF'\nopen('/{other}/x-author-hard/y.txt','w').write('x')\nEOF\n"

    result = _check(env, cmd)

    assert result is not None
    assert (env["anchor"].drive.lower() + "\\" + other) in _reason(result).lower().replace("/", "\\")


def test_subagent_denied_with_share_dir_as_alternative(env):
    cmd = f"echo hi > {_posix(env['outside'] / 's.txt')}"

    result = _check(env, cmd, payload={"agent_id": "af307f34d8afa24eb"})

    assert result is not None
    reason = _reason(result)
    assert "subagent-share" in reason.replace("\\", "/")
    assert "marker" not in reason.lower()


def test_flag_off_marker_clears_consumer_parity(env, monkeypatch):
    _set_flag(monkeypatch, "off")
    _touch_marker(env)

    assert _check(env, f"echo hi > {_posix(env['outside'] / 'c.txt')}") is None


def test_flag_off_message_is_unchanged_bump_copy(env, monkeypatch):
    _set_flag(monkeypatch, "off")
    monkeypatch.setenv(_LEVEL_ENV, "strict")

    result = _check(env, f"echo hi > {_posix(env['outside'] / 'd.txt')}")

    assert result is not None
    assert "out-of-repo writes are blocked on this box" not in _reason(result)


def test_sanctioned_settings_home_passes(env):
    dest = env["settings"] / "claude-klabauter" / "ok.txt"
    dest.parent.mkdir(parents=True)

    assert _check(env, f"echo hi > {_posix(dest)}") is None


def test_sanctioned_os_temp_scratch_passes(env):
    dest = env["temp"] / "ok.txt"

    assert _check(env, f"echo hi > {_posix(dest)}") is None


def test_sanctioned_claude_home_passes(env):
    dest = env["home"] / ".claude" / "ok.txt"
    dest.parent.mkdir(parents=True)

    assert _check(env, f"echo hi > {_posix(dest)}") is None


def test_sanctioned_lessons_outbox_passes(env):
    dest = env["outside"] / "state" / "lessons-outbox" / "ok.md"
    dest.parent.mkdir(parents=True)

    assert _check(env, f"echo hi > {_posix(dest)}") is None


def test_sanctioned_agent_memory_store_passes(env):
    dest = env["home"] / ".claude" / "projects" / "slug" / "memory" / "m.md"
    dest.parent.mkdir(parents=True)

    assert _check(env, f"echo hi > {_posix(dest)}") is None


def test_sanctioned_session_share_dir_passes(env):
    share = Path(machinery_paths.share_dir(str(env["anchor"]), _SID))
    share.mkdir(parents=True)

    assert guard._target_is_always_allowed(str(share), str(env["anchor"]), _SID, env=os.environ)


def test_sanctioned_declared_run_output_root_passes(env, tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    root = env["outside"] / "run-out"
    root.mkdir()
    applicability.declare_run_output_root("run-author-hard", str(root))

    assert _check(env, f"echo hi > {_posix(root / 'o.txt')}") is None


def test_target_is_always_allowed_matches_allowlist(env):
    probe = str(env["outside"])
    assert guard._target_is_always_allowed(probe, str(env["anchor"]), _SID, env=os.environ) == (
        allowlist.sanctioned_destination(
            probe, session_repo=str(env["anchor"]), session_id=_SID, target_gitdir=None, env=os.environ
        )
        is not None
    )
    assert guard._target_is_always_allowed(probe, str(env["anchor"]), _SID, env=os.environ) is False
