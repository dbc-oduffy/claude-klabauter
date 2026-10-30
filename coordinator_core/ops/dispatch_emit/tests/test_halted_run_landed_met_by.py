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
    out = _call(root, sha=after2, met_by={"R1": after1[:10]})
    assert out["ok"] and out["met_by"] == {"R1": after1}


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


@pytest.mark.parametrize(
    "met_by", ["abc1234", ["x"], {"R1": 5}, {"R1": "NOTHEX"}, {"": "abcd"}],
)
def test_malformed_met_by_refuses(halted, met_by):
    root, _, _, _, _, after2 = halted
    out = _call(root, sha=after2, met_by=met_by)
    assert not out["ok"] and "met_by" in out["error"]


def test_no_met_by_leaves_the_marker_without_the_key(halted):
    root, marker, _, _, _, after2 = halted
    out = _call(root, sha=after2)
    assert out["ok"] and "met_by" not in out
    assert "met_by" not in json.loads(marker.read_text(encoding="utf-8"))
