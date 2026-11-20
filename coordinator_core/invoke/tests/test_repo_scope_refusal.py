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

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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
    # This repo is the unstamped dev tree, so every dispatching call needs the
    # stamp-gate carve-out; mirrors test_invoke_main.py::_invoke. The DR-279
    # refusal fires before the gate and is unaffected by the flag.
    cmd = [sys.executable, "-m", "coordinator_core.invoke", *args, "--allow-unstamped-dispatch"]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_PROJECT_ROOT,
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )


def test_repo_flag_accepted_and_ignored_on_none_scoped_op():
    """One recipe serves preflight (engine.drift) and fire: --repo is a no-op, not a refusal."""
    for op in ("ping",):
        result = _invoke(op, "{}", "--repo", _PROJECT_ROOT)
        assert "meaningless" not in result.stderr, result.stderr
        assert result.returncode == 0, (
            f"--repo on none-scoped {op} must be accepted; got {result.returncode}."
            f"stdout: {result.stdout!r} "
            f"stderr: {result.stderr!r}"
        )


def test_repo_falls_back_to_params_when_cwd_not_a_repo(tmp_path):
    from coordinator_core.invoke.__main__ import _resolve_repo_root

    assert _resolve_repo_root(None, str(tmp_path), {"repo": _PROJECT_ROOT}) == Path(_PROJECT_ROOT).resolve()
    assert _resolve_repo_root(None, str(tmp_path), {"cwd": _PROJECT_ROOT}) == Path(_PROJECT_ROOT).resolve()
    # `repo` wins over `cwd`; an explicit --repo wins over both.
    other = tmp_path / "other"
    assert _resolve_repo_root(None, str(tmp_path), {"repo": _PROJECT_ROOT, "cwd": str(other)}) == Path(_PROJECT_ROOT).resolve()
    assert _resolve_repo_root(str(other), str(tmp_path), {"repo": _PROJECT_ROOT}) == other.resolve()


def test_repo_unresolvable_without_params_still_fails(tmp_path):
    from coordinator_core.invoke.__main__ import _resolve_repo_root

    with pytest.raises(SystemExit):
        _resolve_repo_root(None, str(tmp_path), {})


def test_repo_flag_not_refused_on_worktree_scoped_op():
    """--repo on a worktree-scoped op is honored, not refused — the refusal must
    not regress the legitimate --repo use case.

    Vehicle note: this drove `coverage.gate` (scope="show_top") until 2026-08-10.
    That op's cost scales with the repo's review-trail corpus (measured 48s on this
    tree — past the engine's own 30s dispatch timeout AND this file's 30s subprocess
    timeout), so the test became a stopwatch on an unrelated subsystem. Any op in
    WORKTREE_SCOPED_OPS proves the point; `handoff.has_live_children` (common_dir
    class) is worktree-scoped and returns in ~0.1s. Do not restore a
    history-walking op here.
    """
    result = _invoke(
        "handoff.has_live_children",
        '{"candidate": "state/handoffs/does-not-exist-repo-scope-test.md"}',
        "--repo", _PROJECT_ROOT,
    )

    assert result.returncode in (0, 1), (
        f"a worktree-scoped op with --repo must dispatch normally (exit 0 or 1 from the "
        f"op itself), not be refused; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert result.stdout.strip(), "stdout must contain a JSON-RPC response (op dispatched)"
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("jsonrpc") == "2.0"


def test_help_describes_repo_as_optional():
    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_PROJECT_ROOT,
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )
    assert result.returncode == 0, result.stderr
    flat = " ".join(result.stdout.split())
    assert "Required" not in flat
    assert "Optional" in flat
    assert "agent_pipe.send" in flat



def test_no_repo_flag_still_works_on_none_scoped_op():
    result = _invoke("ping", "{}")

    assert result.returncode == 0, (
        f"ping without --repo must still exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed["result"].get("ok") is True
