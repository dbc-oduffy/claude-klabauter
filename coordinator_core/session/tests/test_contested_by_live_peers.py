"""`scope.contested_by_live_peers` -- the narrow peer-claim read behind the
explicit-pathspec commit gate.

WHY THIS EXISTS. Measured 2026-08-31 on ``work/machine-a/2026-08-18to31``:
sessions ``d12e25cf`` and ``1ad288d0`` each held uncommitted hunks in
``coordinator_core/workstream_complete/__init__.py``; ``e74e4ce8`` committed the
whole file at ``40abe011d0`` for an unrelated fix and landed both, under a
message describing neither. ``coordinator-safe-commit "<msg>" -- <paths>``
routes through ``do_pathspec`` -> ``ceremony.commit_v2``, which gates nothing,
and ``check_validate_commit`` never fires on it because its own regex matches a
literal ``git commit`` only. Check 5's C11 hash arm could not have caught it
either: ``e74e4ce8`` wrote the file LAST, so its recorded hash matched disk.

The signal that WAS available: two live peers still held unreleased TOUCHes.
This function reads exactly that, for exactly the named paths -- ``compute_scope``
answers the same question but costs 437ms process time on this repo against a
500ms brightline, because it also enumerates ~900 orphans and mtime-scans the
tree.

FAILURE DIRECTION, pinned: this sits on the commit hot path every live session
shares, so every unreadable/unresolvable state returns ``{}`` -- "could not
establish a contest" is never "contested". A refusal must rest on a claim that
was actually read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.session import core, scope, touch_record


def _claim(root: str, sid: str, path: str, verb: str = touch_record.VERB_TOUCH) -> None:
    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sdir),
        session_id=sid,
        agent_id=None,
        verb=verb,
        path=path,
    )


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


class TestContestedByLivePeers:
    def test_names_every_live_peer_holding_the_path(self, repo, monkeypatch):
        """The incident shape: TWO peers on one file. A refusal naming one of
        them sends the caller to coordinate with half the people it needs to,
        which is why this is not a single merged projection."""
        monkeypatch.setattr(scope.liveness, "session_live", lambda sid, cwd=None: True)
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
        for sid in ("mine", "peer-a", "peer-b"):
            core.init(sid, cwd=repo)
        _claim(repo, "mine", "pkg/mod.py")
        _claim(repo, "peer-a", "pkg/mod.py")
        _claim(repo, "peer-b", "pkg/mod.py")

        result = scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo)

        assert result == {"pkg/mod.py": ["peer-a", "peer-b"]}

    def test_own_claim_alone_is_not_a_contest(self, repo, monkeypatch):
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
        for sid in ("mine", "peer-a"):
            core.init(sid, cwd=repo)
        _claim(repo, "mine", "pkg/mod.py")
        _claim(repo, "peer-a", "pkg/other.py")

        assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {}

    def test_released_peer_claim_is_not_a_contest(self, repo, monkeypatch):
        """A RELEASE is the peer saying it is done. Treating it as ownership
        would wedge every path any session ever touched."""
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
        for sid in ("mine", "peer-a"):
            core.init(sid, cwd=repo)
        _claim(repo, "peer-a", "pkg/mod.py")
        _claim(repo, "peer-a", "pkg/mod.py", verb=touch_record.VERB_RELEASE)

        assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {}

    def test_dead_peer_claim_is_not_a_contest(self, repo, monkeypatch):
        monkeypatch.setattr(
            touch_record, "session_live", lambda sid, cwd=None: sid == "mine"
        )
        for sid in ("mine", "peer-a"):
            core.init(sid, cwd=repo)
        _claim(repo, "peer-a", "pkg/mod.py")

        assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {}

    def test_answers_only_about_the_named_paths(self, repo, monkeypatch):
        """Negative-spec: this is not a second `compute_scope`. A contested path
        the caller did not name must not appear."""
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
        for sid in ("mine", "peer-a"):
            core.init(sid, cwd=repo)
        _claim(repo, "peer-a", "pkg/mod.py")
        _claim(repo, "peer-a", "pkg/unnamed.py")

        result = scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo)

        assert result == {"pkg/mod.py": ["peer-a"]}


class TestFailsOpen:
    def test_empty_session_id_yields_no_contest(self, repo):
        assert scope.contested_by_live_peers(["pkg/mod.py"], "", repo) == {}

    def test_empty_path_set_yields_no_contest(self, repo):
        assert scope.contested_by_live_peers([], "mine", repo) == {}

    def test_absent_session_hub_yields_no_contest(self, tmp_path):
        assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", str(tmp_path)) == {}

    def test_raising_projection_yields_no_contest(self, repo, monkeypatch):
        """A bookkeeping outage must not become a fleet-wide commit refusal."""
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
        for sid in ("mine", "peer-a"):
            core.init(sid, cwd=repo)
        _claim(repo, "peer-a", "pkg/mod.py")

        def _boom(*args, **kwargs):
            raise OSError("sink unreadable")

        monkeypatch.setattr(touch_record, "project_live_claims", _boom)

        assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {}
