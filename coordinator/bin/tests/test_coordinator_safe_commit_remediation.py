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
passed was independently rejected by DR-279 for a `scope="none"` op.

2026-08-26 (C5): the generated retry SCRIPT went too. `do_pathspec`'s
`-- <paths>` form routes through the `ceremony.commit` op in-process, so
the correct retry command is THIS SAME script invoked with `-- <paths>`:
no tempfile, no `PYTHONPATH`, no engine-root resolution, and therefore
nothing left for a killed-op class of drift to attach to.
`_resolve_claude_klabauter_root` is no longer a name on the module at all.

This suite pins the shape that survived: the printed suggestion (1) never
names the killed `ceremony.scoped_git_commit` method, (2) reproduces the
caller's own invocation rather than pointing at a generated artifact, (3)
resolves no engine root on any axis, and (4) renders an absent attribution
signal as "every path unattributed", never as "0 foreign".

Negative-spec: this file no longer covers a tempfile retry script, a
`--params-file` payload, or a `PYTHONPATH` resolution axis -- all three
were deleted, and a test that pins deleted mechanics is the drift this
file's own history is a record of.

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

    suggestion = mod._scoped_commit_suggestion("test subject")

    assert "scoped_git_commit" not in suggestion
    assert "--repo" not in suggestion


def test_retry_command_reproduces_this_same_script_not_a_generated_artifact(monkeypatch):
    """C5's shape: the suggestion is the caller's own invocation, corrected.
    A generated tempfile script is the thing that went stale unnoticed for
    eleven days, so its absence is the property worth pinning -- no `.py`
    artifact path, no params JSON, and the placeholder that keeps the line
    non-executable verbatim."""
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py", "c/d.py"])

    suggestion = mod._scoped_commit_suggestion("test subject")

    # Only the FIRST line is the command; the lines beneath it are the
    # attribution banner, whose entries are dirty paths and may legitimately
    # end in `.py`.
    command_line = suggestion.splitlines()[0]

    assert command_line.strip().startswith("coordinator-safe-commit ")
    assert command_line.endswith("-- <trim-to-your-own-paths>")
    assert ".py" not in command_line
    assert '"worktree_root"' not in suggestion
    assert '"paths"' not in suggestion


def test_retry_command_resolves_no_engine_root_on_any_axis():
    """The suggestion carries no interpreter, no `PYTHONPATH`, and no engine
    root, so neither resolver seam is reachable from it. Pinned as the
    absence of the NAMES: a reintroduced resolution rung would land here
    first, and the previous shape's whole defect class lived on this axis
    choice."""
    mod = _load_cli_module()

    assert not hasattr(mod, "_resolve_claude_klabauter_root")
    assert not hasattr(mod, "resolve_engine_root")


def test_absent_attribution_signal_renders_as_unattributed_not_as_zero_foreign(monkeypatch):
    """A missing `touched.txt` signal must never render as "0 of N are
    foreign" -- a false all-clear manufactured from absent data is the
    failure this banner exists to prevent."""
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_dirty_files", lambda: ["a/b.py", "c/d.py"])
    monkeypatch.setattr(mod, "_own_touched_paths_for_banner", lambda: (None, "no session id"))

    suggestion = mod._scoped_commit_suggestion("test subject")

    assert "attribution unavailable (no session id)" in suggestion
    assert suggestion.count("[foreign/unattributed]") == 2
    assert "0 of 2" not in suggestion
