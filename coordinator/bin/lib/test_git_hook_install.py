"""Regression tests for coordinator.bin.lib.git_hook_install's D3 fix
(2026-07-28, break-class): the unresolvable-interpreter case in the
generated hook shims used to be a silent `[ -n "$_PY" ] || exit 0` — zero
stderr output, asymmetric with the missing-SCRIPT branch two lines below it,
which already prints a loud "commits are NOT being auto-pushed" WARNING.
These tests pin that both cases now announce themselves identically.

See git_hook_install.py's own module docstring (Behavior section) for the
full contract this guards.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_hook_install import _append_block, _shim_body  # noqa: E402


def _make_tool_bindir(tmp_path: Path, tools: dict) -> str:
    """Directory containing ONLY symlinks to `tools` (name -> real absolute
    path) — see coordinator_core/ops/test_install_publish_repo_precommit_hook.py's
    identically-named helper for why reusing a real tool's parent directory
    is unsafe (it can smuggle in other binaries that happen to live next to
    the one you wanted reachable)."""
    bindir = tmp_path / "tool-bindir"
    bindir.mkdir(exist_ok=True)
    for name, real_path in tools.items():
        link = bindir / name
        if not link.exists():
            link.symlink_to(real_path)
    return str(bindir)


def _sh_path(name: str) -> str:
    result = subprocess.run(["/bin/sh", "-c", f"command -v {name}"], capture_output=True, text=True)
    path = result.stdout.strip()
    if not path:
        import pytest

        pytest.skip(f"{name} not found on PATH in this environment")
    return path


def _no_python_path(tmp_path: Path) -> str:
    """A PATH with sh reachable but no python3/python/py binary resolvable."""
    return _make_tool_bindir(tmp_path, {"sh": "/bin/sh"})


# ---------------------------------------------------------------------------
# _shim_body — fresh-install / self-heal shim
# ---------------------------------------------------------------------------

def test_shim_body_missing_interpreter_message_present_in_source():
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    assert "no python3/python/py interpreter found on PATH" in body
    assert "commits are NOT being auto-pushed / annotated by this hook" in body
    # Still exits 0 — a push helper must never block a commit (D3: loud, not fail-closed).
    assert 'exit 0; }' in body


def test_shim_body_missing_interpreter_blocks_loudly_at_runtime(tmp_path):
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    hook = tmp_path / "post-commit"
    hook.write_text(body, encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = _no_python_path(tmp_path)
    result = subprocess.run(["/bin/sh", str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0  # never fail-closed
    assert "WARNING" in result.stderr
    assert "no python3/python/py interpreter found on PATH" in result.stderr


def test_shim_body_missing_interpreter_and_missing_script_read_the_same_shape():
    """The interpreter-missing and script-missing WARNING branches must use
    the same wording contract ("[coordinator] WARNING: hook installed but
    ... commits are NOT being auto-pushed / annotated by this hook") so an
    operator scanning stderr recognizes both as the same class of problem."""
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    assert body.count("[coordinator] WARNING: hook installed but") == 2
    assert body.count("commits are NOT being auto-pushed / annotated by this hook") == 2


# ---------------------------------------------------------------------------
# _append_block — marker-absent append (existing custom hook chain preserved)
# ---------------------------------------------------------------------------

def test_append_block_missing_interpreter_message_present_in_source():
    block = _append_block(
        "/fake/coord/bin",
        "coordinator-auto-push",
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )
    assert "no python3/python/py interpreter found on PATH" in block
    assert "commits are NOT being auto-pushed / annotated by this hook" in block


def test_append_block_missing_interpreter_blocks_loudly_at_runtime(tmp_path):
    block = _append_block(
        "/fake/coord/bin",
        "coordinator-auto-push",
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )
    hook = tmp_path / "post-commit"
    hook.write_text("#!/bin/sh\n" + block + " || true\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = _no_python_path(tmp_path)
    result = subprocess.run(["/bin/sh", str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0  # append blocks never disturb the parent hook's exit status
    assert "WARNING" in result.stderr
    assert "no python3/python/py interpreter found on PATH" in result.stderr


def test_append_block_missing_interpreter_and_missing_script_both_warn():
    block = _append_block(
        "/fake/coord/bin",
        "coordinator-auto-push",
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )
    assert block.count("[coordinator] WARNING: hook installed but") == 2
