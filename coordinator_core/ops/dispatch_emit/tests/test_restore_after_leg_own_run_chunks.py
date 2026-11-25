"""live_write_guard restore: a hunk an earlier leg of the SAME run wrote is the run's own, so a later chunk's write
over it is not refused; a peer run's hunk and a hunk no earlier leg introduced still refuse."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, ledger_recovery, live_write_guard

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/mod.py"
HEAD = b"".join(b"line %d\n" % n for n in range(1, 11))
ME = "ask-own"


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / PATH).write_bytes(HEAD)
    _git(root, "add", PATH)
    _git(root, "commit", "-q", "-m", "base")
    return root


def _manifest(root: Path, run_id: str) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    base = _git(root, "rev-parse", f"HEAD:{PATH}")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: base}}),
        encoding="utf-8",
    )
    return rdir


def _live_peer(root: Path) -> str:
    peer = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-aaaaaa"
    rdir = _manifest(root, peer)
    (rdir / "halted.json").write_text(json.dumps({"status": "halted", "run_artifacts": {"withheld": {}}}), encoding="utf-8")
    halted_withheld_index.record_declared(root, peer, [PATH])
    return peer


def _edit(data: bytes, old: bytes, new: bytes) -> bytes:
    assert old in data
    return data.replace(old, new, 1)


def _leg(root: Path, rec: Path, name: str, pre: bytes, post: bytes) -> Path:
    """One writing leg of the run: its live-snap file sits in the run's record dir, as the fire's legs write it."""
    (root / PATH).write_bytes(pre)
    snap = rec / f"live-{name}.json"
    ledger_recovery.snap_before(root, [PATH], snap)
    (root / PATH).write_bytes(post)
    ledger_recovery.snap_after(snap)
    return snap


def _restore(root: Path, snap: Path, rec: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    code = live_write_guard.main([
        "restore", "--root", str(root), "--snapshot", str(snap), "--run-id", ME, "--record-dir", str(rec),
    ])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["ok"] is True
    return out


def test_a_later_chunk_overwriting_an_earlier_chunks_hunk_of_the_same_run_is_not_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    rec = tmp_path / "rec"
    c0 = _edit(HEAD, b"line 2\n", b"C0 2\n")
    _leg(root, rec, "C0", HEAD, c0)
    c1 = _edit(c0, b"C0 2\n", b"C1 2\n")
    snap = _leg(root, rec, "C1", c0, c1)
    out = _restore(root, snap, rec, capsys)
    assert out["refused"] == []
    assert (root / PATH).read_bytes() == c1
    assert not (rec / "tree-write-refusals.json").exists()


def test_a_hunk_no_earlier_leg_of_the_run_introduced_still_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The earlier leg wrote the path, but the hunk at line 2 was already in its before blob: path membership alone
    does not make it the run's."""
    root = _repo(tmp_path)
    rec = tmp_path / "rec"
    stray = _edit(HEAD, b"line 2\n", b"STRAY 2\n")
    c0 = _edit(stray, b"line 8\n", b"C0 8\n")
    _leg(root, rec, "C0", stray, c0)
    snap = _leg(root, rec, "C1", c0, _edit(c0, b"STRAY 2\n", b"C1 2\n"))
    out = _restore(root, snap, rec, capsys)
    assert len(out["refused"]) == 1
    entries = json.loads((rec / "tree-write-refusals.json").read_text(encoding="utf-8"))
    assert [e["path"] for e in entries] == [PATH]


def test_a_peer_runs_hunk_still_refuses_beside_the_runs_own_earlier_leg(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    _manifest(root, ME)
    peer = _live_peer(root)
    rec = tmp_path / "rec"
    c0 = _edit(HEAD, b"line 8\n", b"C0 8\n")
    _leg(root, rec, "C0", HEAD, c0)
    both = _edit(c0, b"line 2\n", b"PEER 2\n")
    snap = _leg(root, rec, "C1", both, _edit(c0, b"line 2\n", b"MINE 2\n"))
    out = _restore(root, snap, rec, capsys)
    assert len(out["refused"]) == 1 and peer in out["refused"][0]["reason"]
