"""
coordinator_core.invoke.tests.test_repo_scope_refusal — --repo on a
"none"-scoped op fails loud instead of silently no-opping.

Purpose: pin the DR-279 refusal in coordinator_core/invoke/__main__.py's
main(): --repo is read ONLY inside the WORKTREE_SCOPED_OPS branch, so before
this refusal existed, passing --repo alongside a "none"-scoped op (e.g. ping,
cartography.tree) was silently ignored — the process exited 0, indistinguishable
from --repo having actually steered something. Example-market-data-repo-em ran the
end-to-end survey and reported in good faith that "both --repo forms work — we
tested"; true, and carrying zero information, since neither form does anything
on a "none"-scoped op. This is the regression net for the fix.

Subprocess pattern mirrors coordinator_core/tests/test_invoke_main.py: main()
calls os._exit, so it cannot be exercised in-process.

Spec backlink: docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)

# Portable Windows console-suppression flag — resolves to CREATE_NO_WINDOW
# (0x08000000) on Windows and 0 (no-op) on macOS/Linux.
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_env() -> dict[str, str]:
    """Copy of the current environment with PYTHONPATH set so a subprocess
    started from any cwd can still import coordinator_core."""
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else _PROJECT_ROOT
    return env


def _invoke(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "coordinator_core.invoke", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_PROJECT_ROOT,
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )


def test_repo_flag_refused_on_none_scoped_op():
    """--repo on ping (scope="none") exits non-zero with a message naming the
    op's scope and why the flag is meaningless for it — never a silent no-op."""
    result = _invoke("ping", "{}", "--repo", _PROJECT_ROOT)

    assert result.returncode != 0, (
        f"--repo on a 'none'-scoped op must exit non-zero (fail loud), not "
        f"silently no-op; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip() == "", (
        f"stdout must be empty on the _fatal_stderr refusal path; got {result.stdout!r}"
    )
    assert result.stderr.strip(), "stderr must carry the refusal message"
    parsed_err = json.loads(result.stderr.strip())
    assert "error" in parsed_err, f"Expected a JSON-RPC error envelope; got {parsed_err}"
    message = parsed_err["error"]["message"].lower()
    assert "ping" in message, f"Message must name the op; got {message!r}"
    assert "none" in message, f"Message must name the op's scope ('none'); got {message!r}"
    assert "--repo" in message, f"Message must name the offending flag; got {message!r}"


def test_repo_flag_refused_on_another_none_scoped_op():
    """The refusal is not ping-specific — a second 'none'-scoped op (cartography.tree)
    is refused identically, proving the check keys off the registry scope table, not
    a hardcoded op name."""
    result = _invoke("cartography.tree", "{}", "--repo", _PROJECT_ROOT)

    assert result.returncode != 0, (
        f"--repo on cartography.tree (scope='none') must exit non-zero; got "
        f"{result.returncode}.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed_err = json.loads(result.stderr.strip())
    message = parsed_err["error"]["message"].lower()
    assert "cartography.tree" in message
    assert "none" in message


def test_repo_flag_not_refused_on_worktree_scoped_op():
    """--repo on a worktree-scoped op (coverage.gate, scope="show_top") is honored,
    not refused — the refusal must not regress the legitimate --repo use case."""
    result = _invoke("coverage.gate", "{}", "--repo", _PROJECT_ROOT)

    assert result.returncode in (0, 1), (
        f"coverage.gate with --repo must dispatch normally (exit 0 or 1 from the "
        f"op itself), not be refused; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip(), "stdout must contain a JSON-RPC response (op dispatched)"
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"


def test_no_repo_flag_still_works_on_none_scoped_op():
    """Omitting --repo entirely on a 'none'-scoped op is unaffected — the refusal
    fires only when --repo is actually passed."""
    result = _invoke("ping", "{}")

    assert result.returncode == 0, (
        f"ping without --repo must still exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed["result"].get("ok") is True
