"""A same-user restricted token (the codex workspace-write sandbox shape) must
open the warm pipe `election.elect` creates, and must be denied by a DACL
without the server's logon-SID ACE.

Each token restricts to Everyone, Users, the RESTRICTED (S-1-5-12) or
WRITE_RESTRICTED-shaped SID under test, and the logon SID. `election._pipe_sddl`
scopes the pipe to the server's logon session and carries no RC/WR ACE, so the
logon-SID ACE is what satisfies the restricting-SID access check.

The restricted token is made with CreateRestrictedToken from the test
process's own token and impersonated on one worker thread that opens the pipe.
A child process under such a token cannot start here (the interpreter lives in
the user profile, whose DACL a restricting set without the user SID cannot read),
and including the user SID would satisfy the very ACE under test.
"""

from __future__ import annotations

import sys
import threading
import uuid

import pytest

from coordinator_core.warm import election

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes and tokens")

_DISABLE_MAX_PRIVILEGE = 0x1
_WRITE_RESTRICTED = 0x8
_TOKEN_ALL_ACCESS = 0xF01FF
_GENERIC_READ_WRITE = 0xC0000000
_OPEN_EXISTING = 3

# Everyone and Users keep the token realistic; the logon SID is appended per case.
_BASE_RESTRICTING = ["S-1-1-0", "S-1-5-32-545"]


def _with_logon_sid(extra: list[str]) -> list[str]:
    logon_sid = election.current_logon_sid()
    if not logon_sid:
        pytest.skip("test process token carries no logon SID")
    return _BASE_RESTRICTING + extra + [logon_sid]


def _open_pipe_as_restricted_token(pipe: str, restricting_sids: list[str], flags: int) -> int:
    """Return 0 if the pipe opened under the restricted token, else the Win32 error."""
    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]  # noqa: identity-names  (Win32 SID_AND_ATTRIBUTES field)

    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    adv.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]

    proc_token = wintypes.HANDLE()
    assert adv.OpenProcessToken(k32.GetCurrentProcess(), _TOKEN_ALL_ACCESS, ctypes.byref(proc_token))
    restricting = (SID_AND_ATTRIBUTES * len(restricting_sids))()
    for i, text in enumerate(restricting_sids):
        sid_ptr = ctypes.c_void_p()
        assert adv.ConvertStringSidToSidW(text, ctypes.byref(sid_ptr))
        restricting[i] = SID_AND_ATTRIBUTES(sid_ptr, 0)
    restricted = wintypes.HANDLE()
    assert adv.CreateRestrictedToken(
        proc_token, _DISABLE_MAX_PRIVILEGE | flags, 0, None, 0, None,
        len(restricting_sids), restricting, ctypes.byref(restricted),
    ), ctypes.get_last_error()

    result: list[int] = []

    def _worker() -> None:
        if not adv.ImpersonateLoggedOnUser(restricted):
            result.append(ctypes.get_last_error() or -1)
            return
        try:
            h = k32.CreateFileW(pipe, _GENERIC_READ_WRITE, 0, None, _OPEN_EXISTING, 0, None)
            if h in (None, ctypes.c_void_p(-1).value):
                result.append(ctypes.get_last_error())
            else:
                k32.CloseHandle(h)
                result.append(0)
        finally:
            adv.RevertToSelf()

    try:
        t = threading.Thread(target=_worker)
        t.start()
        t.join(5)
        assert result, "impersonation worker did not finish"
        return result[0]
    finally:
        k32.CloseHandle(restricted)
        k32.CloseHandle(proc_token)


@pytest.mark.parametrize(
    "extra_sids,flags",
    [
        (["S-1-5-12"], 0),
        (["S-1-5-33"], _WRITE_RESTRICTED),
    ],
    ids=["restricted-code", "write-restricted"],
)
def test_restricted_token_opens_the_election_pipe(extra_sids, flags):
    import _winapi

    name = election.pipe_name(uuid.uuid4().hex)
    handle = election.elect(name)
    try:
        assert _open_pipe_as_restricted_token(name, _with_logon_sid(extra_sids), flags) == 0
    finally:
        _winapi.CloseHandle(handle)


def test_restricted_token_is_denied_by_the_base_dacl(monkeypatch):
    import _winapi

    restricting = _with_logon_sid(["S-1-5-12"])
    monkeypatch.setattr(
        election,
        "_pipe_sddl",
        lambda sid, logon_sid=None, extra_sids=(): f"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{sid})",
    )
    name = election.pipe_name(uuid.uuid4().hex)
    handle = election.elect(name)
    try:
        assert _open_pipe_as_restricted_token(name, restricting, 0) == 5
    finally:
        _winapi.CloseHandle(handle)


# Stand-in for an engine owned by another account: the pipe is elected with a
# foreign owner SID and no logon ACE, so the test process's own user SID is a
# non-owner caller admitted only through extra_sids (the sandbox-principal ACE).
_FOREIGN_OWNER = "S-1-5-21-1-2-3-1001"


def _elect_foreign_owned_pipe(monkeypatch, extra_sids):
    name = election.pipe_name(uuid.uuid4().hex, user_sid=_FOREIGN_OWNER)
    monkeypatch.setattr(election, "current_logon_sid", lambda: None)
    monkeypatch.setattr(election, "sandbox_principal_sids", lambda: tuple(extra_sids))
    return name, election.elect(name, user_sid=_FOREIGN_OWNER)


def test_restricted_token_whose_user_sid_is_in_extra_sids_opens_the_pipe(monkeypatch):
    import _winapi

    own = election.current_user_sid()
    name, handle = _elect_foreign_owned_pipe(monkeypatch, [own])
    try:
        assert _open_pipe_as_restricted_token(name, _BASE_RESTRICTING + [own], 0) == 0
    finally:
        _winapi.CloseHandle(handle)


def test_restricted_token_whose_user_sid_is_not_in_extra_sids_is_refused(monkeypatch):
    import _winapi

    own = election.current_user_sid()
    name, handle = _elect_foreign_owned_pipe(monkeypatch, ["S-1-5-21-1-2-3-1006"])
    try:
        assert _open_pipe_as_restricted_token(name, _BASE_RESTRICTING + [own], 0) == 5
    finally:
        _winapi.CloseHandle(handle)


def test_restricted_token_without_sandbox_user_in_restricting_set_is_refused_even_when_user_admitted(monkeypatch):
    """An admitted user SID is not enough: the restricting-SID pass needs an
    ACE too. The owner's logon ACE cannot serve a sandbox user (a different
    logon session), so a sandbox token restricted to Everyone/Users/its logon
    SID only is refused; one whose restricting set carries a SID in the DACL
    (its user or an admitted group) passes."""
    import _winapi

    own = election.current_user_sid()
    name, handle = _elect_foreign_owned_pipe(monkeypatch, [own])
    try:
        assert _open_pipe_as_restricted_token(name, _BASE_RESTRICTING, 0) == 5
    finally:
        _winapi.CloseHandle(handle)
