"""live_write_guard.merge_or_refuse: a run's scoped blob applied over a live file keeps another run's hunk or is refused."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import live_write_guard

pytestmark = pytest.mark.spawns_process

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


def _repo(tmp_path: Path, head: bytes = HEAD) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / PATH).write_bytes(head)
    _git(root, "add", PATH)
    _git(root, "commit", "-q", "-m", "base")
    return root


def _halted_peer(root: Path) -> str:
    """A halted run that withholds PATH, baselined at HEAD: it holds whatever the live file adds."""
    peer = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-aaaaaa"
    rdir = root / "state" / "warp" / peer
    rdir.mkdir(parents=True)
    base = _git(root, "rev-parse", f"HEAD:{PATH}")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: base}}),
        encoding="utf-8",
    )
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {PATH: "left in tree"}}}), encoding="utf-8"
    )
    return peer


def _edit(data: bytes, old: bytes, new: bytes) -> bytes:
    assert old in data
    return data.replace(old, new, 1)


def test_scoped_blob_keeps_a_halted_peers_hunk(tmp_path):
    root = _repo(tmp_path)
    _halted_peer(root)
    live = _edit(HEAD, b"line 2\n", b"PEER 2\n")
    (root / PATH).write_bytes(live)
    scoped = _edit(HEAD, b"line 8\n", b"MINE 8\n")
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, scoped)
    assert refusal is None
    assert out == _edit(scoped, b"line 2\n", b"PEER 2\n")
    assert (root / PATH).read_bytes() == live


def test_overlapping_hunk_is_refused_naming_the_holder_and_live_is_untouched(tmp_path):
    root = _repo(tmp_path)
    peer = _halted_peer(root)
    live = _edit(HEAD, b"line 2\n", b"PEER 2\n")
    (root / PATH).write_bytes(live)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(HEAD, b"line 2\n", b"MINE 2\n"))
    assert out is None
    assert peer in refusal and PATH in refusal
    assert (root / PATH).read_bytes() == live


def test_a_hunk_with_no_claim_is_foreign_and_kept(tmp_path):
    root = _repo(tmp_path)
    live = _edit(HEAD, b"line 5\n", b"???\n")
    (root / PATH).write_bytes(live)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(HEAD, b"line 9\n", b"MINE 9\n"))
    assert refusal is None
    assert out == _edit(_edit(HEAD, b"line 5\n", b"???\n"), b"line 9\n", b"MINE 9\n")


def test_an_unclaimed_overlap_is_refused_as_unattributed(tmp_path):
    root = _repo(tmp_path)
    live = _edit(HEAD, b"line 5\n", b"???\n")
    (root / PATH).write_bytes(live)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(HEAD, b"line 5\n", b"MINE 5\n"))
    assert out is None
    assert live_write_guard.UNATTRIBUTED in refusal
    assert (root / PATH).read_bytes() == live


def test_the_same_edit_on_both_sides_is_not_a_conflict(tmp_path):
    root = _repo(tmp_path)
    _halted_peer(root)
    both = _edit(HEAD, b"line 2\n", b"SAME 2\n")
    (root / PATH).write_bytes(both)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(both, b"line 8\n", b"MINE 8\n"))
    assert refusal is None
    assert out == _edit(both, b"line 8\n", b"MINE 8\n")


def test_crlf_line_endings_survive_the_merge(tmp_path):
    head = HEAD.replace(b"\n", b"\r\n")
    root = _repo(tmp_path, head)
    _halted_peer(root)
    (root / PATH).write_bytes(_edit(head, b"line 2\r\n", b"PEER 2\r\n"))
    scoped = _edit(head, b"line 8\r\n", b"MINE 8\r\n")
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, scoped)
    assert refusal is None
    assert out == _edit(scoped, b"line 2\r\n", b"PEER 2\r\n")
    assert out.count(b"\r\n") == 10 and out.count(b"\n") == 10


def test_base_moved_is_refused(tmp_path):
    root = _repo(tmp_path)
    old_sha = _git(root, "rev-parse", f"HEAD:{PATH}")
    (root / PATH).write_bytes(_edit(HEAD, b"line 1\n", b"NEW HEAD 1\n"))
    _git(root, "commit", "-q", "-am", "moved")
    live = (root / PATH).read_bytes()
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(HEAD, b"line 8\n", b"MINE\n"), base_blob_sha=old_sha)
    assert out is None and "base moved" in refusal
    assert (root / PATH).read_bytes() == live
    current = _git(root, "rev-parse", f"HEAD:{PATH}")
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, live + b"x\n", base_blob_sha=current)
    assert refusal is None and out == live + b"x\n"


def test_clean_live_file_takes_the_new_bytes(tmp_path):
    root = _repo(tmp_path)
    new = _edit(HEAD, b"line 3\n", b"MINE 3\n")
    assert live_write_guard.merge_or_refuse(root, ME, PATH, new) == (new, None)


def test_absent_live_file_takes_the_new_bytes(tmp_path):
    root = _repo(tmp_path)
    (root / PATH).unlink()
    assert live_write_guard.merge_or_refuse(root, ME, PATH, b"x\n") == (b"x\n", None)


def test_the_live_bytes_override_not_the_disk_is_the_ours_side(tmp_path):
    root = _repo(tmp_path)
    foreign = _edit(HEAD, b"line 2\n", b"FOREIGN 2\n")
    scoped = _edit(HEAD, b"line 8\n", b"MINE 8\n")
    (root / PATH).write_bytes(scoped)
    assert live_write_guard.merge_or_refuse(root, ME, PATH, scoped) == (scoped, None)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, scoped, live_bytes=foreign)
    assert refusal is None
    assert out == _edit(scoped, b"line 2\n", b"FOREIGN 2\n")
    assert (root / PATH).read_bytes() == scoped


def test_the_override_refuses_an_overlap_and_keeps_a_hunk_the_new_bytes_extend(tmp_path):
    root = _repo(tmp_path)
    pre = _edit(HEAD, b"line 5\n", b"ADDED 5\n")
    (root / PATH).write_bytes(_edit(HEAD, b"line 5\n", b"OTHER 5\n"))
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(HEAD, b"line 5\n", b"OTHER 5\n"), live_bytes=pre)
    assert out is None and live_write_guard.UNATTRIBUTED in refusal
    extended = _edit(HEAD, b"line 5\n", b"ADDED 5\nmore\n")
    assert live_write_guard.merge_or_refuse(root, ME, PATH, extended, live_bytes=pre) == (extended, None)


def test_a_failure_refuses_instead_of_raising(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / PATH).write_bytes(_edit(HEAD, b"line 2\n", b"PEER 2\n"))

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(live_write_guard, "_head_lines", boom)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, b"y\n")
    assert out is None and PATH in refusal


def test_merge_spawns_no_git(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _halted_peer(root)
    (root / PATH).write_bytes(_edit(HEAD, b"line 2\n", b"PEER 2\n"))

    def no_spawn(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, _edit(HEAD, b"line 8\n", b"MINE 8\n"))
    assert refusal is None and out is not None


def _own_run(root: Path) -> None:
    """ME's manifest, baselined at HEAD: whatever the pre-leg bytes add over HEAD is its own earlier work."""
    rdir = root / "state" / "warp" / ME
    rdir.mkdir(parents=True)
    base = _git(root, "rev-parse", f"HEAD:{PATH}")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [], "write_baseline": {PATH: base}}),
        encoding="utf-8",
    )


def test_the_override_lets_the_run_refine_its_own_earlier_hunk(tmp_path):
    root = _repo(tmp_path)
    _own_run(root)
    pre = _edit(HEAD, b"line 5\n", b"EARLIER 5\n")
    refined = _edit(HEAD, b"line 5\n", b"REFINED 5\n")
    assert live_write_guard.merge_or_refuse(root, ME, PATH, refined, live_bytes=pre) == (refined, None)


def test_the_override_still_refuses_the_same_overlap_for_a_run_that_did_not_write_it(tmp_path):
    root = _repo(tmp_path)
    pre = _edit(HEAD, b"line 5\n", b"EARLIER 5\n")
    refined = _edit(HEAD, b"line 5\n", b"REFINED 5\n")
    out, refusal = live_write_guard.merge_or_refuse(root, ME, PATH, refined, live_bytes=pre)
    assert out is None and live_write_guard.UNATTRIBUTED in refusal



def _leg_snapshot(root: Path, tmp_path: Path, pre: bytes, post: bytes, rel: str = PATH) -> Path:
    from coordinator_core.ops.dispatch_emit import ledger_recovery

    (root / rel).write_bytes(pre)
    out = tmp_path / "live.json"
    ledger_recovery.snap_before(root, [rel], out)
    (root / rel).write_bytes(post)
    ledger_recovery.snap_after(out)
    return out


def test_restore_after_leg_names_a_written_path_a_peer_run_holds(tmp_path):
    root = _repo(tmp_path)
    _halted_peer(root)
    pre = _edit(HEAD, b"line 2\n", b"FOREIGN 2\n")
    snap = _leg_snapshot(root, tmp_path, pre, _edit(pre, b"line 8\n", b"MINE 8\n"))
    out = live_write_guard.restore_after_leg(root, snap, ME, tmp_path / "rec")
    assert out["peer_held"] == [PATH] and out["refused"] == []


def test_restore_after_leg_names_no_peer_held_path_when_no_run_claims_it(tmp_path):
    root = _repo(tmp_path)
    pre = _edit(HEAD, b"line 2\n", b"FOREIGN 2\n")
    snap = _leg_snapshot(root, tmp_path, pre, _edit(pre, b"line 8\n", b"MINE 8\n"))
    assert live_write_guard.restore_after_leg(root, snap, ME, tmp_path / "rec")["peer_held"] == []
