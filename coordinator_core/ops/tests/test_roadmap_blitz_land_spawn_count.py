"""Exact-equality spawn count for `roadmap.blitz_land`'s shipped_in reachability check.

One `git rev-list <shas> --not HEAD` covers every baton of a wave, so the count does not
grow with the number of batons. The figure is read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import roadmap_blitz_land
from coordinator_core.session import record_homes
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _budget() -> dict:
    return load_manifest()["overrides"]["roadmap.blitz_land"]["spawn_count_budget"]


def _baton(root, stub_id: str) -> None:
    path = Path(record_homes.record_path(str(root), "handoffs", f"{stub_id}.md"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nkind: roadmap-baton\ntitle: t\nstub_id: %s\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\n---\n\nbody\n" % stub_id,
        encoding="utf-8",
    )


def _routed_xs_wave(tmp_path, count: int) -> dict:
    _git(tmp_path, "init", "-q", "-b", "main")
    entries = []
    for i in range(count):
        stub_id = f"x-{i}"
        _baton(tmp_path, stub_id)
        sizing = tmp_path / "state" / "sizings" / f"2026-10-22-x-{i}.yaml"
        sizing.parent.mkdir(parents=True, exist_ok=True)
        sizing.write_text(f"deliverable_id: dlv-x-{i}-00000{i}\nstatus: open\n", encoding="utf-8")
        baton = Path(record_homes.record_path(str(tmp_path), "handoffs", f"{stub_id}.md"))
        baton.write_text(
            baton.read_text(encoding="utf-8").replace(
                "baton_role: work", f"baton_role: work\ndeliverable_id: dlv-x-{i}-00000{i}"
            ),
            encoding="utf-8",
        )
        entries.append({"batonId": stub_id, "route": "dispatch", "tshirt": "XS"})
    return {"waveIndex": 0, "routedElsewhere": entries}


def test_two_unsized_xs_batons_spawn_one_batched_grep(tmp_path, monkeypatch):
    wave_result = _routed_xs_wave(tmp_path, 2)

    with _count_spawns_attributed(monkeypatch) as spawns:
        out = roadmap_blitz_land._handler({"wave_result": wave_result}, repo_root=tmp_path)

    assert len(out["xsLinked"]) == 2 and not out["refused"], out
    assert len(spawns) == _budget()["xs_sizing_match"], [s.argv for s in spawns]
    assert all("grep" in s.argv for s in spawns), [s.argv for s in spawns]


def test_a_wave_with_no_unsized_xs_batons_spawns_no_grep(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _baton(tmp_path, "s-0")
    wave_result = {
        "waveIndex": 0,
        "routedElsewhere": [{"batonId": "s-0", "route": "dispatch", "tshirt": "S"}],
    }

    with _count_spawns_attributed(monkeypatch) as spawns:
        roadmap_blitz_land._handler({"wave_result": wave_result}, repo_root=tmp_path)

    assert not [s for s in spawns if "grep" in s.argv], [s.argv for s in spawns]


def test_a_four_baton_wave_spawns_one_rev_list_in_total(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "landed")
    landed = _git(tmp_path, "rev-parse", "HEAD")
    ids = [f"b-{i}" for i in range(4)]
    for stub_id in ids:
        _baton(tmp_path, stub_id)
    wave_result = {"waveIndex": 0, "ready": [{"batonId": i, "route": "dispatch"} for i in ids]}

    with _count_spawns_attributed(monkeypatch) as spawns:
        out = roadmap_blitz_land._handler(
            {"wave_result": wave_result, "shipped_in": landed}, repo_root=tmp_path
        )

    assert [r["closed"] for r in out["closed"]] == [True] * 4 and not out["refused"], out
    assert len(spawns) == _budget()["shipped_in_reachability"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("rev-list" in s.argv for s in spawns), [s.argv for s in spawns]
