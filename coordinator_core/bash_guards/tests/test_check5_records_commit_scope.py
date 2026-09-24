"""Check 5's commit-scope-events record must describe THIS commit's actual
scope, not the whole staged index, when the commit names an explicit
pathspec that narrows below the full staged set.

Spec: docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md (T40)
Item 40 — `_pathspec_scoped` was recorded, but the event's `staged` field
still carried the whole-index `staged` list rather than the pathspec-narrowed
`commit_scope`, so a reader could not tell WHICH paths a scoped commit
actually touched. Record-only fix per the row's negative spec: this test
must never assert a verdict/branching change, only the recorded value.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import commit_scope_events
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SID = "sess-check5-scope"


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **no_console_creationflags())
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True,
        **no_console_creationflags(),
    )
    return repo


def _session_dir(repo, sid=SID):
    return repo / ".git" / "coordinator-sessions" / sid


def _init_session(repo, sid=SID):
    """A session dir whose ``started_at`` is an hour in the FUTURE, so
    compute_scope's mtime fallback does not auto-adopt a freshly-staged file
    into this session's own scope."""
    from coordinator_core.session import core

    assert core.init(sid, cwd=str(repo))
    sdir = _session_dir(repo, sid)
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )
    return sdir


def _stage(repo, name, content="x"):
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _verdict(cmd, repo):
    out = dispatch_checks.check_validate_commit(cmd, SID, str(repo))
    if out is None:
        return "none"
    decision = out["hookSpecificOutput"]["permissionDecision"]
    return "deny" if decision == "deny" else "advisory"


def _events(repo, sid=SID):
    path = commit_scope_events.events_path(_session_dir(repo, sid))
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_scoped_commit_records_the_pathspec_scope_not_the_whole_index(tmp_path):
    repo = _init_repo(tmp_path)
    _init_session(repo)
    # Two paths staged; the commit below names ONLY foreign.txt via an
    # explicit pathspec, so commit_scope narrows to ["foreign.txt"] while
    # the whole staged index still holds both paths.
    _stage(repo, "other.txt")
    _stage(repo, "foreign.txt")

    assert _verdict('git commit -m "x" -- foreign.txt', repo) == "advisory"

    records = _events(repo)
    assert len(records) == 1
    rec = records[0]
    assert rec["pathspec_scoped"] is True
    # The bug: this used to be the whole staged index (["foreign.txt",
    # "other.txt"]) even though the commit only ever touches foreign.txt.
    assert rec["staged"] == ["foreign.txt"]
    assert "other.txt" not in rec["staged"]
