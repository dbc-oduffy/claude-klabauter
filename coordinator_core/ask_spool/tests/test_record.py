"""Spool record contract: id, 0600 exclusive write, claim-by-rename."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys

import pytest

from coordinator_core.ask_spool import record


def _spool(root, payload=b'{"a":1}', tuid="tu1", sid="s1"):
    return record.spool_ask(
        str(root), payload, session_id=sid, tool_use_id=tuid, reason="engine_unreachable"
    )


def test_spool_id_matches_sha1_hex16_of_concatenation():
    want = hashlib.sha1(b"s1tu1").hexdigest()[:16]
    assert record.spool_id_for("s1", "tu1", b"x") == want
    assert len(want) == 16


def test_no_tool_use_id_hashes_payload():
    a = record.spool_id_for("s1", None, b"one")
    b = record.spool_id_for("s1", None, b"two")
    assert a != b
    assert a == record.spool_id_for("s1", None, b"one")
    assert a == hashlib.sha1(b"s1one").hexdigest()[:16]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_entry_is_0600_and_roundtrips(tmp_path):
    sid, created = _spool(tmp_path, payload=b'{"x":"\xc3\xa9"}\nmore')
    assert created
    path = record.entry_path(str(tmp_path), sid)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    header, payload = record.read_entry(path)
    assert payload == b'{"x":"\xc3\xa9"}\nmore'
    assert header["spool_id"] == sid
    assert header["op"] == "hooks.submit_engine_ask"
    assert header["reason"] == "engine_unreachable"
    assert header["session_id"] == "s1"
    assert header["repo_root"] == str(tmp_path)
    assert "spooled_at" in header


def test_second_write_is_idempotent_and_keeps_first(tmp_path):
    sid, first = _spool(tmp_path, payload=b"A")
    sid2, second = _spool(tmp_path, payload=b"B")
    assert (first, second) == (True, False)
    assert sid == sid2
    _, payload = record.read_entry(record.entry_path(str(tmp_path), sid))
    assert payload == b"A"


def test_distinct_asks_without_tool_use_id_do_not_collide(tmp_path):
    a, _ = _spool(tmp_path, payload=b"one", tuid=None)
    b, _ = _spool(tmp_path, payload=b"two", tuid=None)
    assert a != b
    assert record.list_pending(str(tmp_path)) == sorted([a, b])


def test_claim_is_single_winner_then_minted(tmp_path):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    assert record.claim(root, sid) is not None
    assert record.claim(root, sid) is None
    assert record.list_pending(root) == []
    record.mark_minted(root, sid)
    assert os.path.exists(record.entry_path(root, sid, record.MINTED_SUFFIX))
    assert not os.path.exists(record.entry_path(root, sid, record.CLAIMED_SUFFIX))
    # a minted id re-spooled is a new .json; the hook dedupes on pending only
    assert record.claim(root, sid) is None


def test_release_returns_claim_to_pending(tmp_path):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    record.claim(root, sid)
    record.release(root, sid)
    assert record.list_pending(root) == [sid]


def test_spool_writes_via_tmp_then_replace(tmp_path, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((os.path.basename(src), os.path.basename(dst), os.path.exists(src)))
        return real(src, dst)

    monkeypatch.setattr(record.os, "replace", spy)
    sid, created = _spool(tmp_path)
    assert created
    assert len(seen) == 1
    src, dst, existed = seen[0]
    assert (dst, existed) == (sid + ".json", True)
    assert re.fullmatch(re.escape(sid) + r"\.\d+-[0-9a-f]{8}\.tmp", src)
    assert not [n for n in os.listdir(record.spool_dir(str(tmp_path))) if n.endswith(".tmp")]
    assert record.list_pending(str(tmp_path)) == [sid]


def test_claim_permission_error_is_not_claimed(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)

    def deny(src, dst):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(record.os, "rename", deny)
    assert record.claim(root, sid) is None
    assert record.list_pending(root) == [sid]


def test_release_file_exists_error_does_not_raise(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    record.claim(root, sid)
    real = os.rename

    def collide(src, dst):
        raise FileExistsError(dst)

    monkeypatch.setattr(record.os, "rename", collide)
    record.release(root, sid)
    monkeypatch.setattr(record.os, "rename", real)
    assert not os.path.exists(record.entry_path(root, sid, record.CLAIMED_SUFFIX))


def test_list_pending_missing_dir(tmp_path):
    assert record.list_pending(str(tmp_path)) == []


def test_read_entry_rejects_malformed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_bytes(b"no newline")
    with pytest.raises(ValueError):
        record.read_entry(str(p))
    p.write_bytes(b"notjson\npayload")
    with pytest.raises(ValueError):
        record.read_entry(str(p))


def test_orphaned_tmp_is_reclaimed_but_a_fresh_tmp_still_suppresses(tmp_path):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    final = record.entry_path(root, sid)
    os.unlink(final)
    tmp = record.entry_path(root, sid, ".4242-deadbeef" + record.TMP_SUFFIX)
    open(tmp, "wb").write(b"half")
    assert _spool(tmp_path) == (sid, False)
    assert not os.path.exists(final)
    assert os.path.exists(tmp)
    old = os.stat(tmp).st_mtime - record.TMP_STALE_S - 5
    os.utime(tmp, (old, old))
    assert _spool(tmp_path) == (sid, True)
    assert os.path.exists(final) and not os.path.exists(tmp)


def test_writers_use_distinct_tmp_names_and_reclaim_spares_a_live_peer(tmp_path, monkeypatch):
    root = str(tmp_path)
    names = []
    real = os.replace

    def spy(src, dst):
        names.append(os.path.basename(src))
        return real(src, dst)

    monkeypatch.setattr(record.os, "replace", spy)
    _spool(tmp_path, tuid="a")
    monkeypatch.setattr(record.os, "getpid", lambda: 7)
    _spool(tmp_path, tuid="b")
    _spool(tmp_path, tuid="b", sid="s2")
    assert len(set(names)) == 3
    # a live peer's fresh tmp survives another writer's reclaim; only the orphan goes
    sid = record.spool_id_for("s9", "z", b"")
    live = record.entry_path(root, sid, ".1-aaaaaaaa" + record.TMP_SUFFIX)
    orphan = record.entry_path(root, sid, ".2-bbbbbbbb" + record.TMP_SUFFIX)
    for p in (live, orphan):
        open(p, "wb").write(b"half")
    old = os.stat(orphan).st_mtime - record.TMP_STALE_S - 5
    os.utime(orphan, (old, old))
    assert _spool(tmp_path, tuid="z", sid="s9") == (sid, False)
    assert os.path.exists(live) and not os.path.exists(orphan)


def _windows_rename(monkeypatch):
    real = os.rename

    def strict(src, dst):
        if os.path.exists(dst):
            raise FileExistsError(dst)
        return real(src, dst)

    monkeypatch.setattr(record.os, "rename", strict)


def test_claim_file_exists_error_is_not_claimed(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)

    def collide(src, dst):
        raise FileExistsError(dst)

    monkeypatch.setattr(record.os, "rename", collide)
    assert record.claim(root, sid) is None
    assert record.list_pending(root) == [sid]


def _age(path, seconds):
    old = os.stat(path).st_mtime - seconds
    os.utime(path, (old, old))


def _holder(root, sid, pid):
    with open(record.entry_path(root, sid, record.HOLDER_SUFFIX), "w", encoding="utf-8") as fh:
        fh.write(str(pid))


def test_stale_claimed_with_dead_holder_is_reclaimed_but_a_fresh_one_is_not(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    claimed = record.entry_path(root, sid, record.CLAIMED_SUFFIX)
    open(claimed, "wb").write(b"dead claimer")
    _holder(root, sid, 99999)
    monkeypatch.setattr(record, "_pid_alive", lambda pid: False)
    _windows_rename(monkeypatch)
    assert record.claim(root, sid) is None
    _age(claimed, record.TMP_STALE_S + 5)
    assert record.claim(root, sid) is None  # past TMP_STALE_S is not past CLAIM_STALE_S
    assert record.list_pending(root) == [sid]
    _age(claimed, record.CLAIM_STALE_S)
    assert record.claim(root, sid) == claimed
    assert record.list_pending(root) == []
    assert not [n for n in os.listdir(record.spool_dir(root)) if "reclaim" in n]


def test_live_or_unknown_holder_keeps_an_old_claim(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    claimed = record.entry_path(root, sid, record.CLAIMED_SUFFIX)
    open(claimed, "wb").write(b"claim")
    _age(claimed, record.CLAIM_STALE_S * 10)
    _windows_rename(monkeypatch)
    assert record.claim(root, sid) is None  # no holder file: ambiguous
    _holder(root, sid, 12345)
    monkeypatch.setattr(record, "_pid_alive", lambda pid: True)
    assert record.claim(root, sid) is None
    open(record.entry_path(root, sid, record.HOLDER_SUFFIX), "w", encoding="utf-8").write("garbage")
    assert record.claim(root, sid) is None
    assert os.path.exists(claimed)


def test_pid_alive_is_true_for_self_and_for_a_non_positive_pid():
    assert record._pid_alive(os.getpid())
    assert record._pid_alive(0)


def test_respool_of_a_claimed_or_minted_id_is_a_noop(tmp_path):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    record.claim(root, sid)
    assert _spool(tmp_path) == (sid, False)
    assert record.list_pending(root) == []
    record.mark_minted(root, sid)
    assert _spool(tmp_path) == (sid, False)
    assert record.list_pending(root) == []


def test_long_mint_is_not_stolen_by_a_respool(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    claimed = record.claim(root, sid)
    assert claimed
    _age(claimed, record.TMP_STALE_S + 30)  # the mint outlives 60 s
    assert _spool(tmp_path) == (sid, False)
    assert record.list_pending(root) == []
    _windows_rename(monkeypatch)
    assert record.claim(root, sid) is None
    record.mark_minted(root, sid)
    assert os.path.exists(record.entry_path(root, sid, record.MINTED_SUFFIX))
    assert not os.path.exists(record.entry_path(root, sid, record.HOLDER_SUFFIX))


def test_only_file_exists_from_os_open_means_already_spooled(tmp_path, monkeypatch):
    real = os.open

    def deny(path, flags, mode=0o777):
        if str(path).endswith(record.TMP_SUFFIX):
            raise PermissionError(path)
        return real(path, flags, mode)

    monkeypatch.setattr(record.os, "open", deny)
    with pytest.raises(PermissionError):
        _spool(tmp_path)

    def exists(path, flags, mode=0o777):
        raise FileExistsError(path)

    monkeypatch.setattr(record.os, "open", exists)
    assert _spool(tmp_path)[1] is False


def test_respool_recovers_an_ask_stranded_by_a_dead_claimer(tmp_path, monkeypatch):
    root = str(tmp_path)
    sid, _ = _spool(tmp_path)
    claimed = record.claim(root, sid)
    _age(claimed, record.CLAIM_STALE_S + 5)
    assert _spool(tmp_path) == (sid, False)  # holder is this live process
    monkeypatch.setattr(record, "_pid_alive", lambda pid: False)
    assert _spool(tmp_path) == (sid, True)
    assert record.list_pending(root) == [sid]


def test_pid_alive_reads_a_missing_process_as_dead(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(record.sys, "platform", "linux")
    monkeypatch.setattr(record.os, "kill", gone)
    assert record._pid_alive(424242) is False


def _fake_ctypes(monkeypatch, *, handle, last_error=0, exit_code=259):
    import types

    class _Code:
        def __init__(self):
            self.value = 0

    k32 = types.SimpleNamespace(
        OpenProcess=lambda *a: handle,
        GetExitCodeProcess=lambda h, ref: setattr(ref, "value", exit_code) or True,
        CloseHandle=lambda h: True,
    )
    fake = types.SimpleNamespace(
        windll=types.SimpleNamespace(kernel32=k32),
        GetLastError=lambda: last_error,
        c_ulong=_Code,
        byref=lambda x: x,
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(record.sys, "platform", "win32")


def test_pid_alive_windows_branch_dead_only_when_provably_gone(monkeypatch):
    _fake_ctypes(monkeypatch, handle=0, last_error=87)
    assert record._pid_alive(999999) is False
    _fake_ctypes(monkeypatch, handle=0, last_error=5)  # access denied: doubt
    assert record._pid_alive(999999) is True
    _fake_ctypes(monkeypatch, handle=7, exit_code=1)  # exited
    assert record._pid_alive(999999) is False
    _fake_ctypes(monkeypatch, handle=7, exit_code=259)  # STILL_ACTIVE
    assert record._pid_alive(999999) is True


def test_holder_file_is_0600(tmp_path):
    record.spool_ask(str(tmp_path), b"x", session_id="s", tool_use_id="t", reason="r")
    sid = record.list_pending(str(tmp_path))[0]
    record.claim(str(tmp_path), sid)
    mode = stat.S_IMODE(os.stat(record.entry_path(str(tmp_path), sid, record.HOLDER_SUFFIX)).st_mode)
    if sys.platform != "win32":
        assert mode == 0o600
