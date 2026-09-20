"""A read touch and a write touch are different claims, and only one blocks.

`state/bug-queue/2026-09-20-the-touch-record-cannot-distinguish-a-read-touch-
from-a-write-touch.yaml`. Every touch was a bare `T`, so a session that opened
a file to trace a bug was recorded identically to the session editing it, and
`coordinator-safe-commit` refused a third party's commit naming BOTH as
holders. Only one had written anything; the operator messaged the other by
name and was told it had never touched the bytes.

The fix is a third AXIS, not a third verb: a hold is still T-or-R, and the
kind says what the holder did with the path. Three properties are pinned here
because getting any one of them wrong reopens the defect or replaces it with a
worse one:

  1. a read hold does not contest a peer's commit;
  2. a write hold does, unchanged;
  3. an UNKNOWN hold does too -- every line predating the axis carries no
     kind, and treating those as reads would retire the guard for the whole
     existing corpus in a single edit. Too strict becomes unsound.

Run: python3 -m pytest
coordinator_core/session/tests/test_touch_kind_read_vs_write.py -q
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.session import scope, touch_record


@pytest.fixture()
def repo(tmp_path):
    """A session hub on disk, so nothing here reaches the real tree's live-
    session directory.

    No `git init`: `contested_by_live_peers` reads `<root>/.git/coordinator-
    sessions/` as a directory and spawns nothing, so a real repo would buy
    this module only a process per test on a box whose load norm is ~50
    concurrent sessions.
    """
    root = os.path.realpath(str(tmp_path))
    os.makedirs(os.path.join(root, ".git", "coordinator-sessions"), exist_ok=True)
    return root


def _peer_claim(repo: str, sid: str, path: str, kind) -> None:
    """Write one peer TOUCH of `kind`, and make that peer read as live."""
    sid_dir = os.path.join(repo, ".git", "coordinator-sessions", sid)
    os.makedirs(sid_dir, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sid_dir),
        session_id=sid,
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path=path,
        kind=kind,
    )


@pytest.fixture(autouse=True)
def _everyone_is_live(monkeypatch):
    """Liveness is a different axis and a different module's question; pin it
    True so a failure here can only be about the kind."""
    monkeypatch.setattr(scope.touch_record, "session_live", lambda *a, **k: True)
    monkeypatch.setattr(
        "coordinator_core.session.liveness.session_live", lambda *a, **k: True
    )


def test_a_read_hold_does_not_contest_a_peer_commit(repo):
    """The defect. A peer that only READ the file must not refuse my commit."""
    _peer_claim(repo, "peer-reader", "pkg/mod.py", touch_record.KIND_READ)

    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {}


def test_a_write_hold_still_contests(repo):
    """The half that must NOT change: this is the mechanism working."""
    _peer_claim(repo, "peer-writer", "pkg/mod.py", touch_record.KIND_WRITE)

    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {
        "pkg/mod.py": ["peer-writer"]
    }


def test_an_unknown_hold_still_contests(repo):
    """Every pre-axis line. Migration is monotonic: nothing that blocks today
    stops blocking until a writer positively says it was a read."""
    _peer_claim(repo, "peer-legacy", "pkg/mod.py", None)

    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {
        "pkg/mod.py": ["peer-legacy"]
    }


def test_the_reader_is_dropped_and_the_writer_named_from_the_same_path(repo):
    """The incident's own shape: two live holders on one file, one of each
    kind. The refusal must name the writer and ONLY the writer -- naming both
    is what sent the operator to a session that could not act."""
    _peer_claim(repo, "peer-reader", "pkg/mod.py", touch_record.KIND_READ)
    _peer_claim(repo, "peer-writer", "pkg/mod.py", touch_record.KIND_WRITE)

    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {
        "pkg/mod.py": ["peer-writer"]
    }


def test_a_read_touch_is_still_recorded(repo):
    """NEGATIVE SPEC: reads are not dropped from the record. The hash churn on
    a read is what makes interleaved-writer detection work, and it is what
    identified the true author in the filed incident. The record keeps them;
    the CONSUMER filters them."""
    _peer_claim(repo, "peer-reader", "pkg/mod.py", touch_record.KIND_READ)

    sink = touch_record.sink_path(
        os.path.join(repo, ".git", "coordinator-sessions", "peer-reader")
    )
    projection = touch_record.project_live_claims(sink, cwd=repo)
    assert "pkg/mod.py" in projection.claims
    assert projection.claims["pkg/mod.py"].kind == touch_record.KIND_READ


def test_the_blocking_rule_lives_in_exactly_one_place():
    """A consumer comparing to `KIND_READ` itself gets the unknown case wrong
    the first time it forgets about it, which is the whole failure mode. The
    rule is one function, and it is total over the three states."""
    assert touch_record.kind_blocks_a_peer_commit(touch_record.KIND_WRITE) is True
    assert touch_record.kind_blocks_a_peer_commit(None) is True
    assert touch_record.kind_blocks_a_peer_commit(touch_record.KIND_READ) is False


def test_a_kindless_line_is_byte_identical_to_what_was_written_before():
    """No schema bump, and no historical line changes meaning. An encoder that
    started writing `"kind":null` would grow every line and make a pre-axis
    record distinguishable from a new kind-less one for no gain."""
    line = touch_record.encode_line(
        session_id="s", agent_id=None, verb=touch_record.VERB_TOUCH, path="a.py"
    )
    assert b"kind" not in line
    assert touch_record.decode_line(line).kind is None


def test_an_unrecognised_kind_is_malformed_not_silently_unknown():
    """Folding an unrecognised kind into "unknown" would let a typo inherit
    unknown's conservative blocking and never be noticed."""
    with pytest.raises(ValueError):
        touch_record.encode_line(
            session_id="s",
            agent_id=None,
            verb=touch_record.VERB_TOUCH,
            path="a.py",
            kind="write",  # the label, not the stored letter
        )
    with pytest.raises(touch_record.MalformedRecordLine):
        touch_record.decode_line(
            '{"v":1,"verb":"T","ts":1.0,"sid":"s","agent":null,"path":"a.py","kind":"x"}'
        )


def test_a_release_carries_no_kind():
    """A RELEASE ends a hold rather than describing one."""
    line = touch_record.encode_line(
        session_id="s", agent_id=None, verb=touch_record.VERB_RELEASE, path="a.py"
    )
    assert touch_record.decode_line(line).kind is None


# ---------------------------------------------------------------------------
# The other half of the same row: how a hold that outlived its work ends.
# ---------------------------------------------------------------------------
#
# The kind axis above is monotonic on purpose -- a kind-less `T` keeps
# blocking, so nothing that blocks today silently stops. That is only
# defensible if its author has a route to retire it, and the route has to be
# callable on work still IN FLIGHT: the incident's stale claim sat on a file
# its holder had never written and whose bytes were someone else's,
# uncommitted. A route named for committed paths is one the careful holder
# declines, which is exactly what happened and why the claim stayed.


def _my_claim(repo: str, sid: str, path: str, kind) -> None:
    _peer_claim(repo, sid, path, kind)


def _sid_dir(repo: str, sid: str) -> str:
    return os.path.join(repo, ".git", "coordinator-sessions", sid)


def test_a_holder_releases_its_own_in_flight_claim(repo, monkeypatch):
    """No committedness term anywhere in the release condition: the path is
    uncommitted, untracked, and not even present on disk, and the release
    still lands."""
    monkeypatch.setattr(scope.core, "session_dir", lambda sid, cwd=None: _sid_dir(repo, sid))
    monkeypatch.setattr(
        scope.core, "sessions_dir", lambda cwd=None: os.path.join(repo, ".git", "coordinator-sessions")
    )
    _my_claim(repo, "mine", "pkg/mod.py", touch_record.KIND_WRITE)

    scope.release_own_path_claims("mine", ["pkg/mod.py"], cwd=repo)

    sink = touch_record.sink_path(_sid_dir(repo, "mine"))
    assert "pkg/mod.py" not in touch_record.project_live_claims(sink, cwd=repo).claims


def test_releasing_an_unknown_kind_claim_is_the_whole_corpus_migration(repo, monkeypatch):
    """Pre-axis lines are never backfilled, so the only way one stops blocking
    is that its own author retires it. This is that route, and it is the same
    one -- no separate legacy path to keep working."""
    monkeypatch.setattr(scope.core, "session_dir", lambda sid, cwd=None: _sid_dir(repo, sid))
    monkeypatch.setattr(
        scope.core, "sessions_dir", lambda cwd=None: os.path.join(repo, ".git", "coordinator-sessions")
    )
    _peer_claim(repo, "peer-legacy", "pkg/mod.py", None)
    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {
        "pkg/mod.py": ["peer-legacy"]
    }

    scope.release_own_path_claims("peer-legacy", ["pkg/mod.py"], cwd=repo)

    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {}


def test_the_post_commit_name_is_an_alias_and_adds_no_condition(repo, monkeypatch):
    """`release_committed_claims` never checked committedness -- the name did
    the misleading on its own. Pinned so a future reader restoring the check
    the name implies breaks a test instead of a peer's commit."""
    monkeypatch.setattr(scope.core, "session_dir", lambda sid, cwd=None: _sid_dir(repo, sid))
    monkeypatch.setattr(
        scope.core, "sessions_dir", lambda cwd=None: os.path.join(repo, ".git", "coordinator-sessions")
    )
    _my_claim(repo, "mine", "never-committed.py", touch_record.KIND_WRITE)

    scope.release_committed_claims("mine", ["never-committed.py"], cwd=repo)

    sink = touch_record.sink_path(_sid_dir(repo, "mine"))
    assert "never-committed.py" not in touch_record.project_live_claims(sink, cwd=repo).claims


def test_the_release_route_cannot_reach_a_peers_claim(repo, monkeypatch):
    """The reason this route can be handed to an operator at all. It takes one
    sid -- its own -- and a peer naming mine in the paths list changes
    nothing."""
    monkeypatch.setattr(scope.core, "session_dir", lambda sid, cwd=None: _sid_dir(repo, sid))
    monkeypatch.setattr(
        scope.core, "sessions_dir", lambda cwd=None: os.path.join(repo, ".git", "coordinator-sessions")
    )
    _peer_claim(repo, "peer-writer", "pkg/mod.py", touch_record.KIND_WRITE)

    scope.release_own_path_claims("mine", ["pkg/mod.py"], cwd=repo)

    assert scope.contested_by_live_peers(["pkg/mod.py"], "mine", repo) == {
        "pkg/mod.py": ["peer-writer"]
    }
