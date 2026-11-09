"""A halted run records landed when some of its withheld paths hold only a concurrent run's hunks.

Halt side: a path the run's own checkpoint landed, whose remaining dirt its kept record does not
fingerprint, is not withheld. Record side: ``foreign`` voids owed paths, refused for a kept one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.ops.dispatch_emit import kept_work, land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_halt_withheld_only_run_changed_paths import _baseline
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import (  # noqa: F401
    RUN, _call, _chunks, _git, _record, _write, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/p.md"
PEER_PATHS = ["peer1.py", "peer2.py"]
HALT = {"halted": "refusal", "reason": "x", "plan": PLAN}


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(hrl, "_launch", lambda root, run_id: True)
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)


def _commit(root: Path, message: str, *paths: str) -> str:
    _git(root, "add", "--", *paths)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _halt_shape(root: Path) -> None:
    """Own work checkpointed on peer1/peer2; a peer then edits them; own2.py is the run's uncommitted product."""
    _write(root, PLAN, "plan\n")
    _commit(root, "plan", PLAN)
    _record(root)
    _chunks(root, {"C1": [*PEER_PATHS, "own2.py"]})
    _baseline(root, {"peer1.py": None, "peer2.py": None, "own2.py": None})
    _commit(root, f"checkpoint(wave 1): 1 row — C1\n\nCheckpoint-Plan: {PLAN}\n", *PEER_PATHS)
    for p in PEER_PATHS:
        _write(root, p, f"{p} edited by a concurrent run\n")


def test_halt_drops_a_path_only_a_peer_edited_after_the_run_checkpoint(repo):
    _halt_shape(repo)

    out = _call(repo, HALT)

    assert out["withheld"] == {"own2.py": land_run_halt.HALT_RECORD_ONLY}


def test_halt_keeps_a_path_its_own_kept_record_fingerprints(repo):
    _halt_shape(repo)
    record_dir = repo / RECORD_DIR_ROOT / RUN
    kept_work.write(record_dir, {"C1": {"paths": {"peer1.py": kept_work.fingerprint(repo, "peer1.py")}}}, None)

    out = _call(repo, HALT)

    assert set(out["withheld"]) == {"peer1.py", "own2.py"}


def test_halt_withholds_every_path_when_no_checkpoint_landed_it(repo):
    _halt_shape(repo)

    out = _call(repo, {"halted": "refusal", "reason": "x", "plan": "docs/plans/other.md"})

    assert set(out["withheld"]) == {*PEER_PATHS, "own2.py"}


def test_default_call_records_the_halted_run_landed_with_its_own_sha(repo):
    _halt_shape(repo)
    assert _call(repo, HALT)["committed"] is True
    landed = _commit(repo, "hand-land own2", "own2.py")

    out = hrl._handler({"run_id": RUN, "sha": landed}, repo)

    assert out["ok"] is True and out["written"] is True, out
    assert out["covers"] == {landed: ["own2.py"]}


def _marker(root: Path, base: str, withheld: list) -> str:
    rid = "warp-2026-11-09-x-20261109T000000-abcdef"
    d = root / "state" / "warp" / rid
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({
        "status": "halted", "halted": "x", "sizing_rel": None, "run_base_sha": base,
        "run_artifacts": {"withheld": {p: "halt-record-only" for p in withheld}},
    }), encoding="utf-8")
    return rid


def _ten_withheld(tmp_path: Path):
    """Ten withheld paths: five the landing sha changes, five that hold only a peer's dirt."""
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(tmp_path),
                              check=True, capture_output=True, text=True, **no_console_creationflags()).stdout.strip()

    own = [f"own{i}.py" for i in range(5)]
    peer = [f"peer{i}.py" for i in range(5)]
    git("init", "-q", "-b", "main")
    for p in own + peer:
        (tmp_path / p).write_text("v0\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    for p in own:
        (tmp_path / p).write_text("own work\n", encoding="utf-8")
    git("commit", "-q", "-am", "own hunks")
    landed = git("rev-parse", "HEAD")
    for p in peer:
        (tmp_path / p).write_text("peer hunk\n", encoding="utf-8")
    return _marker(tmp_path, base, own + peer), landed, own, peer


def test_owed_peer_paths_refuse_by_default(tmp_path):
    rid, landed, _own, peer = _ten_withheld(tmp_path)

    out = hrl._handler({"run_id": rid, "sha": landed}, tmp_path)

    assert out["ok"] is False and "did not change withheld peer0.py (+4 more)" in out["error"]


def test_foreign_param_voids_the_peer_paths_and_records_landed(tmp_path):
    rid, landed, own, peer = _ten_withheld(tmp_path)

    out = hrl._handler({"run_id": rid, "sha": landed, "foreign": peer}, tmp_path)

    assert out["ok"] is True and out["written"] is True, out
    assert sorted(out["voided"]) == peer
    assert out["covers"] == {landed: own}


def test_foreign_param_is_refused_for_a_path_the_kept_record_fingerprints(tmp_path):
    rid, landed, _own, peer = _ten_withheld(tmp_path)
    record_dir = tmp_path / "state" / "warp" / rid
    kept_work.write(record_dir, {"C1": {"paths": {"peer2.py": "0" * 40}}}, None)

    out = hrl._handler({"run_id": rid, "sha": landed, "foreign": peer}, tmp_path)

    assert out["ok"] is False and "peer2.py" in out["error"] and "kept record" in out["error"]


def test_foreign_param_does_not_excuse_a_path_it_does_not_name(tmp_path):
    rid, landed, _own, peer = _ten_withheld(tmp_path)

    out = hrl._handler({"run_id": rid, "sha": landed, "foreign": peer[:4]}, tmp_path)

    assert out["ok"] is False and "peer4.py" in out["error"]


def test_foreign_param_must_be_a_list_of_paths(tmp_path):
    rid, landed, _own, _peer = _ten_withheld(tmp_path)

    out = hrl._handler({"run_id": rid, "sha": landed, "foreign": "peer0.py"}, tmp_path)

    assert out["ok"] is False and "foreign" in out["error"]
