"""Tests for `coordinator_core.warm.election`'s POSIX unix-socket arm.

TWO TIERS, AND THE SPLIT IS THE POINT. The POSIX election has no
`FILE_FLAG_FIRST_PIPE_INSTANCE` behind it, so what Windows gets from the
kernel as one atomic call is here a decision machine written in Python:
probe the existing path, classify the verdict, unlink only a proven corpse,
re-bind once. That machine contains no syscall of its own and is driven
through an injectable `probe`, so EVERY decision in it is exercised on any
platform -- including the Windows box this file was authored on, which is
where the tiering matters:

  - Tier 1, runs everywhere (including Windows): path derivation, the
    sun_path budget, and the whole stale-socket state machine
    (`reclaim_stale_socket`) against a fake probe. These are the tests that
    would catch a wrong decision.
  - Tier 2, POSIX kernel required and SKIPPED WITH A REASON off it: real
    `bind`/`listen`/`connect`, the 0700 directory check, the flock, and a
    real corpse being reclaimed. These are the tests that would catch a
    wrong syscall.

A Tier-2 skip on Windows is an honest absence of evidence, not a pass. The
POSIX syscall paths in `election.py` have NEVER been executed as of
2026-08-21; the first person to run this file on a Mac or a Linux box is
running them for the first time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.warm import election

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs a POSIX kernel: AF_UNIX bind/listen/connect, mode 0700, fcntl.flock",
)

SHORT_BASE = "/run/u"


@pytest.fixture
def short_runtime_base():
    """The suite-root warm-runtime base, NAMED so a test can build socket
    paths under it directly.

    `coordinator_core/conftest.py::_quarantine_real_home` already redirects
    `breadcrumb.RUNTIME_BASE_ENV` to a short, real, per-test root under
    `/tmp` on POSIX (removed on that fixture's own teardown) -- the fix for
    exactly the `sun_path` overflow this fixture used to work around by
    minting a second short tempdir. That fixture is autouse and sets an env
    var rather than returning a nameable value, so this fixture reads the
    same env var back and hands it out by name, for the tests below that
    build paths (`short_runtime_base / "svc" / "tok.sock"`) rather than
    reading `RUNTIME_BASE_ENV` themselves.
    """
    from coordinator_core.warm import breadcrumb

    return Path(os.environ[breadcrumb.RUNTIME_BASE_ENV])


def test_socket_path_is_the_breadcrumbs_own_svc_dir(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.warm import breadcrumb

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, SHORT_BASE)
    path = election.socket_path("abc123", engine_clone=tmp_path)

    assert path.parent == breadcrumb.svc_dir(tmp_path)
    assert path.name == "abc123" + election.SOCKET_SUFFIX


def test_socket_path_changes_with_token_and_clone(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.warm import breadcrumb

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, SHORT_BASE)
    other_clone = tmp_path / "other"
    other_clone.mkdir()

    assert election.socket_path("tok1", engine_clone=tmp_path) != election.socket_path(
        "tok2", engine_clone=tmp_path
    )
    assert election.socket_path("tok1", engine_clone=tmp_path) != election.socket_path(
        "tok1", engine_clone=other_clone
    )
    assert election.socket_path("tok1", engine_clone=tmp_path) == election.socket_path(
        "tok1", engine_clone=tmp_path
    )


def test_runtime_base_has_no_xdg_branch(monkeypatch) -> None:
    """CONTRACT PIN, not a style check. The C door recomputes this base
    independently; a candidate one side consults and the other does not
    produces NO ERROR -- the door finds no socket, goes cold forever, and
    every surface stays green. PM-locked 2026-08-21 to three candidates.
    Adding a fourth means changing the door in the same move.
    """
    from coordinator_core.warm import breadcrumb

    monkeypatch.delenv(breadcrumb.RUNTIME_BASE_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/501")

    assert breadcrumb.runtime_base() == Path.home() / ".cache"


def test_runtime_base_candidate_order_is_the_locked_one(monkeypatch) -> None:
    """`$COORDINATOR_WARM_RUNTIME_BASE`, else `%LOCALAPPDATA%`, else
    `~/.cache` -- the order the door reimplements."""
    from coordinator_core.warm import breadcrumb

    monkeypatch.setenv("LOCALAPPDATA", "/local")
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, "/override")
    assert breadcrumb.runtime_base() == Path("/override")

    monkeypatch.delenv(breadcrumb.RUNTIME_BASE_ENV)
    assert breadcrumb.runtime_base() == Path("/local")

    monkeypatch.delenv("LOCALAPPDATA")
    assert breadcrumb.runtime_base() == Path.home() / ".cache"


def test_guarded_ancestors_are_exactly_the_ones_we_create() -> None:
    base = Path("/home/u/.cache")
    leaf = base / "coordinator" / "warm" / "abc123"

    assert election._interposed_ancestors(leaf / "tok.sock", base) == [
        leaf,
        base / "coordinator" / "warm",
        base / "coordinator",
    ]


def test_guarded_ancestors_never_include_the_operators_own_base() -> None:
    base = Path("/home/u/.cache")
    leaf = base / "coordinator" / "warm" / "abc123" / "tok.sock"

    guarded = election._interposed_ancestors(leaf, base)
    assert base not in guarded
    assert base.parent not in guarded
    assert base.parent.parent not in guarded


def test_guarded_ancestors_of_a_path_outside_the_base_is_empty() -> None:
    assert election._interposed_ancestors(Path("/elsewhere/x.sock"), Path("/home/u/.cache")) == []
    assert election._interposed_ancestors(Path("/a/b/c.sock"), None) == []


def test_socket_path_refuses_a_path_over_the_sun_path_budget(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.warm import breadcrumb

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / ("d" * 200)))
    with pytest.raises(election.SocketPathTooLongError):
        election.socket_path("tok1", engine_clone=tmp_path)


def test_reclaim_unlinks_only_a_refused_endpoint(tmp_path: Path) -> None:
    """ECONNREFUSED is the ONLY proof of staleness POSIX offers, and it is
    the only verdict that may remove a file."""
    corpse = tmp_path / "dead.sock"
    corpse.write_bytes(b"")

    assert election.reclaim_stale_socket(corpse, probe=lambda p: election.PROBE_STALE) is True
    assert not corpse.exists()


def test_reclaim_leaves_a_live_endpoint_alone(tmp_path: Path) -> None:
    live = tmp_path / "live.sock"
    live.write_bytes(b"")

    assert election.reclaim_stale_socket(live, probe=lambda p: election.PROBE_LIVE) is False
    assert live.exists()


def test_reclaim_reports_nothing_removed_for_an_absent_path(tmp_path: Path) -> None:
    absent = tmp_path / "gone.sock"
    assert election.reclaim_stale_socket(absent, probe=lambda p: election.PROBE_ABSENT) is False


def test_reclaim_never_swallows_an_unclassified_probe_failure(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded.sock"
    guarded.write_bytes(b"")

    def _boom(path):
        raise PermissionError(13, "denied")

    with pytest.raises(PermissionError):
        election.reclaim_stale_socket(guarded, probe=_boom)
    assert guarded.exists()


def test_reclaim_treats_a_concurrently_removed_corpse_as_reclaimed(tmp_path: Path) -> None:
    already_gone = tmp_path / "raced.sock"
    assert election.reclaim_stale_socket(already_gone, probe=lambda p: election.PROBE_STALE) is True


def test_socket_identity_and_ownership_checked_unlink(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.sock"
    path.write_bytes(b"")
    mine = election.socket_identity(path)
    assert mine is not None

    path.unlink()
    path.write_bytes(b"successor")
    assert election.unlink_if_owned(path, mine) is False
    assert path.exists()

    assert election.unlink_if_owned(path, election.socket_identity(path)) is True
    assert not path.exists()


def test_unlink_if_owned_with_no_identity_removes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "someone-elses.sock"
    path.write_bytes(b"")
    assert election.unlink_if_owned(path, None) is False
    assert path.exists()


def test_election_lost_endpoint_alias_matches_the_pinned_attribute() -> None:
    lost = election.ElectionLost("/tmp/x.sock")
    assert lost.endpoint == lost.pipe_name == "/tmp/x.sock"
    assert isinstance(lost, election.ElectionError)


def test_posix_only_entry_points_refuse_to_run_on_windows() -> None:
    real_is_windows = election._is_windows
    election._is_windows = lambda: True
    try:
        with pytest.raises(RuntimeError):
            election.current_user_id()
        with pytest.raises(RuntimeError):
            election.elect_unix_socket(Path("/does/not/matter.sock"))
    finally:
        election._is_windows = real_is_windows


@posix_only
def test_ensure_private_dir_lands_0700_under_a_permissive_umask(tmp_path: Path) -> None:
    """The whole reason the mode is VERIFIED rather than requested: under
    umask 022 a `mkdir(0700)` lands 0755, and the directory is the connect
    boundary on POSIX."""
    import os
    import stat

    old = os.umask(0o022)
    try:
        target = election.ensure_private_dir(tmp_path / "svc")
    finally:
        os.umask(old)

    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o700


@posix_only
def test_ensure_private_dir_refuses_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(election.InsecureRuntimeDirError):
        election.ensure_private_dir(link)


@posix_only
def test_a_writable_parent_is_refused_not_merely_noted(tmp_path: Path, monkeypatch) -> None:
    """THE SUBSTITUTION VECTOR. A world-writable `coordinator/warm` lets
    another local account rename our per-clone directory aside and put its
    own there -- after which every 0700 check on "the leaf" passes, on the
    attacker's directory. The election must refuse to bind, not warn."""
    import os

    from coordinator_core.warm import breadcrumb

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path))
    warm_dir = tmp_path / "coordinator" / "warm"
    warm_dir.mkdir(parents=True, mode=0o700)

    leaf = warm_dir / "abc123"
    election.ensure_private_dir(leaf, base=tmp_path)

    os.chmod(warm_dir, 0o777)
    monkeypatch.setattr(election.os, "chmod", lambda *a, **k: None)
    with pytest.raises(election.InsecureRuntimeDirError):
        election.ensure_private_dir(leaf, base=tmp_path)


@posix_only
def test_a_writable_parent_is_repaired_when_it_can_be(tmp_path: Path) -> None:
    import os
    import stat

    warm_dir = tmp_path / "coordinator" / "warm"
    warm_dir.mkdir(parents=True, mode=0o755)
    leaf = warm_dir / "abc123"

    os.chmod(warm_dir, 0o777)
    election.ensure_private_dir(leaf, base=tmp_path)

    assert not (stat.S_IMODE(os.lstat(warm_dir).st_mode) & 0o022)


@posix_only
def test_elect_verifies_the_ancestors_it_was_given_a_base_for(
    tmp_path: Path, short_runtime_base: Path, monkeypatch
) -> None:
    from coordinator_core.warm import breadcrumb

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(short_runtime_base))
    path = election.socket_path("tok1", engine_clone=tmp_path)

    seen: list = []
    monkeypatch.setattr(election, "_verify_owned_ancestor", lambda p: seen.append(p))
    sock = election.elect_unix_socket(path)
    try:
        assert path.parent.parent in seen, "coordinator/warm was never verified"
    finally:
        sock.close()


@posix_only
def test_elect_wins_binds_and_listens(short_runtime_base: Path) -> None:
    import socket

    path = short_runtime_base / "svc" / "tok.sock"
    sock = election.elect_unix_socket(path)
    try:
        assert isinstance(sock, socket.socket)
        assert path.exists()
        assert election.probe_endpoint(path) == election.PROBE_LIVE
    finally:
        sock.close()


@posix_only
def test_elect_loses_to_a_live_owner(short_runtime_base: Path) -> None:
    path = short_runtime_base / "svc" / "tok.sock"
    winner = election.elect_unix_socket(path)
    try:
        with pytest.raises(election.ElectionLost) as excinfo:
            election.elect_unix_socket(path)
        assert excinfo.value.endpoint == str(path)
    finally:
        winner.close()


@posix_only
def test_elect_reclaims_a_hard_killed_servers_socket(short_runtime_base: Path) -> None:
    """THE test this whole POSIX arm exists for. A server killed without
    cleanup leaves a socket FILE; `bind()` then fails EADDRINUSE forever
    against a path nothing is listening on, and no amount of retrying fixes
    it. Closing the socket without unlinking reproduces exactly that state.
    """
    path = short_runtime_base / "svc" / "tok.sock"
    dead = election.elect_unix_socket(path)
    dead.close()
    assert path.exists()
    assert election.probe_endpoint(path) == election.PROBE_STALE

    successor = election.elect_unix_socket(path)
    try:
        assert election.probe_endpoint(path) == election.PROBE_LIVE
    finally:
        successor.close()


@posix_only
def test_a_held_election_lock_reads_as_a_loss(tmp_path: Path) -> None:
    path = tmp_path / "svc" / "tok.sock"
    election.ensure_private_dir(path.parent)

    fd = election._acquire_election_lock(path)
    try:
        with pytest.raises(election.ElectionLost):
            election.elect_unix_socket(path)
    finally:
        election._release_election_lock(fd)


@posix_only
def test_a_probe_of_a_never_created_path_is_absent(short_runtime_base: Path) -> None:
    assert election.probe_endpoint(short_runtime_base / "nothing.sock") == election.PROBE_ABSENT
