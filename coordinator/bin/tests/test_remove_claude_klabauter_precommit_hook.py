"""coordinator/bin/tests/test_remove_claude_klabauter_precommit_hook.py — proves
`coordinator/bin/remove-claude-klabauter-precommit-hook.py` (and its `.cmd` twin)
actually work by SUBPROCESS EXECUTION, in the operator's own invocation
shape, against a throwaway `tmp_path` git repo.

Purpose: AC2 of `docs/plans/2026-08-25-the-staged-rollback-gate-dies-
without-blocking-a-commit.md` chunk C1 — the remover must remain runnable
AFTER a later chunk deletes the gate op and its installer, so this test
proves it by ACTUALLY RUNNING it as a subprocess (never an in-process import
of `main()`), in the same shape an operator invokes it: the `.cmd` launcher
on Windows, `python3 <path>` on POSIX. An import-graph assertion alone would
not catch a launcher body defect or a runtime AttributeError only a real
process boundary exposes — see this module's own module docstring's
"cheap adjunct, never the proof" framing.

Three scenarios, each a fresh subprocess invocation:
    1. A hook carrying the registry banner is removed, rc == 0.
    2. Re-running against the (now-absent) hook is idempotent, rc == 0.
    3. A hook that does NOT carry the banner (operator-authored) is left in
       place untouched, rc == 0.

Negative-spec:
    - Never imports `coordinator_core` — the module under test does not, and
      this test proves that indirectly by running it as an isolated
      subprocess rather than importing it into this test's own interpreter.
    - Does not assert on stdout/stderr wording — only on exit code and the
      hook file's presence/content, which is the actual observable contract.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Real `git`/interpreter subprocess spawns -- cadence-tier, not the per-commit
# fast path. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parents[1]
_SCRIPT_PY = _BIN_DIR / "remove-claude-klabauter-precommit-hook.py"
_SCRIPT_CMD = _BIN_DIR / "remove-claude-klabauter-precommit-hook.cmd"

_BANNER_HOOK_BODY = (
    "#!/bin/sh\n"
    "# claude-klabauter pre-commit gates — fire before drift can land.\n"
    "# Registry-driven (coordinator_core.ops.install_claude_klabauter_precommit_hook);\n"
    "# fake gate body for test purposes only.\n"
    "exit 0\n"
)

_FOREIGN_HOOK_BODY = (
    "#!/bin/sh\n"
    "# operator-authored, not ours.\n"
    "exit 0\n"
)


def _no_console_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        creationflags=_no_console_creationflags(),
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    return repo


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-commit"


def _run_remover(repo: Path) -> subprocess.CompletedProcess:
    """Invoke the remover in the operator's own invocation shape: the `.cmd`
    launcher (bareword-equivalent) on Windows, `python3 <path>` on POSIX."""
    if os.name == "nt":
        argv = [str(_SCRIPT_CMD), str(repo)]
    else:
        argv = [sys.executable, str(_SCRIPT_PY), str(repo)]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        creationflags=_no_console_creationflags(),
    )


def test_removes_hook_carrying_the_registry_banner(tmp_path):
    repo = _make_repo(tmp_path)
    hook = _hook_path(repo)
    hook.write_text(_BANNER_HOOK_BODY, encoding="utf-8")

    result = _run_remover(repo)

    assert result.returncode == 0, result.stderr
    assert not hook.exists()


def test_idempotent_when_no_hook_is_installed(tmp_path):
    repo = _make_repo(tmp_path)
    hook = _hook_path(repo)
    assert not hook.exists()

    result = _run_remover(repo)

    assert result.returncode == 0, result.stderr
    assert not hook.exists()


def test_refuses_to_remove_a_foreign_hook(tmp_path):
    repo = _make_repo(tmp_path)
    hook = _hook_path(repo)
    hook.write_text(_FOREIGN_HOOK_BODY, encoding="utf-8")

    result = _run_remover(repo)

    assert result.returncode == 0, result.stderr
    assert hook.exists()
    assert hook.read_text(encoding="utf-8") == _FOREIGN_HOOK_BODY


def test_module_source_never_imports_coordinator_core():
    source = _SCRIPT_PY.read_text(encoding="utf-8")
    assert "import coordinator_core" not in source
    assert "from coordinator_core" not in source
