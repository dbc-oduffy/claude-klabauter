"""live_write_guard restore (the default post-leg entry): a leg's tool write that drops a live peer run's hunk
gets the hunk back, or the collision is recorded naming that run; never a silent drop."""

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
    """A halted run that declares PATH and withholds nothing, baselined at HEAD as the leg's own run is: only
    its recorded baseline delta over the pre-leg bytes says which hunk is its."""
    peer = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-aaaaaa"
    rdir = _manifest(root, peer)
    (rdir / "halted.json").write_text(json.dumps({"status": "halted", "run_artifacts": {"withheld": {}}}), encoding="utf-8")
    halted_withheld_index.record_declared(root, peer, [PATH])
    return peer


def _edit(data: bytes, old: bytes, new: bytes) -> bytes:
    assert old in data
    return data.replace(old, new, 1)


def _leg(root: Path, tmp_path: Path, pre: bytes, post: bytes) -> Path:
    (root / PATH).write_bytes(pre)
    snap = tmp_path / "live.json"
    ledger_recovery.snap_before(root, [PATH], snap)
    (root / PATH).write_bytes(post)
    ledger_recovery.snap_after(snap)
    return snap


def _restore(root: Path, snap: Path, record_dir: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    code = live_write_guard.main([
        "restore", "--root", str(root), "--snapshot", str(snap), "--run-id", ME, "--record-dir", str(record_dir),
    ])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["ok"] is True
    return out


def test_a_leg_write_that_leaves_the_file_at_the_peer_runs_baseline_gets_the_hunk_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The leg resets the file to HEAD, the peer's baseline: the tree no longer shows the peer dirty, so the
    claim scan on the file on disk names no holder and only the peer's declaration still says whose hunk it was."""
    root = _repo(tmp_path)
    _manifest(root, ME)
    _live_peer(root)
    pre = _edit(HEAD, b"line 2\n", b"PEER 2\n")
    snap = _leg(root, tmp_path, pre, HEAD)
    out = _restore(root, snap, tmp_path / "rec", capsys)
    assert out["refused"] == [] and out["restored"] == [PATH]
    assert (root / PATH).read_bytes() == pre
    assert not (tmp_path / "rec" / "tree-write-refusals.json").exists()


def test_a_leg_write_without_the_peer_runs_hunk_keeps_the_legs_own_hunk_and_restores_the_peers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    _manifest(root, ME)
    _live_peer(root)
    both = _edit(_edit(HEAD, b"line 2\n", b"PEER 2\n"), b"line 8\n", b"MINE 8\n")
    snap = _leg(root, tmp_path, both, _edit(HEAD, b"line 8\n", b"MINE 8\n"))
    out = _restore(root, snap, tmp_path / "rec", capsys)
    assert out["refused"] == []
    assert (root / PATH).read_bytes() == both


def test_a_collision_with_the_peer_runs_hunk_is_recorded_naming_that_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    _manifest(root, ME)
    peer = _live_peer(root)
    pre = _edit(HEAD, b"line 2\n", b"PEER 2\n")
    post = _edit(HEAD, b"line 2\n", b"MINE 2\n")
    snap = _leg(root, tmp_path, pre, post)
    out = _restore(root, snap, tmp_path / "rec", capsys)
    assert out["restored"] == [] and len(out["refused"]) == 1
    assert (root / PATH).read_bytes() == post
    entries = json.loads((tmp_path / "rec" / "tree-write-refusals.json").read_text(encoding="utf-8"))
    assert [e["path"] for e in entries] == [PATH]
    assert peer in entries[0]["reason"] and entries[0]["run_id"] == ME


def test_no_declaring_peer_means_the_leg_keeps_its_own_dropped_hunk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    _manifest(root, ME)
    pre = _edit(_edit(HEAD, b"line 2\n", b"EARLIER 2\n"), b"line 8\n", b"MINE 8\n")
    post = _edit(HEAD, b"line 8\n", b"MINE 8\n")
    snap = _leg(root, tmp_path, pre, post)
    out = _restore(root, snap, tmp_path / "rec", capsys)
    assert out["restored"] == [] and out["refused"] == []
    assert (root / PATH).read_bytes() == post
