"""dispatch.record_halted_run_landed: sha-verified landed close of a halted run, spawn-free."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PARENT = "ask-parent-20261015T000000-abcdef"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


def _seed(root: Path, statuses: dict):
    runs = [_run("A"), _run("B", ["A"], c.STATUS_AFTER)]
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, tuple(runs)).to_json()), encoding="utf-8")
    for rid, entry in statuses.items():
        st = root / c.status_rel(rid)
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"parent_id": PARENT, **entry}), encoding="utf-8")


def _status(root, rid):
    return json.loads((root / c.status_rel(rid)).read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    first = _commit(tmp_path, "one")
    second = _commit(tmp_path, "two")
    return tmp_path, first, second


def _call(root, **params):
    return op._handler(params, root)


def test_halted_run_lands_and_releases_after_successor(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["written"] is True and out["sha"] == first
    assert out["siblings"]["released"] == [fire_command("state/sizings/B.yaml")]
    assert _status(root, "A")["status"] == "landed" and _status(root, "A")["sha"] == first


def test_second_call_same_sha_is_idempotent(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    _call(root, run_id="A", sha=first)
    before = (root / c.status_rel("A")).read_bytes()
    out = _call(root, run_id="A", sha=first)
    assert out == {"ok": True, "run_id": "A", "sha": first, "written": False}
    assert (root / c.status_rel("A")).read_bytes() == before


@pytest.mark.parametrize("status", ["running", "after", "queued", "ready", "held"])
def test_non_halted_status_refuses(repo, status):
    root, first, _ = repo
    _seed(root, {"A": {"status": status}})
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "not halted" in out["error"]
    assert _status(root, "A")["status"] == status


def test_landed_at_other_sha_refuses(repo):
    root, first, second = repo
    _seed(root, {"A": {"status": "landed", "sha": second}})
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "already landed" in out["error"]


def test_absent_status_refuses(repo):
    root, first, _ = repo
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "never ran" in out["error"]


SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"


def _solo(root: Path, status: str = "halted", **extra) -> Path:
    d = root / "state" / "warp" / SOLO
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    marker.write_text(json.dumps({"status": status, "halted": "x", "sizing_rel": None, **extra}), encoding="utf-8")
    return marker


def test_solo_halted_run_records_landed_marker(repo):
    root, first, _ = repo
    marker = _solo(root)
    out = _call(root, run_id=SOLO, sha=first[:10])
    assert out == {
        "ok": True, "run_id": SOLO, "sha": first, "written": True,
        "recorded": f"state/warp/{SOLO}/halted.json",
    }
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["status"] == "landed" and data["sha"] == first and data["halted"] == "x"
    assert not (root / c.status_rel(SOLO)).exists()


def test_solo_second_call_is_idempotent(repo):
    root, first, _ = repo
    marker = _solo(root)
    _call(root, run_id=SOLO, sha=first)
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha=first)
    assert out == {"ok": True, "run_id": SOLO, "sha": first, "written": False}
    assert marker.read_bytes() == before


def test_solo_unreachable_sha_refuses_and_leaves_marker(repo):
    root, _, _ = repo
    marker = _solo(root)
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha="b" * 40)
    assert not out["ok"] and "not a commit" in out["error"]
    assert marker.read_bytes() == before


def test_solo_landed_at_other_sha_refuses(repo):
    root, first, second = repo
    _solo(root, "landed", sha=second)
    out = _call(root, run_id=SOLO, sha=first)
    assert not out["ok"] and "already landed" in out["error"]


@pytest.mark.parametrize("status", ["resumed", "running"])
def test_solo_non_halted_status_refuses(repo, status):
    root, first, _ = repo
    marker = _solo(root, status)
    before = marker.read_bytes()
    out = _call(root, run_id=SOLO, sha=first)
    assert not out["ok"] and "not halted" in out["error"]
    assert marker.read_bytes() == before


def test_sha_not_a_commit_refuses(repo):
    root, _, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    out = _call(root, run_id="A", sha="b" * 40)
    assert not out["ok"] and "not a commit" in out["error"]
    assert _status(root, "A")["status"] == "halted"


def test_sha_off_first_parent_chain_refuses(repo):
    root, first, _ = repo
    _git(root, "checkout", "-q", "-b", "side", first)
    side = _commit(root, "side")
    _git(root, "checkout", "-q", "-")
    _seed(root, {"A": {"status": "halted"}})
    out = _call(root, run_id="A", sha=side)
    assert not out["ok"] and "first-parent" in out["error"]


def test_sha_beyond_window_refuses(repo, monkeypatch):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    monkeypatch.setattr(op, "ANCESTRY_WINDOW", 1)
    out = _call(root, run_id="A", sha=first)
    assert not out["ok"] and "first-parent" in out["error"]


@pytest.mark.parametrize("params", [
    {"run_id": "../x", "sha": "a" * 40},
    {"run_id": "A", "sha": "abc"},
    {"run_id": "A", "sha": "xyz1234"},
    {"run_id": "A", "sha": "A" * 40},
    {"run_id": "", "sha": "a" * 40},
])
def test_malformed_params_refuse(repo, params):
    assert not _call(repo[0], **params)["ok"]


def test_abbreviated_sha_records_full_sha(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})
    out = _call(root, run_id="A", sha=first[:10])
    assert out["ok"] and out["written"] is True and out["sha"] == first
    assert _status(root, "A")["sha"] == first


def test_abbreviated_sha_idempotent_after_landing(repo):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}})
    _call(root, run_id="A", sha=first[:10])
    out = _call(root, run_id="A", sha=first[:10])
    assert out == {"ok": True, "run_id": "A", "sha": first, "written": False}


def test_ambiguous_abbreviation_refuses_by_name(repo, monkeypatch):
    from coordinator_core.git import rollback_check

    root = repo[0]
    _seed(root, {"A": {"status": "halted"}})
    fake =["abcd" + "1" * 36, "abcd" + "2" * 36]
    monkeypatch.setattr(rollback_check, "_first_parent_ancestors", lambda *a, **k: [(0, s) for s in fake])
    out = _call(root, run_id="A", sha="abcd")
    assert not out["ok"] and "abcd" in out["error"] and "ambiguous" in out["error"]
    assert _status(root, "A")["status"] == "halted"


def test_unknown_abbreviation_refuses_by_name(repo):
    root, first, second = repo
    prefix = "0000" if not (first + second).count("0000") else "ffff"
    _seed(root, {"A": {"status": "halted"}})
    out = _call(root, run_id="A", sha=prefix)
    assert not out["ok"] and prefix in out["error"] and "no commit" in out["error"]
    assert _status(root, "A")["status"] == "halted"


def test_op_spawns_no_process(repo, monkeypatch):
    root, first, _ = repo
    _seed(root, {"A": {"status": "halted"}, "B": {"status": "after"}})

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    out = _call(root, run_id="A", sha=first)
    assert out["ok"] and out["written"] is True


def test_op_spawns_no_process_solo(repo, monkeypatch):
    root, first, _ = repo
    _solo(root)

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    out = _call(root, run_id=SOLO, sha=first)
    assert out["ok"] and out["written"] is True
