"""Process-time gate: the recheck-due reader stays under the 500ms brightline with zero spawns."""
import statistics
import subprocess
from datetime import date

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.workday_start.recheck_due import recheck_due

BAR_MS = 500.0


def test_recheck_due_median_under_bar_and_spawns_nothing(tmp_path, monkeypatch):
    for n in range(7):
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / f"g-recheck-due-2026-07-{n + 1:02d}.md").write_text("x")
    spawns: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a))
    today = date(2026, 10, 15)
    probe = tmp_path
    code, text = recheck_due(probe, today)
    assert code == 0 and len(text.splitlines()) == 7
    samples = [
        in_process_time_ms(lambda: recheck_due(probe, today))["process_time_ms"]
        for _ in range(5)
    ]
    median = statistics.median(samples)
    print(f"budget: recheck_due median {median}ms of {samples} (bar {BAR_MS}ms), spawns {len(spawns)}")
    assert median < BAR_MS
    assert spawns == []
