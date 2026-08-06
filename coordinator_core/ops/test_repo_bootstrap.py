"""Tests for coordinator_core.ops.repo_bootstrap.

Machine-local registry calls are faked via an in-memory dict monkeypatched
over `_machine_local_get`/`_machine_local_set` (no real `machine-local`
binary is required/invoked). Every `git` invocation runs against a throwaway
source repo created fresh under pytest's `tmp_path` fixture and cloned via
the real `clone_idempotent()` — never the working claude-klabauter repo.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import repo_bootstrap as rb


_GIT_TIMEOUT = 30


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _make_source_repo(tmp_path: Path) -> Path:
    """A throwaway local git repo (one commit) to clone from."""
    src = tmp_path / "source-repo"
    src.mkdir()
    _run_git(["init"], cwd=src)
    _run_git(["config", "user.email", "test@example.invalid"], cwd=src)
    _run_git(["config", "user.name", "Test"], cwd=src)
    (src / "README.md").write_text("hello\n")
    _run_git(["add", "README.md"], cwd=src)
    _run_git(["commit", "-m", "initial"], cwd=src)
    return src


class _FakeRegistry:
    """Stand-in for the machine-local registry, monkeypatched over the
    module's own get/set helpers so no real `machine-local` CLI is needed."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []
        self.get_calls: list[str] = []

    def get(self, machine_local_bin: str, key: str):
        self.get_calls.append(key)
        return self.store.get(key)

    def set(self, machine_local_bin: str, key: str, value: str) -> bool:
        self.set_calls.append((key, value))
        self.store[key] = value
        return True


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeRegistry()
    monkeypatch.setattr(rb, "_resolve_machine_local_bin", lambda: "dummy-machine-local")
    monkeypatch.setattr(rb, "_machine_local_get", registry.get)
    monkeypatch.setattr(rb, "_machine_local_set", registry.set)
    return registry


def test_fresh_clone_and_register(tmp_path, fake_registry):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "sibling"

    result = rb.clone_and_register_sibling_repo("repos.sibling", str(src), str(target))

    assert result == {
        "cloned": True,
        "registered": True,
        "path": str(target),
        "already_present": False,
    }
    assert (target / ".git").is_dir()
    assert fake_registry.store["repos.sibling"] == str(target)


def test_second_invocation_is_a_safe_no_op(tmp_path, fake_registry):
    """AC7 — double-invocation with identical inputs is idempotent: no
    re-clone, no re-registration, no mutation of the already-present checkout
    or the already-registered key."""
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "sibling"

    first = rb.clone_and_register_sibling_repo("repos.sibling", str(src), str(target))
    assert first["cloned"] is True

    marker = target / "README.md"
    original_mtime = marker.stat().st_mtime
    set_calls_after_first = list(fake_registry.set_calls)

    second = rb.clone_and_register_sibling_repo("repos.sibling", str(src), str(target))

    assert second == {
        "cloned": False,
        "registered": False,
        "path": str(target),
        "already_present": True,
    }
    assert marker.stat().st_mtime == original_mtime
    assert fake_registry.set_calls == set_calls_after_first  # no new registration write


def test_on_disk_but_not_registered_completes_registration_only(tmp_path, fake_registry):
    """Partial-state repair: a pre-existing manual clone (on disk, never
    registered) gets registered without being re-cloned."""
    src = _make_source_repo(tmp_path)
    target = tmp_path / "manual-clone"
    _run_git(["clone", str(src), str(target)], cwd=tmp_path)
    assert (target / ".git").is_dir()

    marker = target / "README.md"
    original_mtime = marker.stat().st_mtime

    result = rb.clone_and_register_sibling_repo("repos.sibling", str(src), str(target))

    assert result == {
        "cloned": False,
        "registered": True,
        "path": str(target),
        "already_present": False,
    }
    assert marker.stat().st_mtime == original_mtime
    assert fake_registry.store["repos.sibling"] == str(target)


def test_registered_but_missing_on_disk_reclones_only(tmp_path, fake_registry):
    """Partial-state repair: a registered key whose target directory has
    since vanished gets re-cloned without touching the registry again."""
    src = _make_source_repo(tmp_path)
    target = tmp_path / "vanished" / "sibling"
    fake_registry.store["repos.sibling"] = str(target)

    result = rb.clone_and_register_sibling_repo("repos.sibling", str(src), str(target))

    assert result == {
        "cloned": True,
        "registered": False,
        "path": str(target),
        "already_present": False,
    }
    assert (target / ".git").is_dir()
    assert fake_registry.set_calls == []  # already-registered key was never re-written


def test_resolve_machine_local_bin_fallback_uses_userprofile_when_home_absent(
    tmp_path, monkeypatch
):
    """Native-Windows condition (home-resolution-lint bare_home_or_chain fix,
    2026-07-29): CLAUDE_HOME/HOME both absent, no settings-home bin candidate.
    `_resolve_machine_local_bin`'s legacy-compat fallback rung now delegates
    to `_settings_home.home_dir()` instead of a hand-rolled `CLAUDE_HOME or
    HOME` chain that degraded to a cwd-relative `.claude/bin/machine-local`
    in exactly this condition."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-unused"))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    userprofile_home = tmp_path / "winhome"
    fallback = userprofile_home / ".claude" / "bin" / "machine-local"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    # Path.home() only consults USERPROFILE on a real Windows interpreter;
    # simulate that resolution here so the test proves the delegation shape.
    monkeypatch.setattr(Path, "home", lambda: userprofile_home)

    assert rb._resolve_machine_local_bin() == str(fallback)


def test_machine_local_unresolvable_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "_resolve_machine_local_bin", lambda: None)
    target = tmp_path / "target"

    with pytest.raises(rb.RepoBootstrapError):
        rb.clone_and_register_sibling_repo(
            "repos.sibling", "https://example.invalid/repo.git", str(target)
        )
    assert not target.exists()


def test_clone_failure_raises_repo_bootstrap_error(tmp_path, fake_registry):
    nonexistent_source = tmp_path / "does-not-exist"
    target = tmp_path / "target"

    with pytest.raises(rb.RepoBootstrapError):
        rb.clone_and_register_sibling_repo(
            "repos.sibling", str(nonexistent_source), str(target)
        )
    assert not (target / ".git").is_dir()
    assert "repos.sibling" not in fake_registry.store


def test_machine_local_set_failure_raises_repo_bootstrap_error(tmp_path, monkeypatch):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "sibling"

    monkeypatch.setattr(rb, "_resolve_machine_local_bin", lambda: "dummy-machine-local")
    monkeypatch.setattr(rb, "_machine_local_get", lambda ml, key: None)
    monkeypatch.setattr(rb, "_machine_local_set", lambda ml, key, value: False)

    with pytest.raises(rb.RepoBootstrapError):
        rb.clone_and_register_sibling_repo("repos.sibling", str(src), str(target))

    # Clone itself must have succeeded even though registration failed —
    # the repo is on disk but unregistered, matching the raised error's
    # own remediation text.
    assert (target / ".git").is_dir()


def test_registered_handler_dispatches_and_requires_all_params(tmp_path, fake_registry):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "via-handler"

    result = asyncio.run(
        rb._clone_and_register_sibling_repo_op(
            {"repo_key": "repos.sibling", "clone_url": str(src), "dest_path": str(target)}
        )
    )
    assert result == {
        "cloned": True,
        "registered": True,
        "path": str(target),
        "already_present": False,
    }

    for missing in (
        {"clone_url": "x", "dest_path": "y"},
        {"repo_key": "x", "dest_path": "y"},
        {"repo_key": "x", "clone_url": "y"},
    ):
        with pytest.raises(ValueError):
            asyncio.run(rb._clone_and_register_sibling_repo_op(missing))
