"""`fixture_settings_home` moves a fixture repo's engine records under the fixture, off the real store.

Trap: the records dir is keyed by the repo's git common dir under the settings home, so a script that drives
the submit hook (or mints) outside `fixture_settings_home` writes `*-submit-budget-bench-*` run ids into the
operator's REAL `machine-local/engine-records`, where the engine-records tripwire then fails every session.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import _settings_home
from coordinator_core.benchmarks import fixture_settings_home
from coordinator_core.ops.dispatch_emit.engine_record_home import engine_record_dir


def test_a_fixture_repos_engine_records_land_under_the_fixture_home(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    real = _settings_home.machine_local_dir() / "engine-records"
    with fixture_settings_home(tmp_path):
        scoped = engine_record_dir(repo)
    assert scoped is not None
    assert tmp_path.resolve() in scoped.resolve().parents
    assert real.resolve() not in scoped.resolve().parents
