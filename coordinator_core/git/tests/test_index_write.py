
import os
import subprocess
import time

import pytest

from coordinator_core.git import index_write
from coordinator_core.git.git_objects import write_object

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/idx")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _status(repo):
    return _git(repo, "status", "--porcelain").stdout


def test_spliced_new_file_reads_staged_to_real_git(tmp_path):
    repo = _repo(tmp_path)
    (repo / "added.txt").write_text("new content\n", encoding="utf-8", newline="\n")

    sha = write_object(repo / ".git", b"blob", b"new content\n")
    index_write.splice_index(repo, {"added.txt": (0o100644, sha)})

    status = _status(repo)
    assert status.strip() == "A  added.txt", status
    _git(repo, "fsck", "--strict")


def test_untouched_entries_survive_verbatim(tmp_path):
    repo = _repo(tmp_path)
    (repo / "added.txt").write_text("x\n", encoding="utf-8", newline="\n")

    sha = write_object(repo / ".git", b"blob", b"x\n")
    index_write.splice_index(repo, {"added.txt": (0o100644, sha)})

    status = _status(repo)
    assert "seed.txt" not in status, status


def test_commit_then_splice_leaves_a_clean_tree(tmp_path):
    repo = _repo(tmp_path)
    (repo / "landed.txt").write_text("landed\n", encoding="utf-8", newline="\n")
    blob = write_object(repo / ".git", b"blob", b"landed\n")

    index_write.splice_index(repo, {"landed.txt": (0o100644, blob)})
    _git(repo, "commit", "-q", "-m", "landed")

    assert _status(repo).strip() == "", _status(repo)
    show = _git(repo, "show", "HEAD:landed.txt").stdout
    assert show == "landed\n", show


def test_absent_sentinel_stages_a_deletion(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.txt").unlink()
    index_write.splice_index(repo, {"seed.txt": index_write.ABSENT})
    assert _status(repo).strip() == "D  seed.txt", _status(repo)


def test_lock_is_refused_never_stolen(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "index.lock").write_bytes(b"")
    with pytest.raises(index_write.IndexWriteLockBusy):
        index_write.splice_index(repo, {"seed.txt": index_write.ABSENT})


def test_orphaned_stale_lock_is_reaped_before_the_splice(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
    monkeypatch.chdir(repo)
    (repo / "seed.txt").unlink()
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"")
    stale = time.time() - 300
    os.utime(lock, (stale, stale))

    index_write.splice_index(repo, {"seed.txt": index_write.ABSENT})

    assert not lock.exists()
    assert _status(repo).strip() == "D  seed.txt", _status(repo)


def test_absolute_key_refused_before_writing(tmp_path):
    repo = _repo(tmp_path)
    before = (repo / ".git" / "index").read_bytes()
    abs_key = str(repo / "seed.txt")

    with pytest.raises(index_write.IndexWriteError):
        index_write.splice_index(repo, {abs_key: index_write.ABSENT})

    after = (repo / ".git" / "index").read_bytes()
    assert after == before


def test_drive_letter_key_refused_before_writing(tmp_path):
    repo = _repo(tmp_path)
    before = (repo / ".git" / "index").read_bytes()
    drive_key = "C:/claude-klabauter/seed.txt"

    with pytest.raises(index_write.IndexWriteError):
        index_write.splice_index(repo, {drive_key: index_write.ABSENT})

    after = (repo / ".git" / "index").read_bytes()
    assert after == before


def test_scale_the_splice_does_not_rewrite_untouched_entries(tmp_path):
    repo = _repo(tmp_path)
    for i in range(300):
        (repo / f"f{i:04d}.txt").write_text(f"{i}\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bulk")

    before = (repo / ".git" / "index").read_bytes()
    (repo / "one_more.txt").write_text("one\n", encoding="utf-8", newline="\n")
    sha = write_object(repo / ".git", b"blob", b"one\n")
    index_write.splice_index(repo, {"one_more.txt": (0o100644, sha)})

    assert _status(repo).strip() == "A  one_more.txt", _status(repo)
    after = (repo / ".git" / "index").read_bytes()
    assert len(after) > len(before) - 64
    _git(repo, "fsck", "--strict")


def test_the_index_read_happens_under_the_lock(tmp_path):
    repo = _repo(tmp_path)
    lock_path = repo / ".git" / "index.lock"
    (repo / "ours.txt").write_text("ours\n", encoding="utf-8", newline="\n")
    (repo / "peer.txt").write_text("peer\n", encoding="utf-8", newline="\n")

    real_read_bytes = type(repo).read_bytes
    observed = {}

    def watch(self):
        if self.name == "index" and "held" not in observed:
            observed["held"] = lock_path.exists()
            observed["peer_add_rc"] = _git(
                repo, "add", "--", "peer.txt", check=False
            ).returncode
        return real_read_bytes(self)

    blob = write_object(repo / ".git", b"blob", b"ours\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(repo), "read_bytes", watch)
        index_write.splice_index(repo, {"ours.txt": (0o100644, blob)})

    assert observed.get("held") is True, (
        "`.git/index` was read with no lock held -- the read-modify-write is "
        "unserialised and a peer commit in that window is lost"
    )
    assert observed["peer_add_rc"] != 0, (
        "real git staged a path while our splice was mid-read; the window "
        "this test exists to close is open"
    )
    assert _status(repo).strip().splitlines()[0] == "A  ours.txt", _status(repo)
    _git(repo, "fsck", "--strict")


def _reference_splice_bytes(raw, replacements):
    """Oracle: parse every entry, splice replacements, sort by name, rehash."""
    import hashlib
    import struct

    count = struct.unpack(">I", raw[8:12])[0]
    offset = 12
    kept = []
    for _ in range(count):
        start = offset
        nul = raw.find(b"\x00", offset + 62)
        name = raw[offset + 62 : nul]
        offset = start + ((62 + len(name) + 8) & ~7)
        kept.append((name, raw[start:offset]))
    out, seen = [], set()
    for name, blob in kept:
        if name in replacements:
            seen.add(name)
            if replacements[name] is not None:
                out.append((name, replacements[name]))
            continue
        out.append((name, blob))
    for name, blob in replacements.items():
        if name not in seen and blob is not None:
            out.append((name, blob))
    out.sort(key=lambda p: p[0])
    body = struct.pack(">4sII", b"DIRC", 2, len(out)) + b"".join(b for _, b in out)
    return body + hashlib.sha1(body).digest()


def test_splice_is_byte_identical_to_the_reference_algorithm(tmp_path):
    repo = _repo(tmp_path)
    for d in range(20):
        (repo / f"d{d:02d}").mkdir()
        for i in range(100):
            (repo / f"d{d:02d}" / f"f{i:03d}.txt").write_text(
                f"{d}-{i}\n", encoding="utf-8", newline="\n"
            )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bulk")
    (repo / "000-first.txt").write_text("a\n", encoding="utf-8", newline="\n")
    (repo / "zzz-last.txt").write_text("z\n", encoding="utf-8", newline="\n")
    (repo / "d05" / "f050.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    (repo / "d05" / "f050b.txt").write_text("ins\n", encoding="utf-8", newline="\n")
    (repo / "d10" / "f010.txt").unlink()

    def sha(text):
        return write_object(repo / ".git", b"blob", text)

    updates = {
        "000-first.txt": (0o100644, sha(b"a\n")),
        "zzz-last.txt": (0o100644, sha(b"z\n")),
        "d05/f050.txt": (0o100644, sha(b"changed\n")),
        "d05/f050b.txt": (0o100644, sha(b"ins\n")),
        "d10/f010.txt": index_write.ABSENT,
        "d19/f099.txt": index_write.ABSENT,
        "missing/never.txt": index_write.ABSENT,
    }
    index_path = repo / ".git" / "index"
    before = index_path.read_bytes()

    replacements = {}
    for path, value in updates.items():
        key = path.encode()
        if value is index_write.ABSENT:
            replacements[key] = None
        else:
            replacements[key] = index_write._build_entry(
                key, value[0], value[1], (repo / path).stat()
            )
    expected = _reference_splice_bytes(before, replacements)

    index_write.splice_index(repo, updates)
    assert index_path.read_bytes() == expected
    _git(repo, "fsck", "--strict")
    status = _status(repo)
    assert "d10/f010.txt" in status and "000-first.txt" in status, status


def test_missing_key_absent_keeps_the_entry_block_verbatim(tmp_path):
    repo = _repo(tmp_path)
    index_path = repo / ".git" / "index"
    before = index_path.read_bytes()
    index_write.splice_index(repo, {"nope.txt": index_write.ABSENT})
    after = index_path.read_bytes()
    assert after[:12] == before[:12]
    count = int.from_bytes(before[8:12], "big")
    _, body_end = index_write._walk_entries(before, count)
    assert after[12:-20] == before[12:body_end]
    assert _status(repo).strip() == ""
