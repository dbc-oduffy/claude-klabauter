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


# ---------------------------------------------------------------------------
# "Unknown" has to keep meaning "predates the axis".
# ---------------------------------------------------------------------------
#
# The whole migration argument rests on that sentence: an unknown-kind line
# blocks because it cannot be asked, and the corpus drains as those lines'
# authors release them or die. It was false the day it was written. Five
# live writers -- `scope.touch` (and through it every handler's declared
# writes and `touch_written_path`), both arms of `claims.self_claim`, and
# `js_bridge_cli`'s `claim-path` -- still wrote kind-less `T`s, so the
# unknown population was being refilled by current code and would never
# drain. `who-claims-path` rendered a provable write as "unknown-kind".
#
# The gate is structural rather than a list of the five, because the five
# were found by grepping once and a sixth writer is one new call site away.

import ast
import pathlib

_PKG_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Writers that emit a TOUCH and deliberately state no kind. Each is named,
#: never pattern-matched: an entry here is a claim that a machine guessing
#: this line's kind would be wrong.
_KINDLESS_BY_DESIGN = frozenset({
    # Rewrites historical lines into the current record. Stamping them would
    # be the backfill this axis's negative spec forbids.
    "ops/session/legacy_touch_corpus_migrate.py",
})

_TOUCH_WRITERS = {"append_event", "append_touch_claims", "touch"}
_RELEASE_VERBS = {"VERB_RELEASE", "R"}


def _callee(node: ast.Call):
    fn = node.func
    if isinstance(fn, ast.Attribute):
        owner = fn.value
        owner_name = (
            owner.id if isinstance(owner, ast.Name)
            else owner.attr if isinstance(owner, ast.Attribute)
            else None
        )
        return owner_name, fn.attr
    if isinstance(fn, ast.Name):
        return None, fn.id
    return None, None


def _is_release(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg != "verb":
            continue
        v = kw.value
        if isinstance(v, ast.Constant) and v.value in _RELEASE_VERBS:
            return True
        if isinstance(v, ast.Attribute) and v.attr in _RELEASE_VERBS:
            return True
    return False


def _is_touch_record_write(owner, name, rel: str) -> bool:
    if name not in _TOUCH_WRITERS:
        return False
    if name == "touch":
        # `scope.touch` only -- `Path.touch()` is everywhere and unrelated.
        return owner in {"scope", "_scope", "session_scope"} or (
            owner is None and rel == "session/scope.py"
        )
    if name == "append_event":
        # `tracker_store.append_event` is a different ledger entirely.
        return owner in {"touch_record", "_tr", None} and (
            owner is not None or rel == "session/touch_record.py"
        )
    return True


def _unstamped_touch_writes():
    offenders = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if "/tests/" in f"/{rel}" or path.name.startswith("test_"):
            continue
        if rel in _KINDLESS_BY_DESIGN:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            owner, name = _callee(node)
            if not _is_touch_record_write(owner, name, rel):
                continue
            if _is_release(node):
                continue
            if any(kw.arg == "kind" for kw in node.keywords):
                continue
            offenders.append(f"{rel}:{node.lineno} {owner or ''}.{name}".replace(" .", " "))
    return offenders


def test_every_live_touch_writer_states_a_kind():
    """A new writer that omits `kind` refills the unknown population, which
    then never drains -- and unknown blocks, so the too-strict guard this
    axis was built to fix comes back one call site at a time."""
    assert _unstamped_touch_writes() == []


def test_the_gate_can_see_a_kindless_writer(tmp_path, monkeypatch):
    """Guards the gate. A structural check that matches nothing passes for
    free, which is the failure shape this module has already met once."""
    src = (
        "from coordinator_core.session import touch_record\n"
        "def f(sink):\n"
        "    touch_record.append_event(sink, session_id='s', agent_id=None,\n"
        "                              verb=touch_record.VERB_TOUCH, path='a')\n"
        "    touch_record.append_event(sink, session_id='s', agent_id=None,\n"
        "                              verb=touch_record.VERB_RELEASE, path='a')\n"
    )
    (tmp_path / "writer.py").write_text(src, encoding="utf-8")
    monkeypatch.setattr(
        __import__(__name__, fromlist=["_PKG_ROOT"]), "_PKG_ROOT", tmp_path
    )
    assert _unstamped_touch_writes() == ["writer.py:3 touch_record.append_event"]


def test_an_in_process_write_claim_lands_as_a_write(repo, monkeypatch):
    """End to end through the busiest of the five: every handler's declared
    writes and every in-process writer reach the record via this helper."""
    monkeypatch.setattr(scope.core, "session_dir", lambda sid, cwd=None: _sid_dir(repo, sid))
    os.makedirs(_sid_dir(repo, "mine"), exist_ok=True)
    monkeypatch.setattr(scope.core, "ensure_session", lambda *a, **k: _sid_dir(repo, "mine"), raising=False)

    scope.touch_written_path("mine", "pkg/mod.py", repo)

    sink = touch_record.sink_path(_sid_dir(repo, "mine"))
    claims = touch_record.project_live_claims(sink, cwd=repo).claims
    assert claims["pkg/mod.py"].kind == touch_record.KIND_WRITE
