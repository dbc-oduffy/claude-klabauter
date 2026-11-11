"""End-to-end `pre_commit_corpus_artifact_guard.py --install-hook` in a real tmp repo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "pre_commit_corpus_artifact_guard.py"
ENGINE_ROOT = GUARD.parents[2]
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DOE_STUB = (
    "#!/usr/bin/env python3\n"
    "# engine-owned; forwards to the resolved engine root. Edit in claude-klabauter.\n"
    "import os, sys\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib'))\n"
    "from _engine_forward import forward\n"
    "forward('pre_commit_corpus_artifact_guard')\n"
)


def _env() -> dict:
    env = dict(os.environ)
    env["COORDINATOR_ENGINE_ROOT"] = str(ENGINE_ROOT).replace("\\", "/")
    for k in ("CORPUS_ARTIFACT_OK", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(k, None)
    return env


def _run(args, cwd, **kw):
    return subprocess.run(
        args, cwd=cwd, env=_env(), capture_output=True, text=True, timeout=60, creationflags=_FLAGS, **kw
    )


def _install(repo):
    return _run([sys.executable, str(GUARD), "--install-hook"], repo)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _run(["git", "init", "-q"], r)
    _run(["git", "config", "user.email", "t@example.com"], r)
    _run(["git", "config", "user.name", "t"], r)
    return r


def test_stub_is_repaired_and_commit_runs_the_guard(repo):
    from coordinator_core.git.corpus_guard_hook import is_forwarder_stub

    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(DOE_STUB.encode())

    proc = _install(repo)
    assert proc.returncode == 0, proc.stderr
    assert "replaced" in proc.stdout
    assert not is_forwarder_stub(hook.read_text(encoding="utf-8"))
    assert (hook.parent / "pre-commit.retired").read_bytes() == DOE_STUB.encode()

    (repo / "ok.txt").write_text("hello\n")
    _run(["git", "add", "ok.txt"], repo)
    clean = _run(["git", "commit", "-q", "-m", "clean"], repo)
    assert clean.returncode == 0, clean.stderr

    store = repo / ".project-rag-corpus-store"
    store.mkdir()
    (store / "x").write_text("corpus\n")
    _run(["git", "add", "-f", ".project-rag-corpus-store/x"], repo)
    refused = _run(["git", "commit", "-q", "-m", "corpus"], repo)
    assert refused.returncode != 0
    assert "corpus-artifact-guard" in refused.stderr


def test_fresh_install_is_not_a_stub_and_is_idempotent(repo):
    from coordinator_core.git.corpus_guard_hook import STAMP, is_forwarder_stub

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert _install(repo).returncode == 0
    text = hook.read_text(encoding="utf-8")
    assert STAMP in text and not is_forwarder_stub(text)
    before = hook.read_bytes()
    again = _install(repo)
    assert again.returncode == 0 and "current" in again.stdout
    assert hook.read_bytes() == before


def test_foreign_hook_is_left_byte_identical_exit_2(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    foreign = b"#!/bin/sh\necho foreign\n"
    hook.write_bytes(foreign)
    proc = _install(repo)
    assert proc.returncode == 2
    assert str(hook) in proc.stderr or "pre-commit" in proc.stderr
    assert hook.read_bytes() == foreign
