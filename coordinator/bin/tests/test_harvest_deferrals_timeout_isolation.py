"""test_harvest_deferrals_timeout_isolation.py — regression coverage for
Defect 1 of the 2026-08-15 staff review of coordinator-harvest-deferrals.py:
a hung write-seam child (`subprocess.TimeoutExpired`) must degrade to a
per-row failure, never abandoning the remaining candidate rows or the
summary output.

Spec backlink: dispatch brief "Fix coordinator-harvest-deferrals.py" (staff
review refuting the earlier "per-row try shape" comment — no such try exists
in `_harvest()`; isolation is entirely the `_run_*` helpers' return-False
contract).

Prior behaviour (bug): `_run_queue_append` / `_run_lesson_promote` called
`subprocess.run(..., timeout=_SUBPROCESS_TIMEOUT_SECS)` uncaught — a
`TimeoutExpired` propagated straight through `_harvest()` into `main()`,
abandoning every remaining row and the summary print entirely. On a box
running 50-70 concurrent LLM sessions a hung child is the single most likely
failure mode.

This test is in-process: it monkeypatches `subprocess.run` to raise
`TimeoutExpired` for the write-seam CLI spawn only, and asserts `_harvest()`
still processes every candidate row and returns a `failed` count reflecting
the hung one, rather than raising.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys


def _script_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "coordinator-harvest-deferrals.py",
    )


def _load_harvest_module():
    path = _script_path()
    loader = importlib.machinery.SourceFileLoader(
        "_test_harvest_deferrals_timeout_isolation", path
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_hung_child_on_one_row_does_not_abandon_the_rest():
    """A TimeoutExpired on row 1's write dispatch must not prevent rows 2/3
    from being dispatched, and must not raise out of `_harvest()`."""
    module = _load_harvest_module()

    # Never resolve to None — force both write-seam dispatchers down the
    # "call subprocess.run" path rather than the "CLI not found" early-out.
    module._resolve_cli_cmd = lambda name: [sys.executable, "-c", "pass"]

    calls = {"n": 0}
    real_run = subprocess.run

    def _fake_run(cmd, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 0))

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Ok()

    module.subprocess.run = _fake_run
    module._already_harvested = lambda key, evidence_lines: False
    module._candidate_search_dirs = lambda row: []
    module._collect_evidence_lines = lambda search_dirs: []

    try:
        candidates = [
            {"id": "D1", "title": "Row 1", "change_kind": "code-edit", "surface": "x.py", "body": "Row 1 body."},
            {"id": "D2", "title": "Row 2", "change_kind": "code-edit", "surface": "y.py", "body": "Row 2 body."},
            {"id": "D3", "title": "Row 3", "change_kind": "code-edit", "surface": "z.py", "body": "Row 3 body."},
        ]
        queued_ids, deduped, failed, skipped_unroutable = module._harvest(
            "pln-test-plan-abc123", candidates, dry_run=False
        )
    finally:
        module.subprocess.run = real_run

    assert failed == 1, f"expected exactly one hung-row failure, got {failed}"
    assert queued_ids == ["D2", "D3"], (
        f"expected rows D2/D3 to still be dispatched after D1's hang, got {queued_ids}"
    )
    assert skipped_unroutable == []
    assert calls["n"] == 3, f"expected all 3 rows to reach subprocess.run, got {calls['n']}"
