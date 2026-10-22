"""Process-time gate: the watch-state ignore check stays under the 500ms brightline, 0 spawns clean."""
import statistics
import subprocess

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workday_start import runtime_ignore_set as rset
from coordinator_core.workday_start.watch_state_ignore import watch_state_ignore

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BAR_MS = 500.0
TRACKED_FILES = 3000


def _git(root, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr


def test_watch_state_ignore_median_under_bar_and_spawns_nothing(tmp_path, monkeypatch):
    rules = ("state/group-em-watch.json", ".coordinator-local/", "state/**/*.fire.mjs")
    template = tmp_path.parent / f"{tmp_path.name}-gitignore.template"
    template.write_text("\n".join(rules) + "\n", encoding="utf-8")
    monkeypatch.setattr(rset, "resolve_template_path", lambda: template)
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("\n".join(rules) + "\n", encoding="utf-8")
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    for n in range(TRACKED_FILES):
        (bulk / f"f{n}.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    spawns: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a))
    assert watch_state_ignore(tmp_path) == (0, "")
    samples = [
        in_process_time_ms(lambda: watch_state_ignore(tmp_path))["process_time_ms"]
        for _ in range(5)
    ]
    median = statistics.median(samples)
    print(f"budget: watch_state_ignore median {median}ms of {samples} (bar {BAR_MS}ms), spawns {len(spawns)}")
    assert median < BAR_MS
    assert spawns == []
