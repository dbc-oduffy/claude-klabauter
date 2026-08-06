"""Tests for coordinator_core.ops.install_meta_repo_precommit_hook.main_post_sync
— the post-merge/post-checkout gate installer added 2026-07-28.

See that module's own docstring (2026-07-28 addition section) for why this
exists: every pre-existing gate fires on the sending side (pre-commit) or
the authoring side (gen_settings_hooks' own kill-switch check); nothing
fired on the RECEIVING side of a `git merge`/`git pull`, which is where the
2026-07-28 incident's actual corrupted-settings.json transmission happened.

Mirrors coordinator_core/ops/test_install_meta_repo_precommit_hook.py's
style (behavioral: execute the emitted hook via `sh` with a stub gate
script, not just grep the hook text for marker substrings) rather than
duplicating its full corpus — `_install_or_append_hook` is SHARED code
already characterized by that file's fresh-install/append/idempotency
tests against `main()`; this file's job is to prove `main_post_sync()`
drives that shared function correctly for TWO hook filenames with its OWN
registry, not to re-prove the shared mechanism itself.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.install_meta_repo_precommit_hook import (
    _POST_SYNC_GATE_REGISTRY,
    _POST_SYNC_HOOK_FILENAMES,
    main_post_sync,
)
import coordinator_core.ops.install_meta_repo_precommit_hook as _mod


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _make_meta_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fakehome"
    meta = fake_home / ".claude"
    meta.mkdir(parents=True)
    _git_init(meta)
    monkeypatch.setenv("HOME", str(fake_home))
    # CLAUDE_HOME outranks HOME in meta_repo_identity's precedence, and the
    # suite-root home quarantine does not clear it — leaving a real one set
    # would point the op at the developer's live meta-repo.
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    return meta


def _hook_paths(meta_repo: Path) -> list[Path]:
    return [meta_repo / ".git" / "hooks" / name for name in _POST_SYNC_HOOK_FILENAMES]


def _write_stub_gates(fake_bin: Path, exit_map: dict | None = None) -> None:
    exit_map = exit_map or {}
    fake_bin.mkdir(parents=True, exist_ok=True)
    for gate in _POST_SYNC_GATE_REGISTRY:
        rc = exit_map.get(gate.filename, 0)
        script = fake_bin / gate.filename
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"print('RAN:{gate.filename}')\n"
            f"sys.exit({rc})\n",
            encoding="utf-8",
        )
        os.chmod(script, 0o755)


def _run_hook(hook: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(hook)], cwd=str(cwd), capture_output=True, text=True
    )


# ---------------------------------------------------------------------------
# Identity guard — same as main(), reused via _resolve_meta_repo_target
# ---------------------------------------------------------------------------


def test_target_not_a_git_repo(tmp_path, capsys):
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    rc = main_post_sync([str(notgit)])
    assert rc == 0
    assert "not in a git repo" in capsys.readouterr().err


def test_target_is_git_repo_but_not_meta_repo(tmp_path, monkeypatch, capsys):
    somerepo = tmp_path / "somerepo"
    somerepo.mkdir()
    _git_init(somerepo)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    rc = main_post_sync([str(somerepo)])
    assert rc == 0
    assert "not the meta-repo" in capsys.readouterr().err
    for name in _POST_SYNC_HOOK_FILENAMES:
        assert not (somerepo / ".git" / "hooks" / name).exists()


# ---------------------------------------------------------------------------
# Fresh install — both hook filenames written, gate present + reachable
# ---------------------------------------------------------------------------


def test_fresh_install_writes_both_hooks_and_they_execute(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    rc = main_post_sync([str(meta)])
    assert rc == 0

    err = capsys.readouterr().err
    for hook_path in _hook_paths(meta):
        assert hook_path.is_file()
        content = hook_path.read_text(encoding="utf-8")
        for gate in _POST_SYNC_GATE_REGISTRY:
            assert gate.marker in content
        assert content.rstrip("\n").endswith("exit 0")
        if os.name != "nt":
            # git runs this hook directly, so the POSIX exec bit is the property under
            # test. Windows has no equivalent and os.access(X_OK) degrades to F_OK
            # there, which would assert nothing while reading as though it did.
            assert os.access(hook_path, os.X_OK)

        result = _run_hook(hook_path, meta)
        assert result.returncode == 0
        assert "RAN:coordinator-postsync-marker-resync-check" in result.stdout
    assert err.count("installed") == 2


def test_idempotent_on_second_call(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    main_post_sync([str(meta)])
    before = {p: p.read_text(encoding="utf-8") for p in _hook_paths(meta)}

    rc = main_post_sync([str(meta)])
    assert rc == 0
    after = {p: p.read_text(encoding="utf-8") for p in _hook_paths(meta)}
    assert before == after
    assert capsys.readouterr().err.count("already installed") == 2


def test_missing_gate_script_blocks_loudly(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main_post_sync([str(meta)]) == 0

    gate = _POST_SYNC_GATE_REGISTRY[0]
    (fake_bin / gate.filename).unlink()

    for hook_path in _hook_paths(meta):
        result = _run_hook(hook_path, meta)
        assert result.returncode == 1
        assert "BLOCKED" in result.stderr
        assert gate.marker in result.stderr


def test_override_bypasses_missing_gate_script(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)
    assert main_post_sync([str(meta)]) == 0

    gate = _POST_SYNC_GATE_REGISTRY[0]
    (fake_bin / gate.filename).unlink()

    env = dict(os.environ)
    env[gate.override_env] = "1"
    hook_path = _hook_paths(meta)[0]
    result = subprocess.run(
        ["/bin/sh", str(hook_path)], cwd=str(meta), capture_output=True, text=True, env=env
    )
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr


# ---------------------------------------------------------------------------
# Foreign-hook preservation — a human-authored post-merge/post-checkout hook
# must survive both a fresh append AND a repeat call (the exact "second
# install silently destroys a foreign hook chain" shape fixed for the
# sibling git_hook_install.py installer in commit 5e5f0d78; see that
# commit's message). This module's `_install_or_append_hook` never takes a
# whole-file-rewrite branch on an EXISTING hook file (only on a genuinely
# absent one), so this test is a regression guard proving that property
# holds for main_post_sync specifically, not just the shared function.
# ---------------------------------------------------------------------------


def test_preexisting_foreign_hook_is_preserved_and_gate_appended(tmp_path, monkeypatch, capsys):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    custom_body = '#!/bin/sh\necho "human-authored hook: do not eat me"\nexit 0\n'
    for hook_path in _hook_paths(meta):
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(custom_body, encoding="utf-8")
        os.chmod(hook_path, 0o755)

    rc = main_post_sync([str(meta)])
    assert rc == 0

    for hook_path in _hook_paths(meta):
        content = hook_path.read_text(encoding="utf-8")
        assert "human-authored hook: do not eat me" in content
        for gate in _POST_SYNC_GATE_REGISTRY:
            assert gate.marker in content
        result = _run_hook(hook_path, meta)
        assert result.returncode == 0
        assert "human-authored hook: do not eat me" in result.stdout
        assert "RAN:coordinator-postsync-marker-resync-check" in result.stdout

    # Repeat call — the exact scenario that destroyed a foreign hook chain
    # in the sibling installer (5e5f0d78): a second install must not
    # reclassify the appended, foreign-carrying file as a stale whole-file
    # shim and rewrite it.
    before = {p: p.read_text(encoding="utf-8") for p in _hook_paths(meta)}
    rc2 = main_post_sync([str(meta)])
    assert rc2 == 0
    after = {p: p.read_text(encoding="utf-8") for p in _hook_paths(meta)}
    assert before == after
    for hook_path in _hook_paths(meta):
        content = hook_path.read_text(encoding="utf-8")
        assert "human-authored hook: do not eat me" in content


def test_registry_names_the_real_gate_script_on_disk():
    """The registry's filename must match the actual script this dispatch
    shipped (coordinator/bin/coordinator-postsync-marker-resync-check) —
    a stale/renamed entry here would be the exact "gate present but dead"
    failure class this module's own history (see its docstring) already
    burned a session on once."""
    real_bin_dir = _mod._bin_dir()
    for gate in _POST_SYNC_GATE_REGISTRY:
        script = real_bin_dir / gate.filename
        assert script.is_file(), f"registered gate script missing on disk: {script}"
        # No exec-bit assertion: gate scripts are invoked ONLY via an
        # explicit interpreter ("$_py" "$_gate_script"), never by shebang/
        # exec — POSIX exec-bit presence on a tracked file is itself a
        # portability defect class (check_posix_exec_assumptions'
        # `mode_100755`), not a property this test should require.
