"""dispatch.record_halted_run_landed ``met_by``: per-row shas at or after the halt, recorded on the marker."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def halted(tmp_path):
    """A repo: before-halt commit, the halt commit adding halted.json (blocking R1, R2), then two later commits."""
    _git(tmp_path, "init", "-q")
    before = _commit(tmp_path, "before")
    marker = tmp_path / "state" / "warp" / SOLO / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": None, "blocking": ["R1", "R2"]}),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "--", "state")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    after1 = _commit(tmp_path, "after1")
    after2 = _commit(tmp_path, "after2")
    return tmp_path, marker, before, halt, after1, after2


def _call(root, **params):
    return op._handler({"run_id": SOLO, **params}, root)


def test_met_by_at_and_after_the_halt_is_recorded_and_echoed(halted):
    root, marker, _, halt, after1, after2 = halted
    out = _call(root, sha=after2, met_by={"R1": after1, "R2": halt})
    assert out["ok"], out
    assert out["met_by"] == {"R1": after1, "R2": halt}
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["status"] == "landed" and record["met_by"] == {"R1": after1, "R2": halt}


def test_abbreviated_met_by_sha_is_recorded_full(halted):
    root, marker, _, _, after1, after2 = halted
    out = _call(root, sha=after2, met_by={"R1": after1[:10], "R2": after1})
    assert out["ok"] and out["met_by"] == {"R1": after1, "R2": after1}


def test_met_by_before_the_halt_refuses_naming_the_row(halted):
    root, marker, before, _, _, after2 = halted
    out = _call(root, sha=after2, met_by={"R1": before})
    assert not out["ok"] and "R1" in out["error"] and "before the halt" in out["error"]
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "halted"
    assert "met_by" not in json.loads(marker.read_text(encoding="utf-8"))


def test_one_row_before_the_halt_refuses_the_whole_call(halted):
    root, _, before, _, after1, after2 = halted
    out = _call(root, sha=after2, met_by={"R1": after1, "R2": before})
    assert not out["ok"] and "R2" in out["error"]


def test_met_by_not_ancestor_of_head_refuses_naming_the_row(halted):
    root, _, _, _, after1, after2 = halted
    _git(root, "checkout", "-q", "-b", "side", after1)
    side = _commit(root, "side")
    _git(root, "checkout", "-q", "-")
    out = _call(root, sha=after2, met_by={"R1": side})
    assert not out["ok"] and "R1" in out["error"] and "not an ancestor of HEAD" in out["error"]


def test_met_by_row_that_is_not_blocking_refuses_naming_the_row(halted):
    root, _, _, _, after1, after2 = halted
    out = _call(root, sha=after2, met_by={"R9": after1})
    assert not out["ok"] and "R9" in out["error"] and "not a blocking row" in out["error"]


def _ledger_halt_repo(tmp_path, **marker_keys):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "before")
    marker = tmp_path / "state" / "warp" / SOLO / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": None, **marker_keys}), encoding="utf-8",
    )
    _git(tmp_path, "add", "--", "state")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    after = _commit(tmp_path, "after")
    return tmp_path, marker, after


@pytest.mark.parametrize("key", ["blocking_items", "items"])
def test_ledger_check_blocking_item_row_accepts_met_by(tmp_path, key):
    root, marker, after = _ledger_halt_repo(tmp_path, **{key: [{"id": "R1", "remaining": [], "evidence": ""}]})
    out = _call(root, sha=after, met_by={"R1": after})
    assert out["ok"], out
    assert json.loads(marker.read_text(encoding="utf-8"))["met_by"] == {"R1": after}


def test_ledger_check_halt_still_refuses_a_row_it_did_not_block_on(tmp_path):
    root, marker, after = _ledger_halt_repo(tmp_path, blocking_items=[{"id": "R1"}])
    out = _call(root, sha=after, met_by={"R9": after})
    assert not out["ok"] and "R9" in out["error"] and "not a blocking row" in out["error"]


def test_non_ledger_halt_items_do_not_widen_met_by(tmp_path):
    root, marker, after = _ledger_halt_repo(tmp_path, halted="commit", items=[{"id": "R1"}])
    out = _call(root, sha=after, met_by={"R1": after})
    assert not out["ok"] and "not a blocking row" in out["error"]


@pytest.mark.parametrize(
    "met_by", ["abc1234", ["x"], {"R1": 5}, {"R1": "NOTHEX"}, {"": "abcd"}],
)
def test_malformed_met_by_refuses(halted, met_by):
    root, _, _, _, _, after2 = halted
    out = _call(root, sha=after2, met_by=met_by)
    assert not out["ok"] and "met_by" in out["error"]


def test_blocking_rows_without_met_by_refuse_and_leave_the_marker_halted(halted):
    root, marker, _, _, _, after2 = halted
    out = _call(root, sha=after2)
    assert not out["ok"]
    assert out["error"] == "halt reason stands: blocking rows R1, R2 carry no met_by credit"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "halted"


def test_one_credited_row_still_refuses_the_other(halted):
    root, _, _, _, after1, after2 = halted
    out = _call(root, sha=after2, met_by={"R1": after1})
    assert not out["ok"] and "blocking rows R2 carry" in out["error"]


def test_a_marker_without_blocking_rows_lands_with_no_met_by_key(tmp_path):
    root, marker, after = _ledger_halt_repo(tmp_path)
    out = _call(root, sha=after)
    assert out["ok"] and "met_by" not in out
    assert "met_by" not in json.loads(marker.read_text(encoding="utf-8"))


def _met_review(**over) -> dict:
    criterion = {
        "status": "met", "authored_by": "coordinator:exit-criterion-judge", "observation": "ran it",
        "falsifier": {"output": "3 passed"}, **over,
    }
    return {"criterion": criterion, "tests": {"status": "pass", "run": 3}}


def _gate_repo(tmp_path, monkeypatch, review):
    from coordinator_core.ops.dispatch_emit import ask_contract

    root, marker, after = _ledger_halt_repo(tmp_path, reason="completion gate: criterion not met")
    monkeypatch.setattr(
        ask_contract, "read_run_outcome", lambda r, rid: {"inline_review": review} if review is not None else None,
    )
    return root, marker, after


@pytest.mark.parametrize(
    "review", [None, _met_review(authored_by="coordinator:executor"), _met_review(falsifier={}), _met_review(status="not_met")],
)
def test_a_completion_gate_halt_refuses_without_a_judge_authored_met(tmp_path, monkeypatch, review):
    root, marker, after = _gate_repo(tmp_path, monkeypatch, review)
    out = _call(root, sha=after)
    assert not out["ok"] and out["error"].startswith("halt reason stands: completion gate unmet")
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "halted"


def test_a_completion_gate_halt_lands_once_the_judge_authored_met_is_recorded(tmp_path, monkeypatch):
    root, marker, after = _gate_repo(tmp_path, monkeypatch, _met_review())
    out = _call(root, sha=after)
    assert out["ok"], out
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "landed"
