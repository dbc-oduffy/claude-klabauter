"""Tests for `coordinator_core.warm.election` -- Windows named-pipe
first-instance election.

Purpose: C14 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md.
Covers `pipe_name`'s shape and its sensitivity to each of its three
load-bearing components (SID, clone hash, engine token), `current_user_sid`'s
SDDL-string shape, and `elect`'s actual win/lose semantics against a live
pipe name -- the kernel's `FILE_FLAG_FIRST_PIPE_INSTANCE` atomicity is the
thing under test, not a mock of it.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C14
"""

from __future__ import annotations

import re
import sys
import uuid

import pytest

from coordinator_core.warm import election

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="election.elect is Windows-only")


def _unique_pipe_name() -> str:
    return election.pipe_name(uuid.uuid4().hex, user_sid="S-1-5-21-1-2-3-1001")


def test_pipe_name_shape():
    name = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    assert name.startswith(r"\\.\pipe\coordinator-core.")
    parts = name[len(r"\\.\pipe\coordinator-core."):].split(".")
    assert parts[0] == "S-1-5-21-1-2-3-1001"
    assert re.fullmatch(r"[0-9a-f]{16}", parts[1])
    assert parts[2] == "tok1"


def test_pipe_name_changes_with_engine_token():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok2", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    assert a != b


def test_pipe_name_changes_with_clone_path():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok1", engine_clone="..", user_sid="S-1-5-21-1-2-3-1001")
    assert a != b


def test_pipe_name_changes_with_sid():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1002")
    assert a != b


def test_pipe_name_deterministic_for_same_inputs():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    assert a == b


def test_pipe_name_defaults_engine_clone_to_repo_root_and_resolves_own_sid():
    name = election.pipe_name("tok1")
    assert name.startswith(r"\\.\pipe\coordinator-core.")
    parts = name[len(r"\\.\pipe\coordinator-core."):].split(".")
    assert parts[0] == election.current_user_sid()


def test_current_user_sid_shape():
    sid = election.current_user_sid()
    assert re.fullmatch(r"S-1-5-21-\d+-\d+-\d+-\d+", sid)


def test_elect_wins_first_instance():
    name = _unique_pipe_name()
    handle = election.elect(name)
    try:
        assert isinstance(handle, int)
    finally:
        import _winapi

        _winapi.CloseHandle(handle)


def test_elect_loses_to_a_live_first_instance():
    import _winapi

    name = _unique_pipe_name()
    winner = election.elect(name)
    try:
        with pytest.raises(election.ElectionLost) as excinfo:
            election.elect(name)
        assert excinfo.value.pipe_name == name
    finally:
        _winapi.CloseHandle(winner)


def test_elect_reelectable_after_winner_closes_handle():
    import _winapi

    name = _unique_pipe_name()
    first = election.elect(name)
    _winapi.CloseHandle(first)

    second = election.elect(name)
    try:
        assert isinstance(second, int)
    finally:
        _winapi.CloseHandle(second)


def test_elect_and_current_user_sid_are_windows_gated():
    real_is_windows = election._is_windows
    election._is_windows = lambda: False
    try:
        with pytest.raises(RuntimeError):
            election.current_user_sid()
        with pytest.raises(RuntimeError):
            election.elect(r"\\.\pipe\does-not-matter")
    finally:
        election._is_windows = real_is_windows


def test_pipe_sddl_grants_only_the_server_logon_sid_read_write():
    sddl = election._pipe_sddl("S-1-5-21-1-2-3-1001", "S-1-5-5-0-342518")
    assert sddl == (
        "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;S-1-5-21-1-2-3-1001)"
        "(A;;FRFW;;;S-1-5-5-0-342518)"
    )
    assert not re.search(r";(WD|AU|BU|IU|RC|WR);|S-1-1-0", sddl)


def test_pipe_sddl_without_a_logon_sid_adds_no_extra_ace():
    assert election._pipe_sddl("S-1-5-21-1-2-3-1001", None) == (
        "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;S-1-5-21-1-2-3-1001)"
    )


def test_current_logon_sid_reads_the_process_token_and_feeds_the_pipe_dacl(monkeypatch):
    logon = election.current_logon_sid()
    assert logon is not None and re.fullmatch(r"S-1-5-5-\d+-\d+", logon)
    seen = []
    real = election._pipe_sddl
    monkeypatch.setattr(
        election, "_pipe_sddl", lambda sid, logon_sid=None: seen.append(logon_sid) or real(sid, logon_sid)
    )
    election._build_security_attributes(election.current_user_sid())
    assert seen == [logon]


def _open_pipe_as_restricted_token(name: str, restricting_sids: list) -> bool:
    """Open pipe `name` read/write from this thread under a token restricted to
    `restricting_sids` (the codex workspace-write shape). True when the open
    succeeds, False on access denied."""
    import ctypes
    from ctypes import wintypes

    import _winapi

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.SetThreadToken.argtypes = [ctypes.c_void_p, wintypes.HANDLE]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        # Trap: never name this field `the Game Dev Reviewer` -- the publish persona substitute rewrites
        # that token to prose and the klabauter copy stops parsing (71b3d8).
        _fields_ = [("psid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    base = wintypes.HANDLE()
    assert advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x2 | 0x8 | 0x1, ctypes.byref(base))
    entries = (_SID_AND_ATTRIBUTES * len(restricting_sids))()
    for i, text in enumerate(restricting_sids):
        psid = ctypes.c_void_p()
        assert advapi32.ConvertStringSidToSidW(text, ctypes.byref(psid))
        entries[i].psid = psid
    restricted = wintypes.HANDLE()
    assert advapi32.CreateRestrictedToken(
        base, 0, 0, None, 0, None, len(entries), entries, ctypes.byref(restricted)
    )
    impersonation = wintypes.HANDLE()
    assert advapi32.DuplicateTokenEx(
        restricted, 0x02000000, None, 2, 2, ctypes.byref(impersonation)
    )
    assert advapi32.SetThreadToken(None, impersonation)
    try:
        handle = _winapi.CreateFile(
            name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, 0, _winapi.OPEN_EXISTING, 0, 0
        )
    except PermissionError:
        return False
    else:
        _winapi.CloseHandle(handle)
        return True
    finally:
        advapi32.SetThreadToken(None, None)
        for h in (impersonation, restricted, base):
            kernel32.CloseHandle(h)


def test_restricted_token_with_logon_sid_reaches_the_elected_pipe(monkeypatch):
    import _winapi

    logon = election.current_logon_sid()
    assert logon is not None
    name = _unique_pipe_name()
    handle = election.elect(name)
    try:
        assert _open_pipe_as_restricted_token(name, [logon, "S-1-1-0"]) is True
    finally:
        _winapi.CloseHandle(handle)


def test_restricted_token_is_refused_when_the_pipe_has_no_logon_sid_ace(monkeypatch):
    import _winapi

    logon = election.current_logon_sid()
    assert logon is not None
    monkeypatch.setattr(election, "current_logon_sid", lambda: None)
    name = _unique_pipe_name()
    handle = election.elect(name)
    try:
        assert _open_pipe_as_restricted_token(name, [logon, "S-1-1-0"]) is False
    finally:
        _winapi.CloseHandle(handle)


def test_pipe_client_path_reads_no_credential_file():
    """The restricted-token client reaches the engine over the pipe alone: the
    cookie and door-key files (whose directory ACL names no logon SID) are
    server-side and hook-env concerns, never read by the dispatch client, so
    the codex child keeps its hooks (door key arrives by env) without them."""
    import ast
    from pathlib import Path

    from coordinator_core.warm import client, sandboxed_spawn

    for module in (client, sandboxed_spawn):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not {n.rsplit(".", 1)[-1] for n in imported} & {"cookie", "door_credential"}, module.__name__


def test_election_lost_is_an_election_error():
    assert issubclass(election.ElectionLost, election.ElectionError)
