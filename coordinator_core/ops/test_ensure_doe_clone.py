"""Characterization tests for coordinator_core.ops.ensure_doe_clone.

Port source: coordinator/commands/install.md (example-doctrine-repo) Step 3.5a, the two
literal bash fences at lines 731 and 747.
Spec backlink: docs/plans/2026-07-23-skills-carry-no-code-extirpation.md § M3/D9
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import coordinator_core.ops.ensure_doe_clone as edc
from coordinator_core.ops.ensure_doe_clone import main
from coordinator_core.testing.fake_machine_local import write_fake_executable


def _make_fake_bin(tmp_path: Path, *, get_value: str = "", get_rc: int = 0) -> Path:
    """Fake `machine-local` that answers `get repos.example_doctrine_repo` and
    `get repos.example_doctrine_repo_url` from env-driven fixture values.

    Fabricated via `write_fake_executable` (extensionless POSIX,
    `.cmd`-wrapped-Python on Windows) rather than a raw `#!/bin/sh` script:
    a bare-name shebang fake is unexecutable via Windows `CreateProcess`
    (`fake_machine_local.py`'s own module docstring) -- this module is the
    repo's existing cross-platform fake-CLI convention, not a new one."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)

    ml_body = (
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if len(args) >= 2 and args[0] == 'get' and args[1] == 'repos.example_doctrine_repo':\n"
        f"    sys.stdout.write({get_value!r})\n"
        f"    sys.exit({get_rc})\n"
        "if len(args) >= 2 and args[0] == 'get' and args[1] == 'repos.example_doctrine_repo_url':\n"
        "    sys.stdout.write(os.environ.get('FAKE_DOE_URL', ''))\n"
        "    sys.exit(0)\n"
        "sys.exit(9)\n"
    )
    write_fake_executable(bin_dir, "machine-local", ml_body)
    return bin_dir


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
def _isolated_env(monkeypatch):
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO_URL", raising=False)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)
    monkeypatch.setenv("PATH", "")


def test_env_override_ready_when_git_dir_present(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(clone))

    rc = main([])

    assert rc == 0
    assert f"doe_clone: ready ({clone})" in capsys.readouterr().out


def test_check_only_skips_when_unresolved(capsys):
    rc = main(["--check-only"])
    assert rc == 0
    assert "doe_clone: skipped (repos.example_doctrine_repo not set)" in capsys.readouterr().out


def test_non_interactive_fails_loud_when_unresolved(capsys):
    rc = main(["--non-interactive"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "doe_clone: failed (repos.example_doctrine_repo not set" in out


def test_non_interactive_env_var_form(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_NON_INTERACTIVE", "1")
    rc = main([])
    assert rc == 1
    assert "doe_clone: failed (repos.example_doctrine_repo not set" in capsys.readouterr().out


def test_interactive_unresolved_reports_skip_and_nonzero(capsys):
    rc = main([])
    assert rc == 1
    out = capsys.readouterr().out
    assert "doe_clone: skipped (repos.example_doctrine_repo not set — run the interactive" in out


def test_check_only_would_clone_when_resolved_but_absent(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone-not-yet"
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(clone))

    rc = main(["--check-only"])

    # Resolved but absent is a genuinely stale/not-yet-cloned state -- fail
    # loud rather than silently reporting an always-green 0.
    assert rc == 1
    assert f"doe_clone: check failed: {clone} absent (would clone)" in capsys.readouterr().out
    assert not clone.exists()


def test_live_clone_fails_loud_without_resolvable_url(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone-not-yet"
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(clone))

    rc = main([])

    assert rc == 1
    assert "doe_clone: failed (repos.example_doctrine_repo_url not resolvable" in capsys.readouterr().out


def test_live_clone_succeeds_with_resolved_url(tmp_path, monkeypatch, capsys, _fake_git_clone):
    clone = tmp_path / "doe-clone-not-yet"
    bin_dir = _make_fake_bin(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/bin" + os.pathsep + "/usr/bin")
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(clone))
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO_URL", "https://example.invalid/example-doctrine-repo.git")

    rc = main([])

    assert rc == 0
    assert f"doe_clone: cloned ({clone})" in capsys.readouterr().out
    assert (clone / ".git").is_dir()


def test_registry_tier_resolves_clone_path(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone"
    (clone / ".git").mkdir(parents=True)
    bin_dir = _make_fake_bin(tmp_path, get_value=str(clone))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/bin" + os.pathsep + "/usr/bin")

    rc = main([])

    assert rc == 0
    assert f"doe_clone: ready ({clone})" in capsys.readouterr().out


def test_trailing_slash_stripped(tmp_path, monkeypatch, capsys):
    clone = tmp_path / "doe-clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(clone) + "/")

    rc = main([])

    assert rc == 0
    assert f"doe_clone: ready ({clone})" in capsys.readouterr().out
