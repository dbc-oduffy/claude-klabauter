
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.test_baton_assemble import _git, _init_repo, _write_artifact

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

    assert elapsed_process_ms < 1500, (
        f"d6 fan-out at N=3 cost {elapsed_process_ms:.1f}ms of process time -- "
        "over budget; file a defect against baton_assemble, this is not a "
        "reason to rebuild the fan-in plan"
    )
    assert spawn_count["n"] < 40, (
        f"d6 fan-out at N=3 spawned {spawn_count['n']} processes -- "
        "unexpectedly high; investigate before assuming N=5 is safe"
    )
