"""dispatch.record_halted_run_landed ``keep_open``: credit rows on a halted marker without landing the run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.rebind_contract import KEEP_OPEN
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
    """A repo: before-halt commit, the halt commit adding halted.json (blocking R1, R2, R3), then two later commits."""
    _git(tmp_path, "init", "-q")
    before = _commit(tmp_path, "before")
    marker = tmp_path / "state" / "warp" / SOLO / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": None, "blocking": ["R1", "R2", "R3"]}),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "--", "state")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    after1 = _commit(tmp_path, "after1")
    after2 = _commit(tmp_path, "after2")
    return tmp_path, marker, before, after1, after2


def _call(root, **params):
    return op._handler({"run_id": SOLO, KEEP_OPEN: True, **params}, root)


def _record(marker: Path) -> dict:
    return json.loads(marker.read_text(encoding="utf-8"))


def test_keep_open_credits_one_row_and_leaves_the_run_halted(halted):
    root, marker, _, after1, _ = halted
    out = _call(root, met_by={"R2": after1})
    assert out == {"ok": True, "run_id": SOLO, "written": False, "kept_open": True, "met_by": {"R2": after1}}
    record = _record(marker)
    assert record["status"] == "halted" and record["met_by"] == {"R2": after1}


def test_two_keep_open_calls_keep_both_credits(halted):
    root, marker, _, after1, after2 = halted
    assert _call(root, met_by={"R2": after1})["ok"]
    out = _call(root, met_by={"R3": after2[:10]})
    assert out["ok"] and out["met_by"] == {"R2": after1, "R3": after2}
    assert _record(marker)["met_by"] == {"R2": after1, "R3": after2}


def test_later_landing_without_keep_open_sees_the_credited_row(halted):
    root, marker, _, after1, after2 = halted
    assert _call(root, met_by={"R2": after1})["ok"]
    out = op._handler({"run_id": SOLO, "sha": after2, "met_by": {"R1": after1, "R3": after1}}, root)
    assert out["ok"], out
    assert _record(marker)["status"] == "landed"
    assert _record(marker)["met_by"] == {"R1": after1, "R3": after1}


def test_without_keep_open_the_landing_path_is_unchanged(halted):
    root, marker, _, after1, after2 = halted
    out = op._handler({"run_id": SOLO, "sha": after2, "met_by": {"R1": after1}}, root)
    assert not out["ok"] and "halt reason stands" in out["error"]
    assert _record(marker)["status"] == "halted"


def test_keep_open_without_met_by_refuses(halted):
    root, marker, *_ = halted
    out = _call(root)
    assert not out["ok"] and "met_by" in out["error"]
    assert "met_by" not in _record(marker)


def test_keep_open_before_the_halt_refuses_and_stamps_nothing(halted):
    root, marker, before, *_ = halted
    out = _call(root, met_by={"R1": before})
    assert not out["ok"] and "R1" in out["error"] and "before the halt" in out["error"]
    assert "met_by" not in _record(marker)


def test_keep_open_non_blocking_row_refuses(halted):
    root, marker, _, after1, _ = halted
    out = _call(root, met_by={"R9": after1})
    assert not out["ok"] and "R9" in out["error"] and "not a blocking row" in out["error"]
    assert "met_by" not in _record(marker)


def test_keep_open_on_a_landed_run_refuses(halted):
    root, marker, _, after1, after2 = halted
    record = _record(marker)
    marker.write_text(json.dumps({**record, "status": "landed", "sha": after2}), encoding="utf-8")
    out = _call(root, met_by={"R1": after1})
    assert not out["ok"] and "not halted" in out["error"]


def test_keep_open_on_a_running_run_refuses(halted):
    root, marker, _, after1, _ = halted
    record = _record(marker)
    marker.write_text(json.dumps({**record, "status": "running"}), encoding="utf-8")
    out = _call(root, met_by={"R1": after1})
    assert not out["ok"] and "not halted" in out["error"]


def test_keep_open_without_a_halt_record_refuses(tmp_path):
    _git(tmp_path, "init", "-q")
    sha = _commit(tmp_path, "x")
    out = _call(tmp_path, met_by={"R1": sha})
    assert not out["ok"] and "no halt record" in out["error"]


def test_keep_open_must_be_boolean(halted):
    root, _, _, after1, _ = halted
    out = op._handler({"run_id": SOLO, KEEP_OPEN: "yes", "met_by": {"R1": after1}}, root)
    assert not out["ok"] and KEEP_OPEN in out["error"]
