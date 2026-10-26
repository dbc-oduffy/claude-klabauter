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
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {}
    assert _blob_text(root, s.supplied_blobs[REL]) == ROW_TXT
    assert s.landed_text(REL) == ROW_TXT.decode()


def test_pre_equals_head_post_is_worktree_needs_no_blob(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, HEAD_TXT, ROW_TXT)
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.supplied_blobs == {} and s.dropped == {}


def test_incident_shape_peer_import_in_pre_excluded(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, PEER_TXT, BOTH_TXT)
    (root / REL).write_bytes(BOTH_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
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
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {REL: {ROW: contract.DROP_PEER_HUNKS_CONFLICT}}
    assert s.dropped_for(ROW, REL) == contract.DROP_PEER_HUNKS_CONFLICT
    assert REL not in s.supplied_blobs


def test_unparseable_record_drops_fail_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw="{not json")
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {REL: {ROW: contract.DROP_UNATTRIBUTED_SHARED}}


def test_missing_blob_drops_fail_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw=json.dumps({"pre": {REL: "0" * 40}, "post": {REL: "1" * 40}}))
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {REL: {ROW: contract.DROP_UNATTRIBUTED_SHARED}}


def test_no_images_lands_worktree_when_unheld(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {} and s.supplied_blobs == {}
    assert s.landed_text(REL) == ROW_TXT.decode()


def test_no_images_peer_held_drops(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch, held={REL})
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {REL: {ROW: contract.DROP_UNATTRIBUTED_SHARED}}


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
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {}
    assert _blob_text(root, s.supplied_blobs[REL]) == ROW_TXT


def test_pre_without_post_drops_unattributed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw=json.dumps({"pre": {REL: _blob(root, HEAD_TXT)}, "post": {}}))
    (root / REL).write_bytes(BOTH_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {REL: {ROW: contract.DROP_UNATTRIBUTED_SHARED}}


BASE20 = "".join(f"line{i} = {i}\n" for i in range(1, 21))
EDIT_A = BASE20.replace("line2 = 2\n", "line2 = 'a'\n")
EDIT_B = BASE20.replace("line19 = 19\n", "line19 = 'b'\n")
EDIT_AB = EDIT_A.replace("line19 = 19\n", "line19 = 'b'\n")


def _two_row_repo(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    (root / REL).write_bytes(BASE20.encode())
    _git(root, "add", REL)
    _git(root, "commit", "-q", "-m", "base20")
    return root


def _row_images(root: Path, row: str, pre: bytes, post: bytes) -> None:
    f = root / contract.row_images_rel(RUN, row) / contract.IMAGE_RECORD_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"pre": {REL: _blob(root, pre)}, "post": {REL: _blob(root, post)}}), encoding="utf-8")


def test_two_owners_sequential_disjoint_hunks_both_land(tmp_path, monkeypatch):
    root = _two_row_repo(tmp_path, monkeypatch)
    _row_images(root, ROW, BASE20.encode(), EDIT_A.encode())
    _row_images(root, "r2", EDIT_A.encode(), EDIT_AB.encode())
    (root / REL).write_bytes(EDIT_AB.encode())
    s = sel.select(root, RUN, {REL: [ROW, "r2"]})
    assert s.dropped == {}
    assert s.landed_text(REL) == EDIT_AB


def test_two_owners_concurrent_disjoint_hunks_both_land(tmp_path, monkeypatch):
    root = _two_row_repo(tmp_path, monkeypatch)
    _row_images(root, ROW, BASE20.encode(), EDIT_A.encode())
    _row_images(root, "r2", BASE20.encode(), EDIT_B.encode())
    (root / REL).write_bytes(EDIT_AB.encode())
    s = sel.select(root, RUN, {REL: [ROW, "r2"]})
    assert s.dropped == {}
    assert s.landed_text(REL) == EDIT_AB


def test_two_owners_colliding_hunks_drop_only_the_second(tmp_path, monkeypatch):
    root = _two_row_repo(tmp_path, monkeypatch)
    clash = BASE20.replace("line2 = 2\n", "line2 = 'z'\n")
    _row_images(root, ROW, BASE20.encode(), EDIT_A.encode())
    _row_images(root, "r2", BASE20.encode(), clash.encode())
    (root / REL).write_bytes(clash.encode())
    s = sel.select(root, RUN, {REL: [ROW, "r2"]})
    assert s.dropped == {REL: {"r2": contract.DROP_PEER_HUNKS_CONFLICT}}
    assert s.dropped_for(ROW, REL) is None
    assert _blob_text(root, s.supplied_blobs[REL]) == EDIT_A.encode()


def test_crlf_images_over_lf_head_land_lf_with_the_edit(tmp_path, monkeypatch):
    root = _two_row_repo(tmp_path, monkeypatch)
    _git(root, "config", "core.autocrlf", "true")
    crlf = BASE20.replace("\n", "\r\n")
    edited = EDIT_A.replace("\n", "\r\n")
    _row_images(root, ROW, crlf.encode(), edited.encode())
    (root / REL).write_bytes(edited.encode())
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {}
    assert s.supplied_blobs == {}
    assert s.landed_text(REL) == EDIT_A


def test_untracked_new_file_with_prior_pre_lands_pre_to_post(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    f = root / "new.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    rec = root / contract.row_images_rel(RUN, ROW) / contract.IMAGE_RECORD_FILE
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({
        "pre": {"new.py": _blob(root, b"a = 1\n")}, "post": {"new.py": _blob(root, b"a = 1\nb = 2\n")},
    }), encoding="utf-8")
    s = sel.select(root, RUN, {"new.py": [ROW]})
    assert s.dropped == {}
    assert s.landed_text("new.py") == "a = 1\nb = 2\n"


def test_second_owner_without_images_drops_first_lands(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, HEAD_TXT, ROW_TXT)
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: [ROW, "r2"]})
    assert s.dropped == {REL: {"r2": contract.DROP_UNATTRIBUTED_SHARED}}
    assert s.dropped_for(ROW, REL) is None
    assert s.landed_text(REL) == ROW_TXT.decode()


def test_single_owner_deletion_with_file_gone_lands_the_deletion(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, HEAD_TXT, None)
    (root / REL).unlink()
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {}
    assert s.landed_text(REL) is None


def test_two_owner_deletion_drops_both_unattributed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, HEAD_TXT, None)
    _row_images_deleted = root / contract.row_images_rel(RUN, "r2") / contract.IMAGE_RECORD_FILE
    _row_images_deleted.parent.mkdir(parents=True, exist_ok=True)
    _row_images_deleted.write_text(
        json.dumps({"pre": {REL: _blob(root, HEAD_TXT)}, "post": {REL: None}}), encoding="utf-8"
    )
    (root / REL).unlink()
    s = sel.select(root, RUN, {REL: [ROW, "r2"]})
    assert s.dropped == {REL: {ROW: contract.DROP_UNATTRIBUTED_SHARED, "r2": contract.DROP_UNATTRIBUTED_SHARED}}
    assert s.landed_text(REL) == HEAD_TXT.decode()


PS1 = "s.ps1"
PS1_BASE = "".join(f"l{i} = {i}\n" for i in range(1, 6))
PS1_EDIT = PS1_BASE.replace("l2 = 2\n", "l2 = 'x'\n")


def _ps1_repo(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    (root / ".gitattributes").write_bytes(b"*.ps1 text eol=crlf\n")
    (root / PS1).write_bytes(PS1_BASE.encode())
    _git(root, "add", ".gitattributes", PS1)
    _git(root, "commit", "-q", "-m", "ps1")
    return root


def _ps1_images(root: Path, pre: bytes, post: bytes) -> None:
    f = root / contract.row_images_rel(RUN, ROW) / contract.IMAGE_RECORD_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"pre": {PS1: _blob(root, pre)}, "post": {PS1: _blob(root, post)}}), encoding="utf-8")


def test_refused_checkin_worktree_lands_the_lf_image_as_a_supplied_blob(tmp_path, monkeypatch):
    root = _ps1_repo(tmp_path, monkeypatch)
    _ps1_images(root, PS1_BASE.encode(), PS1_EDIT.encode())
    (root / PS1).write_bytes(PS1_EDIT.replace("\n", "\r\n").encode())
    s = sel.select(root, RUN, {PS1: [ROW]})
    assert s.dropped == {}
    assert _blob_text(root, s.supplied_blobs[PS1]) == PS1_EDIT.encode()
    assert s.landed_text(PS1) == PS1_EDIT


def test_refused_checkin_cr_image_drops_unattributed(tmp_path, monkeypatch):
    root = _ps1_repo(tmp_path, monkeypatch)
    crlf = PS1_EDIT.replace("\n", "\r\n").encode()
    _ps1_images(root, PS1_BASE.encode(), crlf)
    (root / PS1).write_bytes(crlf)
    s = sel.select(root, RUN, {PS1: [ROW]})
    assert s.dropped == {PS1: {ROW: contract.DROP_UNATTRIBUTED_SHARED}}
    assert PS1 not in s.supplied_blobs


def test_non_string_image_value_drops_fail_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _no_peers(monkeypatch)
    _images(root, None, None, raw=json.dumps({"pre": {REL: 7}, "post": {REL: None}}))
    (root / REL).write_bytes(ROW_TXT)
    s = sel.select(root, RUN, {REL: [ROW]})
    assert s.dropped == {REL: {ROW: contract.DROP_UNATTRIBUTED_SHARED}}
