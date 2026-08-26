"""coordinator_core.warm.door_credential -- the shared secret the front door
accepts from a hook fire, and the boundary that secret rests on.

Spec backlink: `docs/research/spike-verdicts/2026-08-26-front-door-hook-fire-credential.md`
(verdict `viable`), which gates AC17 of
`docs/plans/2026-08-25-the-bash-guard-stops-paying-for-a-process.md`. Read that
record for the measurements and the threat reasoning; do not restate them here.

WHAT THIS MODULE OWNS -- one per-user secret: where it lives, how it is minted,
how a presented value is compared against it, and the boot-time assertion that
the directory holding it really does exclude other users. Nothing here binds a
socket, reads a header, or answers a request.

WHY A LAUNCH-TIME SECRET AND NOT THE ENGINE'S BOOT COOKIE. The op CLI is our own
code and re-reads a file per invocation, so a cookie regenerated at engine boot
suits it. A hook fire cannot read anything: a `type: "http"` registration's only
channel into the caller's environment is an interpolated header, so whatever the
door accepts must already be in the session's environment before the first fire,
placed there by the `claude-doe` launcher. A session's environment is FIXED AT
LAUNCH with no re-export channel, and a publish evicts the listener (AC18,
measured 7h04m stale with health green throughout). A boot-rotated value is
therefore dead for every session launched before the next publish -- which is
most of them. The secret's lifetime is the USER's, not the engine's.

WHAT A VALID CREDENTIAL PROVES, stated narrowly because the AC it serves says
*authenticate* and a vague claim would be worse than none: the caller is a local
process running as this user that either came through the launcher, descends from
a session that did, or could read `warm_dir()`. It proves the caller is NOT a
browser page, NOT a remote host, and NOT another user on this box -- which is the
threat the fixed, machine-global, published port actually attracts.

It does NOT prove the caller is Claude Code, and no mechanism available here
would. An exported environment variable is inherited by every child process, so
every Bash command this guard evaluates can read it. That residual is accepted
deliberately: a same-user process can already invoke the engine directly, so the
door is not their weakest link and cannot be made their strongest.

NEGATIVE SPEC:
  - No rotation on engine boot, and no delete-on-exit. See the lifetime section
    above; a rotating value silently stops authenticating live sessions.
  - No `os.chmod` on Windows in the belief it restricts anyone -- measured
    inert (it toggles the read-only attribute and moves no ACE). The
    DIRECTORY's inherited ACL is the mechanism there; the mode bits are real
    only on the POSIX leg. Both are covered by the same `os.open` call for
    different reasons.
  - No ctypes `SECURITY_ATTRIBUTES` hand-built for the file. A file written
    plainly into `warm_dir()` already inherits the SYSTEM + Builtin
    Administrators + owning-user ACE set that `election.py` hand-builds for its
    pipe -- measured identical in effective access.
  - This is NOT the engine token and must never be conflated with one. The
    credential authenticates WHO is calling; `skew`'s token detects WHICH
    generation they think they are calling. Two axes, two headers. A wrong
    top-level engine token routes to `skew.evict_on_skew`, which takes the warm
    engine down for every peer rather than refusing one caller -- so the engine
    token must never be exported into a session environment to "complete" a
    forwarding path.
  - No `secrets.compare_digest` shortcut with `==`. Timing is not the live
    threat here, but a constant-time compare costs 60 nanoseconds and removes
    the question.
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.warm import breadcrumb

__all__ = [
    "CREDENTIAL_HEADER",
    "CREDENTIAL_ENV",
    "SECRET_FILENAME",
    "SECRET_NBYTES",
    "warm_dir",
    "secret_path",
    "read_secret",
    "ensure_secret",
    "credential_from_headers",
    "verify",
    "DirectoryBoundaryError",
    "assert_directory_excludes_others",
]

#: The header a fire's credential travels on. Matches this package's existing
#: `X-Coordinator-*` naming (`http_listener.ENGINE_TOKEN_HEADER`,
#: `front_door_routing.CLONE_IDENTITY_HEADER`).
CREDENTIAL_HEADER = "X-Coordinator-Door-Key"

#: The env var the launcher exports and the registration interpolates into
#: `CREDENTIAL_HEADER`. Non-`CLAUDE_`-prefixed, for the same measured reason
#: `COORDINATOR_CLONE_ROOT` is: `${CLAUDE_*}` path placeholders expand to `''`
#: over this transport.
CREDENTIAL_ENV = "COORDINATOR_DOOR_KEY"

SECRET_FILENAME = "door-key"

#: 32 bytes -> 64 hex characters. Not tunable: a caller passing a smaller value
#: would weaken the one property the secret has.
SECRET_NBYTES = 32


def warm_dir() -> Path:
    """The per-user warm directory the per-clone `svc_dir()`s hang under --
    `<runtime base>/coordinator/warm`, the exact composition `breadcrumb.
    svc_dir` uses before appending its clone hash.

    PER-USER, NOT PER-CLONE, and that is the whole point. The front door binds
    ONE fixed, machine-global port and multiplexes every clone on the box
    (`front_door_routing`'s reason for existing). A secret under a clone hash
    would authenticate a session only to its own clone's door, of which there
    is at most one on the machine -- so a session launched against clone A
    could not authenticate to the door clone B happened to win the election
    with. The credential's scope has to match the door's, and the door's is the
    user.

    `election.ensure_private_dir` already applies its ownership check to this
    exact directory (`coordinator/`, `warm/`, `<clone-hash>/`), so the boundary
    here is one the package already asserts rather than a new one.
    """
    return breadcrumb.runtime_base() / "coordinator" / "warm"


def secret_path() -> Path:
    """Where the secret lives: beside the per-clone directories, one per user."""
    return warm_dir() / SECRET_FILENAME


class DirectoryBoundaryError(RuntimeError):
    """The directory holding the secret does not exclude other users, so the
    secret it holds is not a secret. Raised rather than warned: a credential
    resting on a boundary that has been checked and found absent is worse than
    no credential, because callers believe it."""


def _windows_sddl(path: Path) -> Optional[str]:
    """The path's DACL as an SDDL string, or `None` if it cannot be read.

    `election.py` already binds the inverse conversion
    (`ConvertStringSecurityDescriptorToSecurityDescriptorW`) to BUILD a
    descriptor; this is the same idiom read backwards, not a new dependency.
    """
    import ctypes

    advapi32 = ctypes.windll.advapi32
    psd = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    DACL_SECURITY_INFORMATION = 4
    SE_FILE_OBJECT = 1
    rc = advapi32.GetNamedSecurityInfoW(
        ctypes.c_wchar_p(str(path)),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(psd),
    )
    if rc != 0:
        return None
    out = ctypes.c_wchar_p()
    ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        psd,
        1,
        DACL_SECURITY_INFORMATION,
        ctypes.byref(out),
        None,
    )
    if not ok:
        return None
    return out.value


def assert_directory_excludes_others() -> str:
    """Verify -- never assume -- that the secret's directory admits only
    SYSTEM, Builtin Administrators, and the owning user, and return the
    evidence as a string a test can assert on.

    Called at door boot, not per fire: measured at 0.036ms, cheap enough to run
    every boot rather than be cached, and far too expensive to run per request
    against a mechanism whose whole appeal is 60 nanoseconds.

    The Windows leg checks the inherited DACL because that IS the boundary
    there. The POSIX leg checks the mode bits because there they are real.
    Neither is a fallback for the other.
    """
    directory = warm_dir()
    if sys.platform == "win32":
        sddl = _windows_sddl(directory)
        if sddl is None:
            raise DirectoryBoundaryError(
                "cannot read the DACL of %s, so the secret's boundary is "
                "unverified" % directory
            )
        lowered = sddl.lower()
        for well_known in (";;;wd)", ";;;au)", ";;;bu)"):
            if well_known in lowered:
                raise DirectoryBoundaryError(
                    "%s grants access beyond SYSTEM/Administrators/owner "
                    "(SDDL %s)" % (directory, sddl)
                )
        return sddl
    mode = directory.stat().st_mode & 0o777
    if mode & 0o077:
        raise DirectoryBoundaryError(
            "%s is mode %s -- group or other can reach the secret" % (directory, oct(mode))
        )
    return oct(mode)


def read_secret() -> Optional[str]:
    """The stored secret, or `None` when none has been minted yet.

    Never raises on an unreadable or absent file: a door that cannot read its
    own secret must answer every fire as unauthenticated (loud, per AC6/AC15),
    not fail to boot and leave the port unbound.
    """
    try:
        value = secret_path().read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return value or None


def ensure_secret() -> str:
    """Return the stored secret, minting one on FIRST ABSENCE only.

    Atomic `.tmp` -> `os.replace`, which is what prevents a concurrent reader
    seeing a partial value -- Bitcoin Core's `.cookie` shape, and the one part
    of it that transfers here. The mint is not rotation: an existing secret is
    returned untouched, because a live session's exported copy cannot be
    refreshed and overwriting would silently stop authenticating it.

    Concurrent mints race benignly: whoever replaces last wins, and a session
    holding the loser's value simply reads as unauthenticated at its next fire
    rather than being served wrongly.
    """
    existing = read_secret()
    if existing is not None:
        return existing

    path = secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    minted = secrets.token_hex(SECRET_NBYTES)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, minted.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return minted


def credential_from_headers(headers: Mapping[str, str]) -> Optional[str]:
    """Read the credential off a fire's headers, or `None` when absent.

    Case-folded explicitly for the same reason
    `front_door_routing.clone_identity_from_headers` folds: HTTP header names
    are case-insensitive on the wire and `HTTPMessage` handles it, but the
    plain `dict` every test constructs does not.

    A present-but-EMPTY value returns `None`, not `""`. Over this transport an
    unexported variable interpolates to the empty string, so empty and absent
    are the same fact -- and an `httpHookAllowedEnvVars` veto also empties the
    header rather than erroring. Both must read as "no credential", never as a
    credential that happens to be blank.
    """
    if not headers:
        return None
    target = CREDENTIAL_HEADER.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value if isinstance(value, str) and value else None
    return None


def verify(presented: Optional[str], held: Optional[str]) -> bool:
    """Whether `presented` is the secret. `None` on either side is False --
    a door with no secret authenticates nobody rather than everybody."""
    if not presented or not held:
        return False
    return hmac.compare_digest(presented, held)
