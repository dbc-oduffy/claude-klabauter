"""bump_out_of_repo_tool_write under the out-of-repo write block: a Write/Edit
outside the session repo is denied with an in-repo alternative, no marker or
level clears it, and each sanctioned destination still passes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_marker as marker
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards._out_of_repo_allowlist import SANCTIONED_DESTINATIONS
from coordinator_core.session import machinery_paths
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment
from coordinator_core.tests.git_seed import seeded_repo
from coordinator_core.testing.home_sandbox import sandbox_home
from coordinator_core.write_guards import bump_out_of_repo_tool_write as guard

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _flag(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK", value)
    machine_profile.reset_cache()


def _repo(tmp_path: Path, name: str) -> Path:
    return seeded_repo(tmp_path / name, name="Test")


def _payload(tool, path, sid, cwd, agent_id=""):
    return {
        "tool_name": tool,
        "tool_input": {"file_path": str(path)},
        "session_id": sid,
        "cwd": str(cwd),
        "agent_id": agent_id,
    }


def _session(tmp_path, monkeypatch, sid):
    _flag(monkeypatch, "on")
    elsewhere = tmp_path / "unrelated-temp-root"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(elsewhere))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(elsewhere))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)
    own = _repo(tmp_path, "own-repo")
    session_start.write_session_start_record(sid, launch_cwd=str(own))
    return own


def _reason(result) -> str:
    assert result is not None, "expected a deny"
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def _assert_in_repo_alternative(reason: str, own: Path) -> None:
    assert str(own).replace("\\", "/").lower() in reason.replace("\\", "/").lower()
    for banned in ("touch", "guard_level", "machine-local set", "grant"):
        assert banned not in reason


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_no_repo_directory_outside_the_repo_is_denied_with_marker_and_warn(
    tmp_path, monkeypatch, tool
):
    sid = "sess-hard-norepo"
    own = _session(tmp_path, monkeypatch, sid)
    gitdir = marker.resolve_gitdir(str(own))
    (gitdir / marker.marker_basename(sid)).touch()
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    machine_profile.reset_cache()
    outside = tmp_path / "stray-dir"
    outside.mkdir()

    reason = _reason(guard.check(_payload(tool, outside / "x.txt", sid, own)))

    _assert_in_repo_alternative(reason, own)
    assert "scratch" in reason


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_peer_repo_is_denied_with_marker_and_warn_and_names_cross_repo_memo(
    tmp_path, monkeypatch, tool
):
    sid = "sess-hard-peer"
    own = _session(tmp_path, monkeypatch, sid)
    peer = _repo(tmp_path, "peer-repo")
    for gd in (marker.resolve_gitdir(str(own)), marker.resolve_gitdir(str(peer))):
        (gd / marker.marker_basename(sid)).touch()
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    machine_profile.reset_cache()

    reason = _reason(guard.check(_payload(tool, peer / "n.txt", sid, own)))

    _assert_in_repo_alternative(reason, own)
    assert "cross-repo-memo" in reason


def test_unwritable_marker_gitdir_no_longer_opens_the_door(tmp_path, monkeypatch):
    sid = "sess-hard-unwritable"
    own = _session(tmp_path, monkeypatch, sid)
    peer = _repo(tmp_path, "peer-repo")
    monkeypatch.setattr(guard, "marker_gitdir_is_writable", lambda _g: False)

    _reason(guard.check(_payload("Write", peer / "n.txt", sid, own)))


def test_subagent_is_denied_with_its_share_dir_named(tmp_path, monkeypatch):
    sid = "sess-hard-subagent"
    own = _session(tmp_path, monkeypatch, sid)
    outside = tmp_path / "stray-dir"
    outside.mkdir()

    reason = _reason(
        guard.check(_payload("Write", outside / "x.txt", sid, own, agent_id="abcdef123456789012"))
    )

    share = machinery_paths.share_dir(str(own), _sanitize_segment(sid))
    assert share.replace("\\", "/").lower() in reason.replace("\\", "/").lower()
    assert "touch" not in reason


def test_own_repo_write_still_passes_under_the_flag(tmp_path, monkeypatch):
    sid = "sess-hard-own"
    own = _session(tmp_path, monkeypatch, sid)

    assert guard.check(_payload("Write", own / "scratch" / "a.txt", sid, own)) is None


def test_flag_off_with_a_marker_passes(tmp_path, monkeypatch):
    sid = "sess-hard-off"
    own = _session(tmp_path, monkeypatch, sid)
    _flag(monkeypatch, "off")
    peer = _repo(tmp_path, "peer-repo")
    (marker.resolve_gitdir(str(peer)) / marker.marker_basename(sid)).touch()

    assert guard.check(_payload("Write", peer / "n.txt", sid, own)) is None


def test_allowlist_is_the_set_the_passing_cases_below_cover():
    assert SANCTIONED_DESTINATIONS == (
        "claude-home",
        "settings-home",
        "agent-memory-store",
        "os-temp-scratch",
        "session-share-dir",
        "lessons-outbox",
        "declared-run-output-root",
    )


def test_claude_home_passes(tmp_path, monkeypatch):
    sid = "sess-hard-claude-home"
    own = _session(tmp_path, monkeypatch, sid)
    home = _repo(tmp_path, "home-that-is-a-repo")
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    sandbox_home(monkeypatch, home)

    assert guard.check(_payload("Write", home / ".claude" / "settings.json", sid, own)) is None


def test_settings_home_passes(tmp_path, monkeypatch):
    sid = "sess-hard-settings-home"
    own = _session(tmp_path, monkeypatch, sid)
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    assert guard.check(_payload("Write", settings_home / "p" / "a.json", sid, own)) is None


def test_agent_memory_store_passes(tmp_path, monkeypatch):
    sid = "sess-hard-memory"
    own = _session(tmp_path, monkeypatch, sid)
    home = _repo(tmp_path, "memory-home")
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    sandbox_home(monkeypatch, home)
    memory = home / ".claude" / "projects" / "-Users-x-proj" / "memory"
    memory.mkdir(parents=True)

    assert guard.check(_payload("Write", memory / "note.md", sid, own)) is None


def test_os_temp_scratch_passes(tmp_path, monkeypatch):
    sid = "sess-hard-temp"
    own = _session(tmp_path, monkeypatch, sid)
    temp_root = tmp_path / "tmproot"
    temp_root.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(temp_root))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)

    assert guard.check(_payload("Write", temp_root / "scratch.txt", sid, own)) is None


def test_session_share_dir_passes(tmp_path, monkeypatch):
    sid = "sess-hard-share"
    own = _session(tmp_path, monkeypatch, sid)
    share = Path(machinery_paths.share_dir(str(own), _sanitize_segment(sid)))

    assert guard.check(_payload("Write", share / "report.md", sid, own)) is None


def test_lessons_outbox_passes(tmp_path, monkeypatch):
    sid = "sess-hard-lessons"
    own = _session(tmp_path, monkeypatch, sid)
    doe = _repo(tmp_path, "coordinator-content-repo")

    assert guard.check(_payload("Write", doe / "state" / "lessons-outbox" / "l.yaml", sid, own)) is None


def test_declared_run_output_root_passes_and_publish_destination_does_not(tmp_path, monkeypatch):
    sid = "sess-hard-run-root"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))
    own = _session(tmp_path, monkeypatch, sid)
    foreign = _repo(tmp_path, "foreign-repo")
    out = foreign / "scratch" / "run-1"
    out.mkdir(parents=True)
    applicability.declare_run_output_root("run-1", str(out))

    assert guard.check(_payload("Write", out / "f.json", sid, own)) is None

    monkeypatch.setattr(guard, "target_is_publish_destination", lambda _root: True)
    _reason(guard.check(_payload("Write", out / "f.json", sid, own)))
