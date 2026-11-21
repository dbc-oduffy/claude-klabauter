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


_BENCH_IMPORT = re.compile(r"from coordinator_core\.benchmarks\.(\w+) import \(?([^)\n]*(?:\n[^)\n]*)*?)\)?\n")


def _is_scoped(text: str, benchmarks: Path = _BENCHMARKS) -> bool:
    """True when `text` scopes the settings home itself, or enters, in a `with`, a helper it imports from a
    benchmark module that does (`live_environment`)."""
    if _SCOPED.search(text):
        return True
    for module, names in _BENCH_IMPORT.findall(text):
        source = benchmarks / f"{module}.py"
        if not source.is_file() or not _SCOPED.search(source.read_text(encoding="utf-8")):
            continue
        for name in re.findall(r"\w+", names):
            if re.search(rf"\bwith\b[^\n]*\b{name}\(", text):
                return True
    return False


def _store_reaching_scripts() -> dict[str, bool]:
    out = {}
    for path in sorted(_BENCHMARKS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "__main__" in text and _STORE_REACH.search(text):
            out[path.name] = _is_scoped(text)
    return out


def test_every_store_reaching_benchmark_script_scopes_its_settings_home() -> None:
    unscoped = {name for name, scoped in _store_reaching_scripts().items() if not scoped}
    new = sorted(unscoped - _UNSCOPED_PENDING)
    assert not new, f"benchmark scripts reach the engine-record store unscoped; wrap main in fixture_settings_home: {new}"


def test_the_unscoped_ratchet_names_only_live_unscoped_scripts() -> None:
    scripts = _store_reaching_scripts()
    stale = sorted(name for name in _UNSCOPED_PENDING if scripts.get(name, True))
    assert not stale, f"scoped or gone; remove from _UNSCOPED_PENDING: {stale}"


_SCOPER = 'os.environ["COORDINATOR_SETTINGS_HOME"] = "x"\n'
_IMPORT = "from coordinator_core.benchmarks.scoper import live_environment\n"


def test_a_helper_imported_from_a_scoping_benchmark_module_and_entered_counts_as_scoping(tmp_path: Path) -> None:
    (tmp_path / "scoper.py").write_text(_SCOPER, encoding="utf-8")
    assert _is_scoped(_IMPORT + "with live_environment(base):\n    run_token.mint()\n", tmp_path)


def test_an_unentered_or_non_scoping_import_stays_unscoped(tmp_path: Path) -> None:
    (tmp_path / "scoper.py").write_text(_SCOPER, encoding="utf-8")
    (tmp_path / "plain.py").write_text("def build_fixture(): ...\n", encoding="utf-8")
    assert not _is_scoped(_IMPORT + "run_token.mint()\n", tmp_path)
    assert not _is_scoped("from coordinator_core.benchmarks.plain import build_fixture\nwith build_fixture(b):\n    run_token.mint()\n", tmp_path)
