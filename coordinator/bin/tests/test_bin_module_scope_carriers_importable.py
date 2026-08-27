"""Sweep pin: every named module-scope carrier under `coordinator/bin` must
import cleanly under pytest.

Why this exists: 04804f6c9 landed green over five broken modules (the
`ProvenanceDivergenceError` false-positive on the source-tree/published-mirror
pair, plus the stale-mirror `op_trampoline` shadow — see
`coordinator/bin/lib/cc_invoke.py::require_dispatch_engine_on_path`'s
SOURCE-TWIN CARVE-OUT section and the repo-root `conftest.py`'s
"Source-tree coordinator/bin/lib precedence" block for the two fixes) because
no test pinned "every `coordinator/bin` CLI is importable under pytest" — each
broken module only surfaced as a collection error in ITS OWN already-existing
test file, and nothing generalized the assertion to the modules that had no
such test yet.

This sweep loads each of the 13 named carriers by path (`importlib.util`,
mirroring the existing per-CLI test pattern in this directory — e.g.
`test_percolate_mirror.py::_load_module`) and asserts the import raises
nothing. It intentionally does NOT assert anything about each module's
behavior — that is each carrier's own test file's job; this is purely an
import-time tripwire.

Negative-spec: does NOT enumerate every `coordinator/bin/*.py` file (that is
a much larger, slower sweep tracked separately); this pins exactly the 13
carriers named in the dispatch brief that motivated this fix.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

# (module-name-for-loading, filename) — filename is the exact on-disk stem;
# "workday-start-step0" has a same-prefix sibling
# (workday-start-step0-reconcile.py) so the exact filename is spelled out
# rather than glob-matched.
_CARRIERS = [
    ("baton_drift_sweep", "baton-drift-sweep.py"),
    ("coordinator_harvest_deferrals", "coordinator-harvest-deferrals.py"),
    ("coordinator_lesson_promote", "coordinator-lesson-promote.py"),
    ("cruft_sweep", "cruft-sweep.py"),
    ("day_coverage_sweep", "day-coverage-sweep.py"),
    ("handoff_deliverable_carry", "handoff-deliverable-carry.py"),
    ("percolate_mirror", "percolate-mirror.py"),
    ("query_records", "query-records.py"),
    ("reap_stale_subagent_sidecars", "reap-stale-subagent-sidecars.py"),
    ("snippet_registry", "snippet-registry.py"),
    ("verify_orientation_cache_sync", "verify-orientation-cache-sync.py"),
    ("verify_snippet_sync", "verify-snippet-sync.py"),
    ("workday_start_step0", "workday-start-step0.py"),
]


def _load_by_path(module_name: str, filename: str):
    path = _BIN_DIR / filename
    assert path.is_file(), f"carrier missing on disk: {path}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Registered in sys.modules BEFORE exec, not just returned: a carrier
    # defining a `@dataclass` needs `sys.modules[cls.__module__]` resolvable
    # during class-body execution (stdlib `dataclasses._is_type` does a
    # module lookup by name, not by the module object in hand) — omitting
    # this raises `AttributeError: 'NoneType' object has no attribute
    # '__dict__'` from inside `dataclasses.py`, unrelated to the carrier's
    # own code, for any carrier that happens to use a string-annotated
    # dataclass field.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


@pytest.mark.parametrize("module_name,filename", _CARRIERS, ids=[c[1] for c in _CARRIERS])
def test_carrier_imports_without_raising(module_name: str, filename: str) -> None:
    _load_by_path(module_name, filename)
