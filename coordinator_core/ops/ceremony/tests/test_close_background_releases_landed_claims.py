"""The close job releases the landing session's claims on the landed paths in one in-process call."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.run_close import close_job_path
from coordinator_core.session import scope

pytestmark = [pytest.mark.spawns_process]


def _ctx(record: dict) -> close_background.JobContext:
    return close_background.JobContext(Path("."), Path("."), "run", (), None, record=record)


def test_one_release_names_the_session_and_every_landed_path(monkeypatch):
    seen: list = []
    monkeypatch.setattr(scope, "release_committed_claims_in_process_or_retain", lambda *a: seen.append(a))

    close_background._release_landed_claims(
        _ctx({"session_id": "sid", "inputs": {"landed_paths": ["a.py", "b.py"]}})
    )

    assert len(seen) == 1 and seen[0][1:] == (["a.py", "b.py"], "sid", "close_background")


def test_a_record_without_a_session_or_paths_releases_nothing(monkeypatch):
    seen: list = []
    monkeypatch.setattr(scope, "release_committed_claims_in_process_or_retain", lambda *a: seen.append(a))

    close_background._release_landed_claims(_ctx({"inputs": {"landed_paths": ["a.py"]}}))
    close_background._release_landed_claims(_ctx({"session_id": "sid", "inputs": {}}))

    assert seen == []


def test_the_landing_records_its_paths_and_the_job_releases_them(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    repo, script = driver.build_fixture(tmp_path)
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    out = land_run._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": copy.deepcopy(driver.INLINE_REVIEW)},
        repo_root=repo / ".git",
    )
    run_id = out["close"]["run_id"]
    record = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    assert "a.py" in record["inputs"]["landed_paths"]

    seen: list = []
    monkeypatch.setattr(scope, "release_committed_claims_in_process_or_retain", lambda *a: seen.append(a))
    close_background._handler({"run_id": run_id, "claimed_batons": record["claimed_batons"]}, repo_root=repo / ".git")

    mine = [c for c in seen if c[3] == "close_background"]
    assert len(mine) == 1 and "a.py" in mine[0][1] and mine[0][2] == driver.SESSION_ID
