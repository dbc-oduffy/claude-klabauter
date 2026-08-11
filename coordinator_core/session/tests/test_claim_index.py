"""Tests for coordinator_core.session.claim_index.

Plan: docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md,
chunk C1. Every fixture here is a synthetic session/agent dir tree built
under ``tmp_path`` — no process is spawned, and the real
``.git/coordinator-sessions/`` is never touched (every call passes
``sessions_dir=str(tmp_path)`` explicitly).
"""

import os

import pytest

from coordinator_core.session import claim_index


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _session_touched(base, sid, lines):
    _write(os.path.join(str(base), sid, "touched.txt"), "\n".join(lines) + "\n")


def _agent_touched(base, agent_id, owner_sid, lines):
    agent_dir = os.path.join(str(base), ".agents", agent_id)
    _write(os.path.join(agent_dir, "touched.txt"), "\n".join(lines) + "\n")
    _write(os.path.join(agent_dir, "em-session-id.txt"), owner_sid + "\n")


def _touch_line(verb, path, when="2026-08-08T10:00:00.000000Z"):
    return f"{verb} {when} {path}"


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_claimed_path(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo/bar.py")])

    result = claim_index.lookup(["foo/bar.py"], sessions_dir=base)

    assert result == {"foo/bar.py": ["sess-a"]}
    # Unconditional rebuild: a second, independent lookup() call re-derives
    # the same answer from the substrate, not from any persisted cache.
    result_again = claim_index.lookup(["foo/bar.py"], sessions_dir=base)
    assert result_again == {"foo/bar.py": ["sess-a"]}


def test_round_trip_unclaimed_path(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo/bar.py")])

    result = claim_index.lookup(["never/touched.py"], sessions_dir=base)

    assert result == {"never/touched.py": []}


def test_rebuild_returns_in_memory_state_only(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is True
    assert state.claims == {"foo.py": ["sess-a"]}
    # No persisted index file is written anywhere under the sessions dir.
    for _root, dirs, _files in os.walk(base):
        assert ".index" not in dirs


# ---------------------------------------------------------------------------
# Torn-line tolerance
# ---------------------------------------------------------------------------


def test_torn_trailing_line_in_touched_txt_is_discarded(tmp_path):
    base = str(tmp_path)
    # A complete claim line, then a torn (no trailing newline) fragment
    # simulating a reader that caught a concurrent writer mid-append.
    torn = (
        _touch_line("T", "complete.py")
        + "\n"
        + "T 2026-08-08T10:00:01.000000Z partial-wr"
    )
    _write(os.path.join(base, "sess-a", "touched.txt"), torn)

    result = claim_index.lookup(["complete.py", "partial-wr"], sessions_dir=base)

    assert result["complete.py"] == ["sess-a"]
    assert result["partial-wr"] == []  # torn line never resolved -> not claimed


# ---------------------------------------------------------------------------
# unconditional rebuild -- lookup() must see an append to an EXISTING
# claimant's touched.txt, not just a brand-new session dir landing
# ---------------------------------------------------------------------------


def test_lookup_sees_second_claim_appended_to_existing_session(tmp_path):
    """C1b regression: appending a 2nd claim to an ALREADY-EXISTING
    session's touched.txt (what ``scope.py::touch`` does on every call
    after a session's first) changes that file's mtime, not any
    directory's. Under the deleted mtime-staleness probe this test would
    FAIL, because neither the top-level sessions-dir nor ``sess-a/``'s own
    mtime moves on an in-place append -- lookup() would keep serving the
    cached pre-append snapshot and report ``other.py`` unclaimed. Under
    unconditional rebuild it passes, because every lookup() call re-walks
    the substrate regardless of any mtime signal.
    """
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    first = claim_index.lookup(["other.py"], sessions_dir=base)
    assert first == {"other.py": []}

    # Organic append: same session dir, same file, no os.utime anywhere --
    # exactly what scope.py::touch does for a 2nd-and-later claim.
    touched_path = os.path.join(base, "sess-a", "touched.txt")
    with open(touched_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(_touch_line("T", "other.py") + "\n")

    second = claim_index.lookup(["other.py"], sessions_dir=base)
    assert second == {"other.py": ["sess-a"]}


def test_lookup_unresolvable_sessions_dir_is_unanswerable(tmp_path):
    result = claim_index.lookup(["foo.py"], sessions_dir="")
    assert result == {"foo.py": [claim_index.UNANSWERABLE]}


def test_lookup_missing_sessions_dir_on_disk_is_unclaimed_not_unanswerable(tmp_path):
    missing = os.path.join(str(tmp_path), "does-not-exist-yet")
    result = claim_index.lookup(["foo.py"], sessions_dir=missing)
    assert result == {"foo.py": []}


# ---------------------------------------------------------------------------
# Review: coordinator:code-reviewer P1 -- an I/O error reading a claim
# source (as opposed to that source genuinely not existing) must surface as
# UNANSWERABLE, never silently collapse to "unclaimed" -- that is the one
# answer that authorizes a write.
# ---------------------------------------------------------------------------


def test_lookup_permission_error_scanning_sessions_dir_is_unanswerable(
    tmp_path, monkeypatch
):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.abspath(str(path)) == os.path.abspath(base):
            raise PermissionError("simulated I/O error")
        return real_scandir(path)

    monkeypatch.setattr(claim_index.os, "scandir", fake_scandir)

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result == {"foo.py": [claim_index.UNANSWERABLE]}


def test_lookup_permission_error_scanning_agents_subdir_is_unanswerable(
    tmp_path, monkeypatch
):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])
    agents_base = os.path.join(base, claim_index._AGENTS_SUBDIR)

    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.abspath(str(path)) == os.path.abspath(agents_base):
            raise PermissionError("simulated I/O error")
        return real_scandir(path)

    monkeypatch.setattr(claim_index.os, "scandir", fake_scandir)

    # A path resolvable from the session-dir scan still resolves fine; the
    # incompleteness only bites paths this walk never actually reached.
    result = claim_index.lookup(["foo.py", "never/touched.py"], sessions_dir=base)

    assert result["foo.py"] == ["sess-a"]
    assert result["never/touched.py"] == [claim_index.UNANSWERABLE]


def test_lookup_permission_error_reading_agent_backpointer_is_unanswerable(
    tmp_path, monkeypatch
):
    base = str(tmp_path)
    _agent_touched(base, "agent-1", "sess-owner", [_touch_line("T", "x.py")])
    backptr = os.path.join(base, ".agents", "agent-1", "em-session-id.txt")

    real_open = open

    def fake_open(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(backptr):
            raise PermissionError("simulated I/O error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    result = claim_index.lookup(["x.py"], sessions_dir=base)

    assert result == {"x.py": [claim_index.UNANSWERABLE]}


# ---------------------------------------------------------------------------
# Wall-clock cap -- the module's other surviving degradation route
# ---------------------------------------------------------------------------


def test_rebuild_cap_exceeded_mid_walk_marks_incomplete(tmp_path, monkeypatch):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    # Force the very first cap check inside the walk to already be past
    # deadline, deterministically, without depending on real wall-clock
    # timing (a real 500ms sleep would be a needless fixture cost).
    monkeypatch.setattr(claim_index, "REBUILD_WALL_CLOCK_CAP_SECS", -1.0)

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is False


def test_lookup_cap_exceeded_resolves_unanswerable_not_unclaimed(tmp_path, monkeypatch):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    monkeypatch.setattr(claim_index, "REBUILD_WALL_CLOCK_CAP_SECS", -1.0)

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result == {"foo.py": [claim_index.UNANSWERABLE]}


# ---------------------------------------------------------------------------
# Both agent-dir and session-dir are read
# ---------------------------------------------------------------------------


def test_agent_dir_claim_attributed_to_owner_session(tmp_path):
    base = str(tmp_path)
    _agent_touched(
        base, "agent-123", "sess-owner", [_touch_line("T", "engine/core.py")]
    )

    result = claim_index.lookup(["engine/core.py"], sessions_dir=base)

    assert result == {"engine/core.py": ["sess-owner"]}


def test_session_dir_and_agent_dir_claims_both_read_in_one_rebuild(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "session_owned.py")])
    _agent_touched(base, "agent-1", "sess-b", [_touch_line("T", "agent_owned.py")])

    result = claim_index.lookup(
        ["session_owned.py", "agent_owned.py"], sessions_dir=base
    )

    assert result == {
        "session_owned.py": ["sess-a"],
        "agent_owned.py": ["sess-b"],
    }


def test_agent_dir_with_no_backpointer_contributes_no_claims(tmp_path):
    base = str(tmp_path)
    agent_dir = os.path.join(base, ".agents", "orphan-agent")
    _write(
        os.path.join(agent_dir, "touched.txt"), _touch_line("T", "orphan.py") + "\n"
    )
    # No em-session-id.txt written -- unresolvable owner.

    result = claim_index.lookup(["orphan.py"], sessions_dir=base)

    assert result == {"orphan.py": []}


# ---------------------------------------------------------------------------
# Released-then-reclaimed round trip
# ---------------------------------------------------------------------------


def test_claim_then_release_resolves_unclaimed(tmp_path):
    base = str(tmp_path)
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "doc.md", when="2026-08-08T10:00:00.000000Z"),
            _touch_line("R", "doc.md", when="2026-08-08T10:05:00.000000Z"),
        ],
    )

    result = claim_index.lookup(["doc.md"], sessions_dir=base)

    assert result == {"doc.md": []}


def test_claim_release_reclaim_resolves_to_reclaimant(tmp_path):
    base = str(tmp_path)
    # sess-a claims then releases doc.md; sess-b claims it afterward. The
    # negative spec this guards: a T-only reader would still see sess-a's
    # T and wrongly report it claimed by the releasing session.
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "doc.md", when="2026-08-08T10:00:00.000000Z"),
            _touch_line("R", "doc.md", when="2026-08-08T10:05:00.000000Z"),
        ],
    )
    _session_touched(
        base,
        "sess-b",
        [_touch_line("T", "doc.md", when="2026-08-08T10:06:00.000000Z")],
    )

    result = claim_index.lookup(["doc.md"], sessions_dir=base)

    assert result == {"doc.md": ["sess-b"]}


def test_same_session_reclaims_after_releasing(tmp_path):
    base = str(tmp_path)
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "doc.md", when="2026-08-08T10:00:00.000000Z"),
            _touch_line("R", "doc.md", when="2026-08-08T10:05:00.000000Z"),
            _touch_line("T", "doc.md", when="2026-08-08T10:06:00.000000Z"),
        ],
    )

    result = claim_index.lookup(["doc.md"], sessions_dir=base)

    assert result == {"doc.md": ["sess-a"]}


# ---------------------------------------------------------------------------
# R1 -- a backslashed caller pathspec must not read as unclaimed. Keys
# parsed from touched.txt are POSIX-separated (scope.py's write side); the
# production caller (_check_claim_conflicts in scoped_git_commit.py) may
# pass a backslashed relative pathspec on Windows, this repo's first-class
# platform. A raw string-equality miss there authorizes a write against a
# live claim purely on separator dialect.
# ---------------------------------------------------------------------------


def test_lookup_backslashed_input_resolves_claimed_posix_key(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a/b/c.py")])

    result = claim_index.lookup(["a\\b\\c.py"], sessions_dir=base)

    assert result == {"a\\b\\c.py": ["sess-a"]}


def test_lookup_forward_slashed_input_resolves_backslashed_key(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a\\b\\c.py")])

    result = claim_index.lookup(["a/b/c.py"], sessions_dir=base)

    assert result == {"a/b/c.py": ["sess-a"]}


def test_lookup_mixed_separator_input_resolves_claimed(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a/b/c.py")])

    result = claim_index.lookup(["a\\b/c.py"], sessions_dir=base)

    assert result == {"a\\b/c.py": ["sess-a"]}


def test_lookup_dot_dot_normalizing_input_resolves_claimed(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a/b/c.py")])

    result = claim_index.lookup(["a/x/../b/./c.py"], sessions_dir=base)

    assert result == {"a/x/../b/./c.py": ["sess-a"]}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
