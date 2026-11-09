"""A benchmark script that drives the engine-record store scopes the settings home to its fixture.

Run tokens and the live-run index resolve under the settings home, never the fixture repo. pytest
is quarantined by `conftest._quarantine_real_home`, but a script-mode run has no harness: without
`benchmarks.fixture_settings_home` it writes into the operator's REAL machine-local store, which
every live engine scan reads (2026-11-04: 56 leaked fixture homes, 28k tokens, 3.3s hook
round-trips, from `land_run_leg_budget_profile` script mode).

`_UNSCOPED_PENDING` is a shrinking ratchet: an entry that becomes scoped must leave the list.
"""

from __future__ import annotations

import re
from pathlib import Path

_BENCHMARKS = Path(__file__).resolve().parents[1]

# Names a module reaches the engine-record store through (directly, or via a land/commit handler).
_STORE_REACH = re.compile(r"\b(run_token|land_run|commit_v2|warp_live|engine_record_home|submit_engine_ask|live_run_index|live_run_writes|minted_parent|engine_record_dir)\b")
_SCOPED = re.compile(r"fixture_settings_home\(|COORDINATOR_SETTINGS_HOME")

_UNSCOPED_PENDING: frozenset[str] = frozenset()


def _store_reaching_scripts() -> dict[str, bool]:
    out = {}
    for path in sorted(_BENCHMARKS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "__main__" in text and _STORE_REACH.search(text):
            out[path.name] = bool(_SCOPED.search(text))
    return out


def test_every_store_reaching_benchmark_script_scopes_its_settings_home():
    unscoped = {name for name, scoped in _store_reaching_scripts().items() if not scoped}
    new = sorted(unscoped - _UNSCOPED_PENDING)
    assert not new, f"benchmark scripts reach the engine-record store unscoped; wrap main in fixture_settings_home: {new}"


def test_the_unscoped_ratchet_names_only_live_unscoped_scripts():
    scripts = _store_reaching_scripts()
    stale = sorted(name for name in _UNSCOPED_PENDING if scripts.get(name, True))
    assert not stale, f"scoped or gone; remove from _UNSCOPED_PENDING: {stale}"
