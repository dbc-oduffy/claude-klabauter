"""test_coordinator_safe_commit_remediation.py -- coverage for
`coordinator-safe-commit.py::_scoped_commit_suggestion` (A24,
docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md § C24; and 2026-08-25
break-class fix, `ceremony.scoped_git_commit` killed 2026-08-23 DR-344).

The remediation-suggestion builder used to print a manual retry command
shaped wrong on two axes: (1) it carried `_current_dirty_files()`'s JSON
payload as positional argv, exactly the shape `cc_invoke`'s own
`cc_invoke`/`cc_invoke_bare` moved OFF of (unconditionally) to escape
`WinError 206` on Windows once the payload grows past argv's length cap;
(2) it resolved `PYTHONPATH` via `cc_invoke.resolve_engine_root(__file__)`
-- the LOCATOR axis ("where is THIS co-located script's own tree") -- for a
DISPATCH command, so a dual-boot box pointed the operator's retry at the
source checkout rather than the engine the box actually dispatches to.

2026-08-25: the command itself was also retargeted, break-class. It used to
invoke `python -m coordinator_core.invoke ceremony.scoped_git_commit
--params-file <path> --repo <root> --bare` -- that op was killed 2026-08-23
(DR-344; `coordinator-invoke ceremony.scoped_git_commit ...` now returns
`Method not found: 'ceremony.scoped_git_commit'`), and the `--repo` flag it
passed was independently rejected by DR-279 for a `scope="none"` op. The
suggestion now writes a small retry SCRIPT (not a `--params-file` payload)
that calls `coordinator_core.ops.ceremony.commit_pipeline ::
run_commit_pipeline` in-process -- that function has no op-registry/CLI
entry point -- and emits `<python> <script-path>`.

This suite asserts the printed command (1) never names the killed
`ceremony.scoped_git_commit` method, (2) references the retry script it
wrote to disk via a `.py` tempfile path rather than embedding the params
JSON as positional argv, and (3) resolves `PYTHONPATH`/its module
invocation root off `cc_invoke._resolve_claude_klabauter_root()` (the DISPATCH axis),
not the LOCATOR-axis resolver.

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


def test_retry_command_never_names_the_killed_op(monkeypatch):
    """Break-class regression pin: `ceremony.scoped_git_commit` was killed
    2026-08-23 (DR-344) -- a remediation that still names it hands the
    caller a command the engine's op registry no longer has."""
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py", "c/d.py"])
    monkeypatch.setattr(mod, "_resolve_claude_klabauter_root", lambda: "/fake/dispatch/root")
    monkeypatch.setattr(mod, "_resolve_python_invocation", lambda: ("python3", []))

    suggestion = mod._scoped_commit_suggestion("test subject", host_is_windows=False)

    assert "scoped_git_commit" not in suggestion
    assert "--repo" not in suggestion


def test_retry_command_uses_script_file_not_positional_argv(monkeypatch):
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py", "c/d.py"])
    monkeypatch.setattr(mod, "_resolve_claude_klabauter_root", lambda: "/fake/dispatch/root")
    monkeypatch.setattr(mod, "_resolve_python_invocation", lambda: ("python3", []))

    suggestion = mod._scoped_commit_suggestion("test subject", host_is_windows=False)

    # The params (worktree_root/paths/message) are written into the retry
    # SCRIPT on disk, never embedded as positional argv in the printed
    # command -- only the script's own tempfile path appears.
    assert '"worktree_root"' not in suggestion
    assert '"paths"' not in suggestion
    assert ".py" in suggestion


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


def test_claude_klabauter_root_resolution_failure_message_also_uses_script_file(monkeypatch):
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py"])

    def _raise():
        raise RuntimeError("no engine root")

    monkeypatch.setattr(mod, "_resolve_claude_klabauter_root", _raise)

    suggestion = mod._scoped_commit_suggestion("test subject", host_is_windows=False)

    assert ".py" in suggestion
    assert "scoped_git_commit" not in suggestion
    assert '"worktree_root"' not in suggestion
