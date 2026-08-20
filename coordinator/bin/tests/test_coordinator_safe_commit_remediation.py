"""test_coordinator_safe_commit_remediation.py -- coverage for
`coordinator-safe-commit.py::_scoped_commit_suggestion` (A24,
docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md § C24).

The remediation-suggestion builder used to print a manual retry command
shaped wrong on two axes: (1) it carried `_current_dirty_files()`'s JSON
payload as positional argv, exactly the shape `cc_invoke`'s own
`cc_invoke`/`cc_invoke_bare` moved OFF of (unconditionally) to escape
`WinError 206` on Windows once the payload grows past argv's length cap;
(2) it resolved `PYTHONPATH` via `cc_invoke.resolve_engine_root(__file__)`
-- the LOCATOR axis ("where is THIS co-located script's own tree") -- for a
DISPATCH command (`python -m coordinator_core.invoke ...`), so a dual-boot
box pointed the operator's retry at the source checkout rather than the
engine the box actually dispatches to.

This suite asserts the printed command now (1) uses `--params-file`, never
positional params argv, and (2) resolves `PYTHONPATH`/its module invocation
root off `cc_invoke._resolve_claude_klabauter_root()` (the DISPATCH axis), not the
LOCATOR-axis resolver.

Loaded by file path (`importlib.machinery.SourceFileLoader`), matching this
directory's existing hyphenated-module idiom (see
test_coordinator_safe_commit_pathspec_batch.py).

Run:
    pytest coordinator/bin/tests/test_coordinator_safe_commit_remediation.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_remediation_no_longer_imports_locator_axis_resolver():
    """The LOCATOR-axis symbol must not even be on the module -- this is a
    DISPATCH command, and reintroducing the import is the exact regression
    this suite guards against."""
    mod = _load_cli_module()
    assert not hasattr(mod, "resolve_engine_root")


def test_retry_command_uses_params_file_not_positional_argv(monkeypatch):
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py", "c/d.py"])
    monkeypatch.setattr(mod, "_resolve_claude_klabauter_root", lambda: "/fake/dispatch/root")
    monkeypatch.setattr(mod, "_resolve_python_invocation", lambda: ("python3", []))

    suggestion = mod._scoped_commit_suggestion("test subject", host_is_windows=False)

    assert "--params-file" in suggestion
    # The params JSON payload (worktree_root/paths/message keys) must never
    # appear as a positional argv token -- only via the --params-file path.
    assert '"worktree_root"' not in suggestion
    assert '"paths"' not in suggestion


def test_retry_command_resolves_pythonpath_via_dispatch_axis(monkeypatch):
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py"])
    calls: list[str] = []

    def _fake_dispatch_root():
        calls.append("dispatch")
        return "/fake/dispatch/root"

    monkeypatch.setattr(mod, "_resolve_claude_klabauter_root", _fake_dispatch_root)
    monkeypatch.setattr(mod, "_resolve_python_invocation", lambda: ("python3", []))

    suggestion_posix = mod._scoped_commit_suggestion("test subject", host_is_windows=False)
    assert calls == ["dispatch"]
    assert "/fake/dispatch/root" in suggestion_posix

    calls.clear()
    suggestion_win = mod._scoped_commit_suggestion("test subject", host_is_windows=True)
    assert calls == ["dispatch"]
    assert "/fake/dispatch/root" in suggestion_win


def test_claude_klabauter_root_resolution_failure_message_also_uses_params_file(monkeypatch):
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py"])

    def _raise():
        raise RuntimeError("no engine root")

    monkeypatch.setattr(mod, "_resolve_claude_klabauter_root", _raise)

    suggestion = mod._scoped_commit_suggestion("test subject", host_is_windows=False)

    assert "--params-file" in suggestion
    assert '"worktree_root"' not in suggestion
