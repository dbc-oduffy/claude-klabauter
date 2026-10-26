"""Exact-equality git spawn counts for `dispatch.record_halted_run_landed`.

The op's ancestry and prefix reads are two batched `run_git` spawns; the figures come from
the budget manifest row, never chosen here, so a per-sha or per-path spawn fails these tests
instead of fitting under them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PARENT = "ask-parent-20261015T000000-abcdef"
SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"
REAL_LAUNCH = op._launch


def _budget() -> dict:
    return load_manifest()["overrides"]["dispatch.record_halted_run_landed"]["spawn_count_budget"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit_files(repo: Path, files: dict) -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "files")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    shas = [_commit_files(tmp_path, {f"f{i}": str(i)}) for i in range(3)]
    return tmp_path, shas


def _seed_sibling(root: Path, status: str = "halted") -> None:
    runs = [c.SiblingRun("A", "A", (), "state/sizings/A.yaml", c.STATUS_READY)]
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, tuple(runs)).to_json()), encoding="utf-8")
    st = root / c.status_rel("A")
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"parent_id": PARENT, "status": status}), encoding="utf-8")


def _seed_solo(root: Path, **extra) -> Path:
    d = root / "state" / "warp" / SOLO
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    marker.write_text(
        json.dumps({"status": "halted", "halted": "x", "sizing_rel": None, **extra}), encoding="utf-8"
    )
    return marker


def _count(monkeypatch, root: Path, **params):
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = op._handler(params, root)
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns), [s.argv for s in spawns]
    return out, spawns


def test_sibling_run_full_sha_spawns_one_ancestry_check(repo, monkeypatch):
    root, shas = repo
    _seed_sibling(root)
    out, spawns = _count(monkeypatch, root, run_id="A", sha=shas[0])
    assert out["ok"], out
    assert len(spawns) == _budget()["ancestry_check"], [s.argv for s in spawns]


def test_solo_run_full_sha_spawns_one_ancestry_check(repo, monkeypatch):
    root, shas = repo
    _seed_solo(root)
    out, spawns = _count(monkeypatch, root, run_id=SOLO, sha=shas[0])
    assert out["ok"], out
    assert len(spawns) == _budget()["ancestry_check"], [s.argv for s in spawns]


def test_abbreviated_sha_spawns_prefix_resolve_plus_ancestry_check(repo, monkeypatch):
    root, shas = repo
    _seed_solo(root)
    out, spawns = _count(monkeypatch, root, run_id=SOLO, sha=shas[0][:12])
    assert out["ok"] and out["sha"] == shas[0], out
    budget = _budget()
    assert len(spawns) == budget["ancestry_check"] + budget["prefix_resolve"], [s.argv for s in spawns]


def test_three_sha_set_is_still_one_ancestry_check(repo, monkeypatch):
    root, shas = repo
    _seed_solo(root)
    out, spawns = _count(monkeypatch, root, run_id=SOLO, shas=list(shas))
    assert out["ok"], out
    assert len(spawns) == _budget()["ancestry_check"], [s.argv for s in spawns]


def test_several_withheld_paths_add_no_per_path_spawn(repo, monkeypatch):
    root, _ = repo
    names = {f"docs/w{i}.md": str(i) for i in range(5)}
    landed = _commit_files(root, names)
    marker = _seed_solo(root)
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["run_artifacts"] = {"withheld": {p: "r" for p in names}}
    marker.write_text(json.dumps(data), encoding="utf-8")
    out, spawns = _count(monkeypatch, root, run_id=SOLO, sha=landed)
    assert out["ok"] and sorted(out["covers"][landed]) == sorted(names), out
    assert len(spawns) == _budget()["ancestry_check"], [s.argv for s in spawns]


def test_idempotent_second_call_spawns_nothing(repo, monkeypatch):
    root, shas = repo
    _seed_sibling(root)
    assert op._handler({"run_id": "A", "sha": shas[0]}, root)["ok"]
    out, spawns = _count(monkeypatch, root, run_id="A", sha=shas[0])
    assert out["ok"] and out["written"] is False
    assert spawns == []


def test_full_sha_that_is_not_a_commit_spawns_nothing(repo, monkeypatch):
    root, _ = repo
    _seed_solo(root)
    out, spawns = _count(monkeypatch, root, run_id=SOLO, sha="b" * 40)
    assert not out["ok"] and "not a commit" in out["error"]
    assert spawns == []


def test_a_written_landing_launches_the_close_job_exactly_once(repo, launches):
    root, shas = repo
    _seed_solo(root, plan="docs/plans/p.md", blocking=["R1"], run_base_sha="c" * 40)
    out = op._handler({"run_id": SOLO, "sha": shas[0]}, root)
    assert out["ok"] and out["written"] is True and out["close"]["launched"] is True, out
    assert launches == [SOLO]
