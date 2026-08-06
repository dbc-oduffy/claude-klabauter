"""test_reap_stale_subagent_sidecars.py — regression suite for
reap-stale-subagent-sidecars.py.

Covers the three-gate reapable predicate: dead-session liveness, the age
floor, and the blocked/thrashing status carve-out — plus the tracked/
untracked git-rm split and --dry-run non-mutation.

Spec backlink: docs/plans/2026-07-24-reviewer-sidecar-provisioning-reconciliation.md § C7
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import time

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "reap-stale-subagent-sidecars.py")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_module():
    spec = importlib.util.spec_from_file_location("reap_stale_subagent_sidecars", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _git(cwd, *args):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True, creationflags=_NO_WINDOW)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")


def _write_sidecar(path, status):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"---\nstatus: {status}\n---\nbody\n")


def _age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_dead_session_old_complete_is_reaped(tmp_path, mod, monkeypatch, capsys):
    root = str(tmp_path)
    _init_repo(root)
    f = os.path.join(root, "state", "subagent-share", "deadsess", "run.md")
    _write_sidecar(f, "complete")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _age(f, 20)

    monkeypatch.setattr(mod, "_resolve_session_live", lambda: (lambda sid, cwd=None: False))
    monkeypatch.chdir(root)
    rc = mod.main([])
    assert rc == 0
    assert not os.path.exists(f)
    out = capsys.readouterr().out
    assert "1 stale subagent-share sidecar(s) reaped" in out


def test_live_session_preserves_regardless_of_status_or_age(tmp_path, mod, monkeypatch, capsys):
    root = str(tmp_path)
    _init_repo(root)
    f = os.path.join(root, "state", "subagent-share", "livesess", "run.md")
    _write_sidecar(f, "complete")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _age(f, 999)

    monkeypatch.setattr(mod, "_resolve_session_live", lambda: (lambda sid, cwd=None: True))
    monkeypatch.chdir(root)
    rc = mod.main(["--dry-run"])
    assert rc == 0
    assert os.path.exists(f)
    out = capsys.readouterr().out
    assert "nothing to reap" in out
    assert "owning session still live" in out


def test_blocked_status_never_reaped_even_if_dead_and_old(tmp_path, mod, monkeypatch, capsys):
    root = str(tmp_path)
    _init_repo(root)
    f = os.path.join(root, "state", "subagent-share", "deadsess", "blocked.md")
    _write_sidecar(f, "blocked")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _age(f, 999)

    monkeypatch.setattr(mod, "_resolve_session_live", lambda: (lambda sid, cwd=None: False))
    monkeypatch.chdir(root)
    rc = mod.main([])
    assert rc == 0
    assert os.path.exists(f)
    out = capsys.readouterr().out
    assert "status: blocked/thrashing" in out


def test_dead_session_too_young_is_preserved(tmp_path, mod, monkeypatch, capsys):
    root = str(tmp_path)
    _init_repo(root)
    f = os.path.join(root, "state", "subagent-share", "deadsess", "run.md")
    _write_sidecar(f, "complete")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _age(f, 1)

    monkeypatch.setattr(mod, "_resolve_session_live", lambda: (lambda sid, cwd=None: False))
    monkeypatch.chdir(root)
    rc = mod.main(["--age-floor-days", "14"])
    assert rc == 0
    assert os.path.exists(f)
    out = capsys.readouterr().out
    assert "age floor" in out


def test_dry_run_mutates_nothing(tmp_path, mod, monkeypatch, capsys):
    root = str(tmp_path)
    _init_repo(root)
    f = os.path.join(root, "state", "subagent-share", "deadsess", "run.md")
    _write_sidecar(f, "complete")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _age(f, 20)

    monkeypatch.setattr(mod, "_resolve_session_live", lambda: (lambda sid, cwd=None: False))
    monkeypatch.chdir(root)
    rc = mod.main(["--dry-run"])
    assert rc == 0
    assert os.path.exists(f)
    out = capsys.readouterr().out
    assert "would be reaped (dry-run)" in out


def test_untracked_stale_sidecar_uses_plain_remove(tmp_path, mod, monkeypatch, capsys):
    root = str(tmp_path)
    _init_repo(root)
    # Seed a commit so `git rev-parse --show-toplevel` resolves.
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    f = os.path.join(root, "state", "subagent-share", "deadsess", "run.md")
    _write_sidecar(f, "complete")
    _age(f, 20)

    monkeypatch.setattr(mod, "_resolve_session_live", lambda: (lambda sid, cwd=None: False))
    monkeypatch.chdir(root)
    rc = mod.main([])
    assert rc == 0
    assert not os.path.exists(f)
    out = capsys.readouterr().out
    assert "untracked, plain rm" in out
