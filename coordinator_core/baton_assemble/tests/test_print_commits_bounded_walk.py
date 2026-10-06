"""`_print_commits_into_baton` must never run an unbounded Session-Id walk.

The unbounded `git log --grep` scanned the whole history (~300ms process time
at 40k commits) to build an advisory list that cannot predate the session.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import baton_assemble as ba


def _wire(monkeypatch, record, calls):
    monkeypatch.setattr(ba, "_resolve_current_session_id", lambda: "sid-1")
    monkeypatch.setattr(ba, "read_baton", lambda sid, cwd=None: record)

    def _fake_commits(root, sid, commit_range=None, **kw):
        calls["walk"] = (commit_range, kw)
        return [{"sha": "abc"}]

    monkeypatch.setattr(ba, "resolve_session_commits", _fake_commits)
    monkeypatch.setattr(
        ba, "merge_baton", lambda sid, cwd=None, **kw: calls.setdefault("merge", kw)
    )


def test_walk_is_bounded_by_baton_created_at_and_sha_only(monkeypatch, tmp_path: Path):
    calls: dict = {}
    _wire(monkeypatch, {"created_at": "2026-10-06T10:06:55Z"}, calls)
    ba._print_commits_into_baton(tmp_path)
    assert calls["walk"] == ("--since=2026-10-06T10:06:55Z", {"sha_only": True})
    assert calls["merge"] == {"commits": ["abc"]}


def test_no_created_at_means_no_walk(monkeypatch, tmp_path: Path):
    calls: dict = {}
    _wire(monkeypatch, {"created_at": None}, calls)
    ba._print_commits_into_baton(tmp_path)
    assert calls == {}
