"""Tests for coordinator_core.ops.install_meta_repo_precommit_hook.main_install_all
— the single install-time entrypoint that drives BOTH the sending-side
`pre-commit` gate (`main`/`_GATE_REGISTRY`) and the receiving-side
`post-merge`/`post-checkout` gates (`main_post_sync`/
`_POST_SYNC_GATE_REGISTRY`) in one call.

Why this file exists: `main_post_sync` shipped fully built and fully tested
(`test_install_post_sync_hooks.py`) with no install-time call site anywhere
in the tree — the only launcher
(`coordinator/bin/install-meta-repo-precommit-hook.py`) imported bare
`main`, never `main_post_sync`. `main_install_all` is the fix: the launcher
now imports it instead, so there is exactly one call site and it installs
all three hooks together. These tests prove the COMBINED entrypoint's
contract (all three hooks land from one call, idempotent, identity-gated,
foreign content preserved) — the per-hook mechanics themselves are already
characterized by `test_install_meta_repo_precommit_hook.py` (pre-commit) and
`test_install_post_sync_hooks.py` (post-merge/post-checkout); this file does
not re-derive them.

Spec backlink: see `main_install_all`'s own docstring in
install_meta_repo_precommit_hook.py for the incident this closes.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.install_meta_repo_precommit_hook import (
    _GATE_REGISTRY,
    _POST_SYNC_GATE_REGISTRY,
    _POST_SYNC_HOOK_FILENAMES,
    main_install_all,
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


def _all_hook_paths(meta_repo: Path) -> list[Path]:
    names = ["pre-commit", *_POST_SYNC_HOOK_FILENAMES]
    return [meta_repo / ".git" / "hooks" / name for name in names]


def _write_stub_gates(fake_bin: Path) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    for gate in [*_GATE_REGISTRY, *_POST_SYNC_GATE_REGISTRY]:
        script = fake_bin / gate.filename
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"print('RAN:{gate.filename}')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        os.chmod(script, 0o755)


def _run_hook(hook: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(hook)], cwd=str(cwd), capture_output=True, text=True
    )


def test_fresh_install_writes_all_three_hooks(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    rc = main_install_all([str(meta)])
    assert rc == 0

    for hook_path in _all_hook_paths(meta):
        assert hook_path.is_file(), f"expected {hook_path} to be installed"
        if os.name != "nt":
            assert os.access(hook_path, os.X_OK)
        result = _run_hook(hook_path, meta)
        assert result.returncode == 0

    pre_commit = meta / ".git" / "hooks" / "pre-commit"
    for gate in _GATE_REGISTRY:
        assert gate.marker in pre_commit.read_text(encoding="utf-8")
    for hook_name in _POST_SYNC_HOOK_FILENAMES:
        content = (meta / ".git" / "hooks" / hook_name).read_text(encoding="utf-8")
        for gate in _POST_SYNC_GATE_REGISTRY:
            assert gate.marker in content


def test_idempotent_on_second_call(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    assert main_install_all([str(meta)]) == 0
    before = {p: p.read_text(encoding="utf-8") for p in _all_hook_paths(meta)}

    rc = main_install_all([str(meta)])
    assert rc == 0
    after = {p: p.read_text(encoding="utf-8") for p in _all_hook_paths(meta)}
    assert before == after


def test_preexisting_foreign_hooks_preserved_across_all_three(tmp_path, monkeypatch):
    meta = _make_meta_repo(tmp_path, monkeypatch)
    fake_bin = tmp_path / "fakebin"
    monkeypatch.setattr(_mod, "_bin_dir", lambda: fake_bin)
    _write_stub_gates(fake_bin)

    custom_body = '#!/bin/sh\necho "human-authored hook: do not eat me"\nexit 0\n'
    for hook_path in _all_hook_paths(meta):
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(custom_body, encoding="utf-8")
        os.chmod(hook_path, 0o755)

    assert main_install_all([str(meta)]) == 0
    for hook_path in _all_hook_paths(meta):
        content = hook_path.read_text(encoding="utf-8")
        assert "human-authored hook: do not eat me" in content

    # Repeat call must not reclassify the appended, foreign-carrying file as
    # a stale whole-file shim and rewrite it (the 5e5f0d78 defect shape).
    before = {p: p.read_text(encoding="utf-8") for p in _all_hook_paths(meta)}
    assert main_install_all([str(meta)]) == 0
    after = {p: p.read_text(encoding="utf-8") for p in _all_hook_paths(meta)}
    assert before == after


def test_target_not_a_git_repo_touches_nothing(tmp_path):
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    rc = main_install_all([str(notgit)])
    assert rc == 0
    assert not (notgit / ".git").exists()


def test_target_is_git_repo_but_not_meta_repo_touches_nothing(tmp_path, monkeypatch):
    somerepo = tmp_path / "somerepo"
    somerepo.mkdir()
    _git_init(somerepo)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    rc = main_install_all([str(somerepo)])
    assert rc == 0
    for name in ("pre-commit", *_POST_SYNC_HOOK_FILENAMES):
        assert not (somerepo / ".git" / "hooks" / name).exists()
