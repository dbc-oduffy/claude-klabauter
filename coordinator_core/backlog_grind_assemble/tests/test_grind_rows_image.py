"""grind-row image: pre/post blobs recorded in-process."""
import json

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.git import git_objects
from coordinator_core.ops.dispatch_emit import grind_commit_contract as gcc


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.txt").write_bytes(b"one\n")
    return tmp_path


def _run(repo, phase, *paths, run="R1", row="C1"):
    argv = ["image", "--run-id", run, "--row-id", row, "--phase", phase, "--repo-root", str(repo)]
    for p in paths:
        argv += ["--path", p]
    return grind_rows.main(argv)


def _record(repo, run="R1", row="C1"):
    return json.loads((repo / gcc.row_images_rel(run, row) / gcc.IMAGE_RECORD_FILE).read_text(encoding="utf-8"))


def test_pre_post_round_trip_and_blob_readable(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo, "pre", "a.txt", "new.txt") == 0
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / "new.txt").write_bytes(b"n\n")
    assert _run(repo, "post", "a.txt", "new.txt") == 0
    rec = _record(repo)
    assert rec["pre"]["a.txt"] == grind_rows._git_blob_sha(b"one\n")
    assert rec["pre"]["new.txt"] is None
    assert rec["post"]["a.txt"] == grind_rows._git_blob_sha(b"two\n")
    assert git_objects.read_object(repo / ".git", rec["pre"]["a.txt"]) == ("blob", b"one\n")


def test_repeat_is_idempotent_and_urlenc_shape(tmp_path):
    repo = _repo(tmp_path)
    tok = gcc.encode_declared_paths(["a.txt"])
    argv = ["image", "--run-id", "R1", "--row-id", "C1", "--phase", "pre", "--repo-root", str(repo), "--paths-urlenc", tok]
    assert grind_rows.main(argv) == 0
    first = _record(repo)
    assert grind_rows.main(argv) == 0
    assert _record(repo) == first


def test_unsafe_row_id_refused_before_any_write(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo, "pre", "a.txt", row="../x") == 1
    assert not (repo / gcc.ROW_IMAGES_ROOT).exists()


def test_bad_phase_and_escaping_path_refused(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo, "mid", "a.txt") == 2
    assert _run(repo, "pre", "../out.txt") == 1
    assert not (repo / gcc.ROW_IMAGES_ROOT).exists()


def test_pre_rerun_after_edit_keeps_first_image(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo, "pre", "a.txt") == 0
    first = _record(repo)["pre"]["a.txt"]
    (repo / "a.txt").write_bytes(b"edited\n")
    assert _run(repo, "pre", "a.txt") == 0
    assert _record(repo)["pre"]["a.txt"] == first


def test_corrupt_record_refused_and_left_byte_identical(tmp_path):
    repo = _repo(tmp_path)
    rec = repo / gcc.row_images_rel("R1", "C1") / gcc.IMAGE_RECORD_FILE
    rec.parent.mkdir(parents=True)
    rec.write_bytes(b"{not json")
    assert _run(repo, "pre", "a.txt") == 1
    assert rec.read_bytes() == b"{not json"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_writer_reader_round_trip_excludes_peer_hunk(tmp_path):
    import subprocess

    from coordinator_core.ops.dispatch_emit import grind_landing_select

    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    f = tmp_path / "s.txt"
    f.write_bytes(b"l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")
    git("add", "s.txt")
    git("commit", "-q", "-m", "base")
    f.write_bytes(b"PEER\nl1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")
    assert _run(tmp_path, "pre", "s.txt") == 0
    f.write_bytes(b"PEER\nl1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nROW\n")
    assert _run(tmp_path, "post", "s.txt") == 0
    sel = grind_landing_select.select(tmp_path, "R1", {"s.txt": ["C1"]})
    assert sel.dropped == {}
    assert sel.landed_text("s.txt") == "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nROW\n"
