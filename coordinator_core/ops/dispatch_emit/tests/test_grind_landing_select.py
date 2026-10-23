"""grind_landing_select.select: per-path landed bytes from row images, in tmp_path git repos."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object, write_object
from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract
from coordinator_core.ops.dispatch_emit import grind_landing_select as sel

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN, ROW ="run1", "r1"
HEAD_TXT = b'"""doc"""\nbody = 1\n'
ROW_TXT = b'"""doc edited"""\nbody = 1\n'
PEER_TXT = b"import peer_pkg\n" + HEAD_TXT
BOTH_TXT = b"import peer_pkg\n" + ROW_TXT
REL = "m.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / REL).write_bytes(HEAD_TXT)
    _git(tmp_path, "add", REL)
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _blob(root: Path, data: bytes) -> str:
    return write_object(resolve_git_common_dir(root), b"blob", data)


def _images(root: Path, pre, post, *, raw: str | None = None) -> None:
    f = root / contract.row_images_rel(RUN, ROW) / contract.IMAGE_RECORD_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        f.write_text(raw, encoding="utf-8")
        return
    f.write_text(json.dumps({
        "pre": {REL: _blob(root, pre) if pre is not None else None},
        "post": {REL: _blob(root, post) if post is not None else None},
    }), encoding="utf-8")


def _no_peers(monkeypatch, held=()):
    from coordinator_core.ops.ceremony import commit_v2

    def fake(root, paths, peer_holds=None, **_kw):
        for p in paths:
            if p in held and peer_holds is not None:
                peer_holds.append((p, "peer"))
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)


def _blob_text(root: Path, sha: str) -> bytes:
    got = read_object(resolve_git_common_dir(root), sha)
    assert got is not None
    return got[1]


def test_pre_equals_head_lands_post_without_peer_edit(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, HEAD_TXT, ROW_TXT)
    (root / REL).write_bytes(BOTH_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {}
    assert _blob_text(root, s.supplied_blobs[REL]) == ROW_TXT
    assert s.landed_text(REL) == ROW_TXT.decode()


def test_pre_equals_head_post_is_worktree_needs_no_blob(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, HEAD_TXT, ROW_TXT)
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.supplied_blobs == {} and s.dropped == {}


def test_incident_shape_peer_import_in_pre_excluded(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, PEER_TXT, BOTH_TXT)
    (root / REL).write_bytes(BOTH_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {}
    landed = _blob_text(root, s.supplied_blobs[REL])
    assert landed == ROW_TXT
    assert b"import peer_pkg" not in landed and b"doc edited" in landed


def test_overlapping_peer_edit_drops_with_conflict(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    pre = b'"""doc peer"""\nbody = 1\n'
    _images(root, pre, ROW_TXT)
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {REL: contract.DROP_PEER_HUNKS_CONFLICT}
    assert REL not in s.supplied_blobs


def test_unparseable_record_drops_fail_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw="{not json")
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {REL: contract.DROP_UNATTRIBUTED_SHARED}


def test_missing_blob_drops_fail_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw=json.dumps({"pre": {REL: "0" * 40}, "post": {REL: "1" * 40}}))
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {REL: contract.DROP_UNATTRIBUTED_SHARED}


def test_no_images_lands_worktree_when_unheld(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {} and s.supplied_blobs == {}
    assert s.landed_text(REL) == ROW_TXT.decode()


def test_no_images_peer_held_drops(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch, held={REL})
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {REL: contract.DROP_UNATTRIBUTED_SHARED}


def test_reads_the_record_grind_row_image_writes(tmp_path, monkeypatch):
    from coordinator_core.backlog_grind_assemble import grind_rows

    root = _repo(tmp_path)
    _no_peers(monkeypatch)

    def image(phase):
        argv = ["image", "--run-id", RUN, "--row-id", ROW, "--phase", phase, "--repo-root", str(root), "--path", REL]
        assert grind_rows.main(argv) == 0

    (root / REL).write_bytes(PEER_TXT)
    image("pre")
    (root / REL).write_bytes(BOTH_TXT)
    image("post")
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {}
    assert _blob_text(root, s.supplied_blobs[REL]) == ROW_TXT


def test_pre_without_post_drops_unattributed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw=json.dumps({"pre": {REL: _blob(root, HEAD_TXT)}, "post": {}}))
    (root / REL).write_bytes(BOTH_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {REL: contract.DROP_UNATTRIBUTED_SHARED}


def test_non_string_image_value_drops_fail_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw=json.dumps({"pre": {REL: 7}, "post": {REL: None}}))
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: ROW})
    assert s.dropped == {REL: contract.DROP_UNATTRIBUTED_SHARED}
