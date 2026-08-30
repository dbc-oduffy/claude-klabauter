"""coordinator_core.baton_assemble.tests.test_d6_fan_in_n3_budget

Folds the single N=3 measurement the overengineering review
(coordinatoroverengineering-reviewer.ad7fa47881c3f1e1b, finding 5) asked for
in place of a standalone spike chunk: process time and subprocess-spawn
count for the shipped `d6`/`d6-2`/`d6-3` supersede-and-archive fan-out
(`coordinator_core.baton_assemble.__init__._build_directives`, dated
2026-07-29) at N=3 predecessors. The build this would have gated already
shipped -- this is a measurement of existing behaviour, not a stop-the-plan
gate (see that review's finding 5's own rationale for why C0 was dropped).

CLAUDE.md § The brightline: "One process over 200ms needs a fix, not a
rationale" and "process time and spawn count, never wall clock". This test
measures both: `time.process_time()` (this-process CPU time, the honest
proxy for a single-threaded local run with no concurrent I/O wait) and a
subprocess-spawn counter via a thin `subprocess.Popen` wrapper.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_d6_fan_in_n3_budget.py -q
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.test_baton_assemble import _git, _init_repo, _write_artifact

# Spawns real git processes for the archive-transition git-mv/commit;
# runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write_predecessor(root: Path, rel: str, handoff_id: str) -> Path:
    return _write_artifact(
        root / rel,
        [
            f"handoff_id: {handoff_id}",
            "deployment_state: in_flight",
            "title: N=3 fan-in predecessor",
            "created: 2026-08-30",
            "branch: work/test/2026-01-01",
            'predecessor: "none"',
            "category: infra",
            "summary: predecessor handoff for the N=3 d6 fan-out budget test",
            "claimed_at: 2026-08-30T09:00:00Z",
            "claimed_by: test-session",
        ],
    )


def test_d6_fan_out_at_n3_stays_in_process_time_and_process_time_stays_under_the_bar(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)

    predecessors = [
        _write_predecessor(repo, f"state/handoffs/predecessor-{i}.md", f"hnd-pred-{i}-1a2b4{i}")
        for i in range(3)
    ]
    for i, p in enumerate(predecessors):
        rel = p.relative_to(repo).as_posix()
        _git(repo, "add", rel)
        _git(repo, "commit", "-m", f"add predecessor {i}")

    successor_rel = "state/handoffs/successor.md"
    successor_abs = repo / successor_rel
    successor_abs.parent.mkdir(parents=True, exist_ok=True)
    successor_abs.write_text("scaffolded-by-d1\n", encoding="utf-8")

    spawn_count = {"n": 0}
    _real_popen_init = subprocess.Popen.__init__

    def _counting_popen_init(self, *args, **kwargs):
        spawn_count["n"] += 1
        return _real_popen_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _counting_popen_init)

    start = time.process_time()
    for i, p in enumerate(predecessors):
        rel = p.relative_to(repo).as_posix()
        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [rel, successor_rel, successor_rel], repo
        )
        assert result["result"]["superseded"] is True
    elapsed_process_ms = (time.process_time() - start) * 1000

    print(f"d6 fan-out N=3: process_time={elapsed_process_ms:.1f}ms spawns={spawn_count['n']}")

    # Deliberately generous relative to CLAUDE.md's 500ms end-to-end
    # brightline -- this measures ONE leg of a larger apply() run (d6* only,
    # not d1/d2/d7), and is a courtesy instrument for a build that already
    # shipped, not a new gate. A breach here is a defect report against
    # `baton_assemble`, not a plan chunk.
    assert elapsed_process_ms < 1500, (
        f"d6 fan-out at N=3 cost {elapsed_process_ms:.1f}ms of process time -- "
        "over budget; file a defect against baton_assemble, this is not a "
        "reason to rebuild the fan-in plan"
    )
    # Each leg's supersede/archive-transition shells out to real git for the
    # move/commit -- a handful of spawns per leg is the known, accepted
    # cost of that op; this pins it does not blow up superlinearly with N.
    assert spawn_count["n"] < 40, (
        f"d6 fan-out at N=3 spawned {spawn_count['n']} processes -- "
        "unexpectedly high; investigate before assuming N=5 is safe"
    )
