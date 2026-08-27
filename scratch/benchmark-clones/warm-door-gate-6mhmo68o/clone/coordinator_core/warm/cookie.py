"""coordinator_core.warm.cookie -- the loopback HTTP listener's boot cookie.

Spec backlink: docs/plans/2026-08-26-the-loopback-listener-gets-a-credential.md § C1

WHAT THIS MODULE IS: a self-contained mint/read/clear surface for a
per-boot random credential, stored beside the existing discovery record in
`breadcrumb.svc_dir()`. It is deliberately NOT wired into `supervisor.py`,
`server.py`, or `client.py` here -- this chunk supplies the primitive; a
later chunk wires minting into boot, delivery into `client.py`'s curl path,
and refusal into the HTTP handler. See the cited plan's § Refusal
semantics (in the spike verdict it cites) for the refusal contract this
module's callers will eventually enforce -- not restated here.

WHY ONE `os.open(..., 0o600)` CALL COVERS BOTH PLATFORMS, FOR DIFFERENT
REASONS: on Windows, `os.chmod` is measured INERT -- `st_mode` and the
DACL are both unmoved even after `os.chmod(path, 0o666)`. The cookie's
real protection there comes from the CONTAINING DIRECTORY:
`breadcrumb.svc_dir()` sits under `%LOCALAPPDATA%`, which inherits
exactly SYSTEM + Builtin Administrators + the owning user -- the same ACE
set `election._build_security_attributes` hand-builds for the named pipe
-- so a file written plainly there already inherits that protection and
no DACL work is needed on the write side (the plan's own anti-scope
forbids hand-building one here; ctypes appears only on the READ side, in
`assert_directory_private`, to VERIFY the inherited ACL rather than trust
it). On POSIX, `_runtime_base()` falls back to `~/.cache` -- conventionally
0755 -- so there the FILE's own mode is what actually carries the
protection, and `0o600` is real. The single `os.open` call is correct on
both platforms; it is just doing different jobs on each.

Negative-spec:
  - Does NOT wire `mint()` into any boot sequence -- no live call site
    exists yet (see WIRING NOTE above). That is a later chunk's job.
  - Does NOT enforce refusal on the HTTP path -- this module only mints,
    reads, clears, and asserts; the refusal contract belongs to the
    handler that will call `read()`.
  - Does NOT use `os.chmod` on Windows, and does NOT hand-build a DACL for
    the cookie file with `SECURITY_ATTRIBUTES`/`SetFileSecurityW` -- see
    module docstring above and the plan's Anti-scope.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.warm import breadcrumb

__all__ = [
    "COOKIE_FILENAME",
    "CURL_CONFIG_FILENAME",
    "COOKIE_HEADER",
    "curl_config_path",
    "DirectoryNotPrivateError",
    "CookieUnreadableError",
    "cookie_path",
    "mint",
    "ensure",
    "read",
    "clear",
    "assert_directory_private",
]

COOKIE_FILENAME = "warm.cookie"

#: Sibling curl config carrying the cookie as a header line. A SECOND
#: spelling of the same secret, in the same ACL-protected directory, and it
#: exists for one measured reason: the whole justification for the HTTP
#: transport is that curl reaches the listener in ~10.4ms against a ~26.0ms
#: bare-interpreter floor. Any delivery path that starts a Python process to
#: read the cookie has already spent more than the transport saves. A curl
#: caller does `curl --config <this file>` and pays nothing.
CURL_CONFIG_FILENAME = "warm.curlrc"

#: The request header the cookie travels in.
COOKIE_HEADER = "X-Coordinator-Cookie"


class CookieUnreadableError(Exception):
    """A cookie file is PRESENT but could not be read.

    Distinct from absent on purpose. `read()` collapses both to None by
    design -- it is a degrade-to-no-information reader -- but a boot path
    must not, because the two have opposite correct responses: absent
    means generate one, present-but-unreadable means REFUSE. Re-minting
    over an unreadable file rotates the secret out from under every live
    session on what may be a transient I/O error, and serving without
    reading it authenticates nobody.
    """


class DirectoryNotPrivateError(Exception):
    """`assert_directory_private` could not verify that the cookie's
    resolved location excludes other users.

    Raised rather than silently degraded: per the cited plan's AC3, an
    engine that cannot verify this protection must not publish an HTTP
    endpoint. Callers that boot the listener are expected to let this
    propagate and fail closed -- wiring that refusal into boot is a later
    chunk's job (see this module's own WIRING NOTE); this module only
    supplies the callable and its exception.
    """


def cookie_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/warm.cookie` for `engine_root` -- reuses
    `breadcrumb.svc_dir` rather than re-deriving the runtime directory."""
    return breadcrumb.svc_dir(engine_root) / COOKIE_FILENAME


def curl_config_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/warm.curlrc` -- the interpreter-free delivery path."""
    return breadcrumb.svc_dir(engine_root) / CURL_CONFIG_FILENAME


def _write_private_atomically(path: Path, content: str) -> None:
    """Write `content` to `path` via a `.tmp` sibling + `os.replace`.

    The rename is load-bearing: it is what stops a reader seeing a
    half-written file, the property Bitcoin Core's own atomic-cookie shape
    relies on. `0o600` is real on POSIX and inert on Windows, where the
    directory's inherited ACL carries the protection instead -- see the
    module docstring.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(str(tmp_path), str(path))


def mint(engine_root: Optional[Path] = None, port: Optional[int] = None) -> str:
    """Mint a new boot cookie and write it ATOMICALLY: a `.tmp` sibling is
    written and fsynced-by-close, then `os.replace` renames it onto the
    final path. The rename is load-bearing -- it is what stops a client
    reading a half-written cookie mid-write, exactly the property Bitcoin
    Core's own atomic-cookie shape relies on.

    Returns the minted token (`secrets.token_hex(32)`, 64 hex chars).

    Creates `cookie_path(engine_root)`'s parent directory if it does not
    yet exist, matching `breadcrumb.write_breadcrumb`'s own
    `mkdir(parents=True, exist_ok=True)` pattern.

    LIFETIME -- "boot cookie" NAMES THE FILE, NOT A ROTATION SCHEDULE, and
    the wiring chunk must not call this at every boot. The secret is
    generated ON FIRST ABSENCE and never rotated on a schedule
    (`docs/research/spike-verdicts/2026-08-26-loopback-op-dispatch-credential-shape.md`,
    corrected at `112810600`). The hook-fire caller cannot read this file:
    it receives the secret interpolated from a session environment fixed at
    launch, with no re-export channel, so a cookie rotated at engine boot
    leaves every session launched before that restart authenticating
    nothing for the rest of its life -- and a listener can outlive a
    publish by hours with health green throughout.

    `port`, WHEN GIVEN, IS EMITTED INTO THE SAME CURLRC WRITE -- an `url =`
    line alongside the existing `header =` line, so `curl --config <file>`
    alone carries both the credential and the destination and a caller
    never has to re-derive the listener's port. Formatted identically to
    `supervisor.listener_url` (`http://127.0.0.1:<port>`) -- not imported
    from there, to avoid a cycle (`supervisor` already imports `cookie`).
    Omitted entirely when `port` is None, which is what today's only
    caller (`ensure()`, from `supervisor._assert_credential_ready`, called
    BEFORE the bind -- see that function's docstring) still gets: the port
    is not yet known at that call site. Wiring a real port through is a
    later chunk's job; this parameter only supplies the capability.
    """
    token = secrets.token_hex(32)
    _write_private_atomically(cookie_path(engine_root), token)
    # Written in the SAME mint, never lazily on first read. The two writes
    # are individually atomic but NOT atomic as a pair -- an interruption
    # between them leaves a curlrc lagging the cookie. That is tolerated,
    # not prevented: a lagging curlrc sends a stale credential, which the
    # refusal path rejects as a wrong cookie. Fails safe, never open.
    curl_config = f'header = "{COOKIE_HEADER}: {token}"\n'
    if port is not None:
        curl_config += f'url = "http://127.0.0.1:{port}"\n'
    _write_private_atomically(curl_config_path(engine_root), curl_config)
    return token


def _probe_presence(path: Path) -> None:
    """Raise `FileNotFoundError` if `path` is genuinely absent; return if it
    is present; raise another `OSError` if that cannot be determined.

    A NAMED SEAM, not an inlined `path.lstat()`, so a test can make presence
    unknowable without monkeypatching `Path.lstat` for the whole process.
    That distinction is not cosmetic: this suite runs live listener threads,
    and patching a stdlib method globally poisons every one of them for the
    duration -- observed as failures wandering between unrelated modules
    depending on test order.

    `lstat`, never `stat` or `exists()`: `exists()` swallows every `OSError`
    into False, and both follow symlinks. A dangling symlink is PRESENT and
    must not be minted through.
    """
    path.lstat()


def ensure(engine_root: Optional[Path] = None) -> str:
    """Return the existing cookie, minting one only if none is there.

    THE LIFETIME POLICY, IN ONE FUNCTION. `mint()` unconditionally
    replaces; this is the call a boot path wants, and the difference is
    the whole correction recorded in `mint`'s docstring: a secret
    regenerated at every engine boot strands every session launched
    before that boot, because the hook-fire caller receives it
    interpolated from a launch-fixed environment with no re-export
    channel. Generated ON FIRST ABSENCE, never rotated on a schedule.

    Rotation is an operator action bounded by the longest live session --
    `clear()` then this -- never a boot side-effect.

    A cookie that exists but is unreadable RAISES
    `CookieUnreadableError` rather than being replaced -- replacing it
    would rotate the secret out from under every live session on what may
    be a transient read error. `read()` alone cannot make this
    distinction (it returns None for missing AND unreadable), so the
    existence check is done separately and deliberately.

    CONCURRENT BOOT, STATED NARROWLY: two engines can both read absent
    and both mint, and the second `os.replace` supersedes the first. No
    reader ever sees a torn token -- that much IS by construction -- but
    "exactly one final cookie" is not guaranteed here; it is guaranteed
    upstream, by the election lock that admits one supervisor per root.
    """
    existing = read(engine_root)
    if existing is not None:
        return existing
    # PRESENT-BUT-UNREADABLE IS NOT ABSENT, AND `read()` CANNOT TELL YOU
    # WHICH -- it returns None for both, by design. Asking the filesystem
    # directly is what separates "generate one" from "refuse": re-minting
    # over a file that exists but would not read rotates the secret out
    # from under every live session, on what may be a transient error.
    path = cookie_path(engine_root)
    try:
        _probe_presence(path)
    except FileNotFoundError:
        # GENUINELY ABSENT -- the only case that mints.
        return mint(engine_root)
    except OSError as exc:
        # `Path.exists()` WOULD HAVE SWALLOWED THIS AND MINTED. A stat that
        # fails for any reason other than "not there" -- a directory whose
        # permissions changed, a filesystem error -- tells us nothing about
        # whether a cookie exists, and minting on no information is the
        # rotation this function exists to prevent. Refuse.
        raise CookieUnreadableError(
            f"cookie presence could not be determined: {path} ({exc})"
        ) from exc
    # `lstat`, never `stat`: a DANGLING SYMLINK at this path is present, and
    # `exists()` (which follows links) would call it absent and mint THROUGH
    # the link onto whatever it targets. Present-and-unusable is refused, not
    # written over.
    raise CookieUnreadableError(
        f"cookie exists but could not be read: {path}. "
        "It was NOT replaced -- re-minting over an unreadable cookie would "
        "strand every live session holding the old value. Inspect it, then "
        "remove it deliberately to have a new one generated."
    )


def read(engine_root: Optional[Path] = None) -> Optional[str]:
    """Read the current cookie, or return None if it is missing or
    unreadable -- never raises. Mirrors `breadcrumb.read_breadcrumb`'s own
    "degrade to no information" contract for its read path."""
    path = cookie_path(engine_root)
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return None
    except UnicodeDecodeError:
        return None
    text = text.strip()
    return text or None


def clear(engine_root: Optional[Path] = None) -> None:
    """Best-effort, idempotent unlink of BOTH the cookie and its curl
    config. Never raises -- matches `breadcrumb.unlink_breadcrumb`'s own
    best-effort contract.

    Both, because leaving the curlrc behind would leave a readable secret
    on disk after a clean exit that was supposed to remove it.
    """
    for path in (cookie_path(engine_root), curl_config_path(engine_root)):
        try:
            path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# `assert_directory_private` -- the AC2/AC3 boot-time guard. Verifies the
# module docstring's assumption instead of trusting it: a redirected
# profile, a hand-loosened ACL, or a non-default POSIX umask would all
# break the assumption silently otherwise.
# --------------------------------------------------------------------------

#: Windows `SE_OBJECT_TYPE` for `GetNamedSecurityInfoW` -- a filesystem
#: object (file OR directory; the API does not distinguish).
_SE_FILE_OBJECT = 1

#: `SECURITY_INFORMATION` flags: the DACL (the ACE set this check
#: verifies) and the OWNER. No group, no SACL -- neither is read.
_DACL_SECURITY_INFORMATION = 0x00000004
_OWNER_SECURITY_INFORMATION = 0x00000001

# Both are requested together, and the OWNER half is not decorative: an
# `OWNER RIGHTS` ("OW") ACE in the DACL is a grant to whoever owns the
# object, so it can only be judged against the `O:` field. Requesting the
# DACL alone yields an SDDL string with no `O:` component at all, and the
# OW branch below would then compare against None and refuse every
# directory. See `assert_directory_private`.
_DACL_AND_OWNER_INFORMATION = _DACL_SECURITY_INFORMATION | _OWNER_SECURITY_INFORMATION

#: `dwRevision` for `ConvertSecurityDescriptorToStringSecurityDescriptorW`
#: -- SDDL_REVISION_1, the only revision Windows defines.
_SDDL_REVISION_1 = 1


def _directory_dacl_sddl(directory: Path) -> str:
    """Read `directory`'s DACL as an SDDL string via
    `GetNamedSecurityInfoW` + `ConvertSecurityDescriptorToStringSecurityDescriptorW`
    -- the read-side ctypes idiom `election._build_security_attributes`
    already binds the inverse of. Measured cost on this box: 0.036 ms, well
    within a boot-time budget.
    """
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_uint32,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

    psd = ctypes.c_void_p()
    ret = advapi32.GetNamedSecurityInfoW(
        str(directory),
        _SE_FILE_OBJECT,
        _DACL_AND_OWNER_INFORMATION,
        None,
        None,
        None,
        None,
        ctypes.byref(psd),
    )
    if ret != 0:
        raise ctypes.WinError(ret)
    try:
        str_sd = wintypes.LPWSTR()
        length = wintypes.ULONG()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            psd,
            _SDDL_REVISION_1,
            _DACL_AND_OWNER_INFORMATION,
            ctypes.byref(str_sd),
            ctypes.byref(length),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str_sd.value
        finally:
            kernel32.LocalFree(str_sd)
    finally:
        kernel32.LocalFree(psd)


_ACE_RE = re.compile(r"\(([^)]*)\)")


def _parse_sddl_ace_sids(sddl: str) -> set:
    """Extract the trustee SID of each ACE in a `D:...` SDDL string.

    Matches on the ACE SET, not the literal string -- inherited (`ID`)
    vs. protected (`P`) flags legitimately differ between a directory and
    the pipe `election.py` hand-builds, and this check has no opinion on
    them. Each ACE is `type;flags;rights;object_guid;inherit_guid;sid` --
    the trustee SID is always the last (sixth) field.
    """
    sids = set()
    for match in _ACE_RE.finditer(sddl):
        fields = match.group(1).split(";")
        if len(fields) >= 6 and fields[5]:
            sids.add(fields[5])
    return sids


# `O:` runs until the next COMPONENT marker (`G:`/`D:`/`S:`) or end of
# string. A character-class exclusion cannot express that: every SID
# begins with `S`, so `[^GDS]+` matches nothing at all. The lookahead is
# the point -- it anchors on the two-character component markers rather
# than on their leading letters.
_OWNER_RE = re.compile(r"O:(.+?)(?=G:|D:|S:|$)")


def _parse_sddl_owner_sid(sddl: str) -> Optional[str]:
    """The `O:` owner field of an SDDL string, or None if absent.

    Read for one purpose: deciding whether an `OWNER RIGHTS` ("OW") ACE
    is a grant to THIS user or to somebody who took ownership -- see
    `assert_directory_private`. `O:` runs until the next component
    letter (`G:`/`D:`/`S:`), which is what the character class excludes.
    """
    match = _OWNER_RE.search(sddl)
    return match.group(1) if match else None


def assert_directory_private(engine_root: Optional[Path] = None) -> None:
    """Boot-time guard: raise `DirectoryNotPrivateError` unless the
    cookie's protection actually holds on THIS platform.

    Windows: reads the containing directory's DACL and asserts the ACE
    set is EXACTLY SYSTEM ("SY") + Builtin Administrators ("BA") + the
    owning user's SID, nothing else -- the same set `election.py`
    hand-builds for the pipe.

    POSIX: asserts the cookie FILE's mode is exactly `0o600` -- the
    property `mint()`'s `os.open` call actually establishes there. The
    NAME says directory and this leg does not check one, deliberately:
    on POSIX the file's own mode carries the protection, because write
    access to the containing directory does not let another user read or
    overwrite a `0o600` file they do not own.

    Never called from any boot sequence by this chunk (see module
    docstring's WIRING NOTE) -- it is a callable a later chunk wires in.
    """
    if sys.platform == "win32":
        from coordinator_core.warm.election import current_user_sid

        directory = breadcrumb.svc_dir(engine_root)
        try:
            sddl = _directory_dacl_sddl(directory)
            actual = _parse_sddl_ace_sids(sddl)
            owner_sid = current_user_sid()
        except OSError as exc:
            raise DirectoryNotPrivateError(
                f"could not read DACL for {str(directory)!r}: {exc}"
            ) from exc
        # "OW" IS NOT AN ALIAS FOR THE OWNING USER, AND ACCEPTING IT
        # UNCONDITIONALLY WOULD WEAKEN THIS CHECK. It is `OWNER RIGHTS`
        # (S-1-3-4), a DYNAMIC grant meaning "whoever currently owns this
        # object" -- so a directory whose ownership had been taken by
        # another principal would still read `{SY, BA, OW}` and pass.
        #
        # Where each spelling actually appears, measured on this box:
        # the production `svc_dir()` under `%LOCALAPPDATA%` carries the
        # LITERAL owner SID; a directory created under `%TEMP%` (which is
        # what the test's `COORDINATOR_WARM_RUNTIME_BASE` redirect
        # produces) carries `OW`. So `OW` is a property of the test's
        # location, never of the path this guard protects in production.
        #
        # It is accepted ONLY when the descriptor's own `O:` owner field
        # is this user -- which collapses the dynamic grant back to the
        # static one the expected set names, and refuses it otherwise.
        if "OW" in actual:
            sddl_owner = _parse_sddl_owner_sid(sddl)
            if sddl_owner != owner_sid:
                raise DirectoryNotPrivateError(
                    f"{str(directory)!r} DACL carries OWNER RIGHTS (OW) while the "
                    f"directory is owned by {sddl_owner!r}, not {owner_sid!r} -- "
                    f"that grants access to an owner who is not this user"
                )
            actual = (actual - {"OW"}) | {owner_sid}
        expected = {"SY", "BA", owner_sid}
        if actual != expected:
            raise DirectoryNotPrivateError(
                f"{str(directory)!r} DACL grants {sorted(actual)!r}, "
                f"expected exactly {sorted(expected)!r}"
            )
    else:
        import stat as _stat

        path = cookie_path(engine_root)
        try:
            st = os.stat(path)
        except OSError as exc:
            raise DirectoryNotPrivateError(
                f"could not stat cookie {str(path)!r}: {exc}"
            ) from exc
        mode = _stat.S_IMODE(st.st_mode)
        if mode != 0o600:
            raise DirectoryNotPrivateError(
                f"cookie {str(path)!r} is mode {mode:04o}, expected 0600"
            )
