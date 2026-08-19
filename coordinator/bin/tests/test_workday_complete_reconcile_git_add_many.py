"""bin/tests/test_workday_complete_reconcile_git_add_many.py

Purpose: Guard `workday-complete-reconcile.py`'s `_git_add_many` -- the
Step 2.6 completion-reconcile sweep's batched `git add -- <entries...>` spawn
introduced alongside the other amplification-gate batching sites in commit
e527554b8. No dedicated regression test covered this function before (flagged
by amp-review-s4, WARN finding 1): a revert to the pre-batch per-entry
`_git_add(entry_path)` loop (one `subprocess.run(["git", "add", "--",
entry_path])` spawn per reconciled entry, called from `run_completion_
reconcile`'s `for entry_path in to_git_add` tail) would pass the existing
suite silently.

Test coverage:
  T1  multiple entries reach `git add` in exactly one batched spawn, all
      entries in a single pathspec tail
  T2  an empty entry list never spawns `git add` at all -- an empty pathspec
      after `--` means "everything" to git, so a bare `git add --` would be
      the dangerous degenerate case this guard exists to prevent
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "workday-complete-reconcile.py")


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("_wcr_git_add_many_under_test", _CLI)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_git_add_many_batches_single_call(monkeypatch):
    """`_git_add_many` must spawn exactly one `git add -- <entries...>` for
    the whole reconciled-entry set, not one call per entry.

    Fails against the pre-batch shape (`_git_add(entry_path)` called once
    per entry inside `run_completion_reconcile`'s loop, each spawning
    `git add -- <entry_path>`): that shape would produce 3 `subprocess.run`
    calls here, each carrying a single entry, so both the call-count
    assertion and the single-batched-argv assertion below fail on it.
    """
    mod = _load_cli_module()
    calls = []

    def fake_run(cmd, **kwargs):  # popup-safe-env-suppressed: stub, no real spawn
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._git_add_many(["a.md", "b.md", "c.md"])

    assert len(calls) == 1, f"expected one batched add spawn, got {calls}"
    assert calls[0] == ["git", "add", "--", "a.md", "b.md", "c.md"]


def test_git_add_many_empty_list_never_spawns_bare_add(monkeypatch):
    """Empty-input guard: an empty pathspec list after `--` means
    'everything' to git, so a bare `git add --` degenerate call would stage
    every dirty/untracked file in the tree -- `_git_add_many` must return
    before spawning anything when `entry_paths` is empty.

    Note (per-report): unlike the test above, this one does NOT
    discriminate against the pre-batch per-entry loop -- a `for e in []:`
    loop also spawns zero calls, so both shapes pass it. Kept anyway
    because it pins a real safety guard the review named explicitly; the
    batching regression itself is covered by the test above.
    """
    mod = _load_cli_module()
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        # popup-safe-env-suppressed: stub, no real spawn
        lambda *a, **k: calls.append((a, k)) or subprocess.CompletedProcess(a, 0),
    )
    mod._git_add_many([])
    assert calls == []
