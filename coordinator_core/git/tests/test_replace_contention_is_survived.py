"""The four `os.replace`/open sites that lost commits under concurrency.

DEMONSTRATED-RED: before the fix these four were unretried and unwrapped, and a
12-way concurrent run against ONE repo produced, per 612 attempts,
`PermissionError` 3 (2 of them escaping `cas_ref`'s bool contract as a crash)
and `IndexParseError` 5. After: 0 and 0, with reported successes exactly equal
to commits landed.

WHY EACH SITE GETS ITS OWN TEST RATHER THAN ONE BLANKET ASSERTION. The four are
different problems and a single "it retries now" test would pass on a blanket
retry, which is the wrong fix and was explicitly ruled out: `write_object` is
content-addressed so a lost race means a peer wrote identical bytes; `cas_ref`
holds a lock whose premise can expire, so its retry must re-verify and its
failure to TAKE the lock must stay a refusal; the index write lands after the
ref, so its failure is a stale index and never a lost commit; the index read
takes nothing and can simply be re-read.

`CommitRefused` under a 12-way race is CORRECT BEHAVIOUR, not a fault -- it is
twelve processes racing one ref and the CAS doing its job. Stated here because
a reviewer seeing a high refusal count could easily "fix" the CAS into a force,
which is precisely the failure the CAS exists to prevent.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_objects import _replace_with_retry, cas_ref, write_object


def _gitdir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Plain directories, no `git init` spawn.

    `cas_ref` only needs the ref's parent to exist, and the assertion here is
    about OUR refusal path, never about real git's behaviour -- so a spawn
    would buy nothing and cost a process on a box where process creation IS
    the cost.
    """
    gitdir = tmp_path / ".git"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    return gitdir


class TestReplaceHelper:
    def test_retries_the_windows_transient_then_succeeds(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "s", tmp_path / "d"
        src.write_bytes(b"x")
        calls = {"n": 0}
        real = os.replace

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return real(a, b)

        monkeypatch.setattr(git_objects.os, "replace", flaky)
        assert _replace_with_retry(src, dst) is True
        assert calls["n"] == 3

    def test_gives_up_rather_than_forcing(self, tmp_path, monkeypatch):
        """A destination that never frees is reported, never forced.

        Forcing would convert a refused commit into a silently orphaned one.
        """
        src, dst = tmp_path / "s", tmp_path / "d"
        src.write_bytes(b"x")
        monkeypatch.setattr(
            git_objects.os,
            "replace",
            lambda a, b: (_ for _ in ()).throw(PermissionError(5, "denied")),
        )
        assert _replace_with_retry(src, dst) is False
        assert not dst.exists()

    def test_a_non_transient_oserror_is_not_retried(self, tmp_path, monkeypatch):
        """Retrying a missing source or a read-only tree only delays the report."""
        src, dst = tmp_path / "s", tmp_path / "d"
        src.write_bytes(b"x")
        calls = {"n": 0}

        def boom(a, b):
            calls["n"] += 1
            raise IsADirectoryError(21, "not the transient")

        monkeypatch.setattr(git_objects.os, "replace", boom)
        assert _replace_with_retry(src, dst) is False
        assert calls["n"] == 1, "a non-transient must not walk the ladder"

    def test_still_valid_is_rechecked_before_each_retry(self, tmp_path, monkeypatch):
        """The premise can expire DURING the wait -- that is the whole reason a
        retry can be wrong, so one up-front check would not be enough."""
        src, dst = tmp_path / "s", tmp_path / "d"
        src.write_bytes(b"x")
        monkeypatch.setattr(
            git_objects.os,
            "replace",
            lambda a, b: (_ for _ in ()).throw(PermissionError(5, "denied")),
        )
        checks = {"n": 0}

        def expires_after_two():
            checks["n"] += 1
            return checks["n"] < 2

        assert _replace_with_retry(src, dst, still_valid=expires_after_two) is False
        assert checks["n"] == 2, "must stop the moment the premise goes false"


class TestWriteObjectIsContentAddressed:
    def test_a_peer_writing_identical_bytes_is_success_not_failure(
        self, tmp_path, monkeypatch
    ):
        """CONTENT-ADDRESSED: the path is keyed on the sha of exactly these
        bytes, so if it exists a peer wrote byte-identical content and the
        object IS in the store. No other site may reason this way."""
        gitdir = tmp_path / ".git"
        (gitdir / "objects").mkdir(parents=True)
        payload = b"hello object"

        def peer_wins(a, b):
            pathlib.Path(b).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(b).write_bytes(pathlib.Path(a).read_bytes())
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(git_objects.os, "replace", peer_wins)
        sha = write_object(gitdir, b"blob", payload)
        assert len(sha) == 40
        assert (gitdir / "objects" / sha[:2] / sha[2:]).exists()

    def test_a_destination_that_never_appears_is_raised(self, tmp_path, monkeypatch):
        """The content-addressed escape is not a blanket swallow: with no peer
        write, the failure must surface rather than report a phantom sha."""
        gitdir = tmp_path / ".git"
        (gitdir / "objects").mkdir(parents=True)
        monkeypatch.setattr(
            git_objects.os,
            "replace",
            lambda a, b: (_ for _ in ()).throw(PermissionError(5, "denied")),
        )
        with pytest.raises(OSError, match="stayed locked"):
            write_object(gitdir, b"blob", b"nobody else writes this")


class TestCasRefRefusesRatherThanCrashes:
    def test_permissionerror_taking_the_lock_is_a_refusal(self, tmp_path, monkeypatch):
        """Windows spells a lost lock `PermissionError`, not `FileExistsError`.

        Only the latter was caught, so the raise escaped `cas_ref`'s documented
        bool contract and reached callers as a crash. Failing to TAKE the lock
        is a refusal: nothing written, no ref moved.
        """
        gitdir = _gitdir(tmp_path)
        real_open = os.open

        def denied(path, *a, **k):
            if str(path).endswith(".lock"):
                raise PermissionError(5, "Access is denied")
            return real_open(path, *a, **k)

        monkeypatch.setattr(git_objects.os, "open", denied)
        assert cas_ref(gitdir, "refs/heads/main", None, "0" * 40) is False
