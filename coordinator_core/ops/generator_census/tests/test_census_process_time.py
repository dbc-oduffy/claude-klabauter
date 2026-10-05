"""Budget pin for the declared-pair census on the live tree (DR-344, one bar).

Warm: a populated cache answers in <=300ms in-process CPU with zero spawns.
Cold: an empty cache dir answers in <=500ms process time, child CPU included,
net of the no-op interpreter baseline measured by the same primitive. A red
leg is a defect report, never a looser bar.
"""

from __future__ import annotations

import statistics
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    in_process_time_ms,
    single_invocation_tree_process_time,
)
from coordinator_core.session import machinery_paths

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REPO_ROOT = Path(__file__).resolve().parents[4]
WARM_BAR_MS = 300.0
COLD_BAR_MS = 500.0
COLD_RUNS = 5

_SPAWN_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.posix_spawn",
        "os.spawn",
        "os.startfile",
    }
)

# The child redirects the machinery cache dir to an empty directory before the
# census package is imported, so every blob lookup misses.
_COLD_CHILD = textwrap.dedent(
    """
    import sys
    from coordinator_core.session import machinery_paths

    empty = sys.argv[2]
    machinery_paths.cache_dir = lambda repo_root: empty

    from pathlib import Path
    from coordinator_core.ops.generator_census import census

    records = census(Path(sys.argv[1]))
    assert records, "census returned no records"
    """
)


def _median_tree_ms(cmd: list[str], runs: int) -> tuple[float, list[float]]:
    samples = []
    for _ in range(runs):
        result = single_invocation_tree_process_time(cmd, cwd=str(REPO_ROOT))
        assert result["rc"] == 0, f"measured command failed: {cmd}"
        samples.append(result["process_time_ms"])
    return statistics.median(samples), samples


def test_warm_census_under_bar_with_zero_spawns(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(machinery_paths, "cache_dir", lambda repo_root: cache)

    from coordinator_core.ops.generator_census import census

    populated = census(REPO_ROOT)
    assert populated, "census returned no records on the live tree"

    spawns: list[str] = []
    armed = False

    def _hook(event: str, _args) -> None:
        if armed and event in _SPAWN_EVENTS:
            spawns.append(event)

    sys.addaudithook(_hook)
    armed = True
    try:
        result = in_process_time_ms(lambda: census(REPO_ROOT))
    finally:
        armed = False

    print(f"warm census: {result['process_time_ms']:.1f}ms over k={result['k']}")
    assert spawns == [], f"warm census spawned: {spawns}"
    assert result["process_time_ms"] <= WARM_BAR_MS, result


def test_cold_census_under_bar_child_inclusive(tmp_path):
    baseline, _ = _median_tree_ms([sys.executable, "-c", "pass"], COLD_RUNS)

    gross_samples = []
    for i in range(COLD_RUNS):
        empty = tmp_path / f"empty-{i}"
        empty.mkdir()
        result = single_invocation_tree_process_time(
            [sys.executable, "-c", _COLD_CHILD, str(REPO_ROOT), str(empty)],
            cwd=str(REPO_ROOT),
        )
        assert result["rc"] == 0, "cold census child failed"
        gross_samples.append(result["process_time_ms"])

    gross = statistics.median(gross_samples)
    net = gross - baseline
    print(
        f"cold census: raw {gross:.1f}ms net {net:.1f}ms "
        f"(baseline {baseline:.1f}ms, samples {gross_samples})"
    )
    assert net <= COLD_BAR_MS, (gross, net, baseline, gross_samples)
