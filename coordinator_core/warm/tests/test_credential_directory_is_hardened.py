"""The front door's credential directory is made private, not assumed to be.

THE DEFECT THIS PINS, measured on a healthy macOS box (2026-09-02):

    >>> door_credential.warm_dir()
    PosixPath('/Users/<user>/.cache/coordinator/warm')     # mode 0o755
    >>> door_credential.assert_directory_excludes_others()
    DirectoryBoundaryError: ... is mode 0o755 -- group or other can reach the secret

Nothing was wrong with that machine. `ensure_secret`'s own
`path.parent.mkdir(parents=True, exist_ok=True)` created the directory with no
mode, so a default umask of 022 left it 0755, and the door's boot check then
refused a directory the door itself had made. `front_door.load_secret` catches
that refusal and returns None -- deliberately, so a bad boundary cannot brick
the port -- so the visible symptom is not a crash but every fire answering
`credential_absent`, forever, on every POSIX box with an ordinary umask.

WHY THE ASSUMPTION LOOKED SAFE. `warm_dir`'s docstring argued the boundary was
already asserted by `election.ensure_private_dir`, which does check this exact
path. It checks it with `_verify_owned_ancestor`, which is WEAKER on purpose
and says so: an interposed directory need only be unwritable by others, because
demanding 0700 there "would reject a directory an earlier version of this code
legitimately created at 0755". An ownership check on an ancestor is not a mode
fix on a leaf. Two correct functions, one wrong inference between them.

WHAT THE FIX MUST NOT DO, and what the last test here exists to hold: silence
the refusal. Repairing a 0755 that this package's own `mkdir` produced is
repair. A directory owned by someone else, or a symlink standing where it
should be, is not a umask artifact and must still refuse -- otherwise the fix
for "the door never holds a credential" becomes "the door holds one anywhere".
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, door_credential
from coordinator_core.warm.election import InsecureRuntimeDirError

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="mode bits are the boundary on POSIX; the Windows leg checks the inherited DACL",
)


@pytest.fixture
def runtime_base(monkeypatch):
    """A private runtime base, kept SHORT deliberately. Sibling suites here were
    red on macOS for months because pytest's `tmp_path` is deep enough that the
    derived socket path blows the `sun_path` budget; nothing in this file binds
    a socket, but inheriting the habit costs nothing and the next test added
    beside these might."""
    base = Path(tempfile.mkdtemp(prefix="cred-", dir="/tmp"))
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
    yield base


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
def test_a_default_umask_still_produces_a_private_directory(runtime_base, monkeypatch):
    """The exact condition the box was in. 022 is the default on essentially
    every distribution and on macOS, so this is the ordinary case, not an edge
    one -- which is why the defect reached every POSIX deployment at once."""
    previous = os.umask(0o022)
    try:
        directory = door_credential.ensure_directory_excludes_others()
    finally:
        os.umask(previous)

    assert _mode(directory) == 0o700
    assert door_credential.assert_directory_excludes_others() == oct(0o700)


@posix_only
def test_an_existing_0755_directory_is_repaired(runtime_base):
    """Every box that already ran the old code is in this state -- the fix has
    to reach them, not just be correct for a fresh install."""
    directory = door_credential.warm_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    assert _mode(directory) == 0o755

    door_credential.ensure_directory_excludes_others()

    assert _mode(directory) == 0o700


@posix_only
def test_minting_a_secret_hardens_the_directory_it_mints_into(runtime_base):
    """`ensure_secret` was the creation site: a bare `mkdir` with no mode. A
    secret minted into a world-readable directory is not a secret, and the
    assert that would have caught it runs at door boot -- possibly on another
    process, possibly much later."""
    previous = os.umask(0o022)
    try:
        secret = door_credential.ensure_secret()
    finally:
        os.umask(previous)

    assert secret
    assert _mode(door_credential.warm_dir()) == 0o700
    assert _mode(door_credential.secret_path()) == 0o600


@posix_only
def test_the_secret_survives_hardening(runtime_base):
    """Hardening is not rotation. An existing secret must come back untouched --
    a live session's exported copy cannot be refreshed, so silently minting a
    new one would stop authenticating it."""
    first = door_credential.ensure_secret()
    second = door_credential.ensure_secret()

    assert first == second
    assert door_credential.read_secret() == first


@posix_only
def test_a_symlink_standing_in_for_the_directory_still_refuses(runtime_base):
    """THE REFUSAL SURVIVES. This is the half a careless fix would delete: the
    point of the boundary check is the case where someone else put something
    there, and 'make it 0700' must never become 'make it acceptable'. A symlink
    is the specific vector -- it would otherwise pass every check while pointing
    the secret somewhere the attacker chose."""
    directory = door_credential.warm_dir()
    directory.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = runtime_base / "elsewhere"
    elsewhere.mkdir()
    directory.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(InsecureRuntimeDirError):
        door_credential.ensure_directory_excludes_others()


def test_the_hardener_is_exported_beside_the_assertion():
    """They are a pair, and a caller that can reach one must be able to reach
    the other -- `front_door.load_secret` calls both, in that order."""
    assert "ensure_directory_excludes_others" in door_credential.__all__
    assert "assert_directory_excludes_others" in door_credential.__all__
