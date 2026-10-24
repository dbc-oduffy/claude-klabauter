"""Pins the closed out-of-repo destination list and each entry's delegation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _out_of_repo_allowlist as allow
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.session import machinery_paths

SID = "sess-c3"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Isolated home, settings home, registry dir and temp root under tmp_path."""
    home = tmp_path / "home"
    settings = tmp_path / "settings-home"
    registry = tmp_path / "registry"
    os_tmp = tmp_path / "os-tmp"
    for d in (home, settings, registry, os_tmp):
        d.mkdir()
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("TMPDIR", str(os_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(os_tmp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: None)
    repo = tmp_path / "session-repo"
    repo.mkdir()
    return {"home": home, "settings": settings, "os_tmp": os_tmp, "repo": repo, "root": tmp_path}


def _dest(world, path, **kw):
    return allow.sanctioned_destination(
        str(path), session_repo=str(world["repo"]), session_id=SID, **kw
    )


def test_tuple_is_the_closed_list():
    assert allow.SANCTIONED_DESTINATIONS == (
        "claude-home",
        "settings-home",
        "agent-memory-store",
        "os-temp-scratch",
        "session-share-dir",
        "lessons-outbox",
        "declared-run-output-root",
        "session-repo-scratch",
    )


def test_session_repo_scratch_nested_clone_is_sanctioned(world):
    clone = world["repo"] / "scratch" / "claude-klabauter-pub"
    (clone / ".git").mkdir(parents=True)
    assert _dest(world, clone, target_gitdir=clone / ".git") == "session-repo-scratch"


def test_sibling_repo_and_scratch_lookalikes_stay_refused(world):
    sibling = world["root"] / "sibling" / "scratch" / "x"
    lookalike = world["root"] / "session-repo-scratch-evil"
    sibling.mkdir(parents=True)
    lookalike.mkdir()
    assert _dest(world, sibling, target_gitdir=sibling) is None
    assert _dest(world, lookalike) is None
    assert _dest(world, world["repo"] / "other") is None


def test_claude_home(world):
    assert _dest(world, world["home"] / ".claude" / "settings.json") == "claude-home"


def test_settings_home(world):
    assert _dest(world, world["settings"] / "x" / "f.txt") == "settings-home"


def test_agent_memory_store(world):
    mem = world["home"] / ".claude" / "projects" / "-slug" / "memory"
    mem.mkdir(parents=True)
    assert applicability.is_agent_memory_store_path(str(mem / "note.md"))
    assert _dest(world, mem / "note.md") is not None


def test_os_temp_scratch(world):
    assert _dest(world, world["os_tmp"] / "scratch" / "f.txt") == "os-temp-scratch"


def test_os_temp_checkout_is_not_scratch(world):
    checkout = world["os_tmp"] / "clone"
    (checkout / ".git").mkdir(parents=True)
    assert _dest(world, checkout / "f.txt", target_gitdir=checkout / ".git") is None


def test_session_share_dir(world):
    share = machinery_paths.share_dir(str(world["repo"]), SID)
    assert _dest(world, Path(share) / "report.md") == "session-share-dir"


def test_other_session_share_dir_is_not_sanctioned(world):
    share = machinery_paths.share_dir(str(world["repo"]), "someone-else")
    assert _dest(world, Path(share) / "report.md") is None


def test_lessons_outbox(world):
    peer = world["root"] / "peer" / "state" / "lessons-outbox"
    assert _dest(world, peer / "L1.yaml") == "lessons-outbox"
    assert allow.is_lessons_outbox_write(str(peer / "drained" / "L1.yaml"))
    assert not allow.is_lessons_outbox_write(str(world["root"] / "peer" / "cross-repo" / "inbox" / "m.md"))
    assert not allow.is_lessons_outbox_write("")


def test_declared_run_output_root(world):
    out = world["root"] / "run-out"
    out.mkdir()
    applicability.declare_run_output_root("run-1", str(out))
    assert _dest(world, out / "r" / "row.md") == "declared-run-output-root"


def test_peer_repo_drive_root_and_home_subdir_are_refused(world):
    peer = world["root"] / "peer-repo"
    (peer / ".git").mkdir(parents=True)
    assert _dest(world, peer / "f.txt", target_gitdir=peer / ".git") is None
    assert _dest(world, world["root"] / "newtopdir" / "f.txt") is None
    assert _dest(world, world["home"] / "randomdir" / "f.txt") is None


@pytest.mark.parametrize("bad", ["", "\x00bad", "//?/::"])
def test_unresolvable_path_returns_none_not_an_exception(world, bad):
    assert _dest(world, bad) is None


def test_no_session_repo_returns_none_for_share(world):
    assert allow.sanctioned_destination(
        str(world["root"] / "x"), session_repo=None, session_id=""
    ) is None
