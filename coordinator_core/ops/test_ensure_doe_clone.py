"""Characterization tests for coordinator_core.ops.ensure_doe_clone.

Port source: coordinator/commands/install.md (DoE-claude) Step 3.5a, the two
literal bash fences at lines 731 and 747.
Spec backlink: DoE-claude:pln-extirpate-pasted-code-from-em--0f42e9 § M3/D9

Converted 2026-08-16 (C7b): `_registry_get` now reads the machine-local
registry in-process (`machine_resolver.registry_get`), so the scenario that
previously drove a fake `machine-local` CLI stub on PATH now seeds a scratch
registry FILE instead (`MACHINE_LOCAL_REGISTRY_DIR` pointed at an empty
`tmp_path` subdirectory, per
`state/lessons/2026-07-17-redirect-state-home-env-to-tmp-in-unit-t-*.yaml`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import coordinator_core.ops.ensure_doe_clone as edc
from coordinator_core.ops.ensure_doe_clone import main

# SPAWN-RATCHET Rule 2/4: main() below is a real-spawn wrapper (`subprocess.call
# (["git", "clone", ...])`) reached by nearly every test in this file. See
# coordinator_core/tests/test_no_new_spawning_tests.py.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _seed_registry(tmp_path: Path, **pairs: str) -> Path:
    """Write a scratch `registry.toml` under `tmp_path` and point
    `MACHINE_LOCAL_REGISTRY_DIR` at it -- replaces the old PATH-injected
    fake-CLI shape. Returns the registry dir (unused by callers today, kept
    for parity with the other converted test modules' helper signature).

    Values are TOML-escaped via `json.dumps` (TOML basic-string escaping is
    a superset of JSON's) -- a raw Windows path value contains backslashes
    that a naive `f'"{v}"'` would silently corrupt into invalid `\\U...`
    escapes, which `tomllib` degrades to "registry not found" rather than
    raising."""
    reg_dir = tmp_path / "ml-registry"
    reg_dir.mkdir(exist_ok=True)
    lines = "".join(f"{json.dumps(k)} = {json.dumps(v)}\n" for k, v in pairs.items())
    (reg_dir / "registry.toml").write_text(lines)
    return reg_dir


@pytest.fixture
def _fake_git_clone(monkeypatch):
    """Stub `ensure_doe_clone`'s `git clone` invocation in-process.

    Unlike `machine-local` (resolved via `shutil.which` in
    `_resolve_machine_local`, so a `.cmd`-wrapped fake works fine once
    resolved to its full path), `main()` invokes bare
    `subprocess.call(["git", "clone", ...])` directly. Win32
    `CreateProcess`'s own bare-name search only auto-appends `.exe` --
    never `.cmd`/`.bat` (that PATHEXT-driven search is a `cmd.exe` shell
    feature, not a `CreateProcess` one) -- so a `.cmd` fake `git` the way
    `write_fake_executable` would produce one is invisible to it, and
    fabricating a real `.exe` PE binary is not something a test can do.
    Stub the OS-process boundary directly instead; real production `git`
    is a genuine `git.exe` and is unaffected by this."""

    def _fake_call(args, **kwargs):
        assert args[:2] == ["git", "clone"]
        os.makedirs(os.path.join(args[3], ".git"), exist_ok=True)
        return 0

    monkeypatch.setattr(edc.subprocess, "call", _fake_call)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE_URL", raising=False)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)
    monkeypatch.setenv("PATH", "")
    # Scratch-scoped, always-empty-unless-seeded registry dir -- shields every
    # test from the operator's REAL machine-local registry (C7b). A test that
    # needs a specific registry value seeds it into this same directory via
    # `_seed_registry(tmp_path, ...)`.
    empty_registry = tmp_path / "ml-registry"
    empty_registry.mkdir(exist_ok=True)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty_registry))


def test_env_override_ready_when_git_dir_present(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(clone))

    rc = main([])

    assert rc == 0
    assert f"doe_clone: ready ({clone})" in capsys.readouterr().out


def test_check_only_skips_when_unresolved(capsys):
    rc = main(["--check-only"])
    assert rc == 0
    assert "doe_clone: skipped (repos.doe_claude not set)" in capsys.readouterr().out


def test_non_interactive_fails_loud_when_unresolved(capsys):
    rc = main(["--non-interactive"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "doe_clone: failed (repos.doe_claude not set" in out


def test_non_interactive_env_var_form(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_NON_INTERACTIVE", "1")
    rc = main([])
    assert rc == 1
    assert "doe_clone: failed (repos.doe_claude not set" in capsys.readouterr().out


def test_interactive_unresolved_reports_skip_and_nonzero(capsys):
    rc = main([])
    assert rc == 1
    out = capsys.readouterr().out
    assert "doe_clone: skipped (repos.doe_claude not set — run the interactive" in out


def test_check_only_would_clone_when_resolved_but_absent(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone-not-yet"
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(clone))

    rc = main(["--check-only"])

    # Resolved but absent is a genuinely stale/not-yet-cloned state -- fail
    # loud rather than silently reporting an always-green 0.
    assert rc == 1
    assert f"doe_clone: check failed: {clone} absent (would clone)" in capsys.readouterr().out
    assert not clone.exists()


def test_live_clone_fails_loud_without_resolvable_url(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone-not-yet"
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(clone))

    rc = main([])

    assert rc == 1
    assert "doe_clone: failed (repos.doe_claude_url not resolvable" in capsys.readouterr().out


def test_live_clone_succeeds_with_resolved_url(tmp_path, monkeypatch, capsys, _fake_git_clone):
    """Both REPO_DOE_CLAUDE and REPO_DOE_CLAUDE_URL are env-overridden here,
    so registry resolution is never reached -- no registry seed needed."""
    clone = tmp_path / "doe-clone-not-yet"
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(clone))
    monkeypatch.setenv("REPO_DOE_CLAUDE_URL", "https://example.invalid/doe-claude.git")

    rc = main([])

    assert rc == 0
    assert f"doe_clone: cloned ({clone})" in capsys.readouterr().out
    assert (clone / ".git").is_dir()


def test_registry_tier_resolves_clone_path(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone"
    (clone / ".git").mkdir(parents=True)
    _seed_registry(tmp_path, **{"repos.doe_claude": str(clone)})

    rc = main([])

    assert rc == 0
    assert f"doe_clone: ready ({clone})" in capsys.readouterr().out


def test_trailing_slash_stripped(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(clone) + "/")

    rc = main([])

    assert rc == 0
    assert f"doe_clone: ready ({clone})" in capsys.readouterr().out
