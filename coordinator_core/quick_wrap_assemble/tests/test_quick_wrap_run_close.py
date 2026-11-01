"""quick-wrap's brief reports gates.run_close and stamps discharged_by on d3/d4/d5."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult
from coordinator_core.quick_wrap_assemble import (
    _directives,
    _run_close_gate,
    _session_birth_floor,
    brief,
)
from coordinator_core.win_portability import no_console_creationflags

SID = "11111111-2222-3333-4444-555555555555"
RUN_ID = "run-1"


def _write_record(root: Path, run_id: str, session_id, settled) -> None:
    run_close.write_close_job(
        root, run_id, (), datetime.now().astimezone() + timedelta(minutes=10),
        session_id=session_id, settled=settled,
    )


def _by_id(directives):
    return {d["id"]: d for d in directives}


def test_drain_and_refresh_run_discharge_d3_d4_d5(tmp_path):
    _write_record(
        tmp_path, RUN_ID, SID,
        {"baton-drain": LegResult("ran"), "orientation-refresh": LegResult("ran")},
    )
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    assert [d["discharged_by"] for d in directives] == [RUN_ID] * 3
    assert gate["applies"] is True
    assert gate["run_ids"] == [RUN_ID]
    assert gate["discharged"] == {"d3": RUN_ID, "d4": RUN_ID, "d5": RUN_ID}
    assert gate["outstanding"] == []
    assert gate["verdict"] == "applicable"
    assert gate["close_id"] is None


def test_only_the_leg_that_ran_discharges(tmp_path):
    _write_record(tmp_path, RUN_ID, SID, {"baton-drain": LegResult("ran")})
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    by_id = _by_id(directives)
    assert by_id["d3"]["discharged_by"] is None
    assert by_id["d4"]["discharged_by"] == RUN_ID
    assert by_id["d5"]["discharged_by"] == RUN_ID
    assert gate["outstanding"] == ["d3"]


def test_failed_leg_does_not_discharge(tmp_path):
    _write_record(
        tmp_path, RUN_ID, SID,
        {"baton-drain": LegResult("failed", "x"), "orientation-refresh": LegResult("ran")},
    )
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    assert gate["outstanding"] == ["d4", "d5"]


def test_session_with_no_record_applies_false(tmp_path):
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    assert gate["applies"] is False
    assert gate["run_ids"] == [] and gate["discharged"] == {} and gate["outstanding"] == []
    assert gate["verdict"] == "not-applicable"
    assert gate["close_id"] is None
    assert all(d["discharged_by"] is None for d in directives)


def test_another_sessions_record_and_a_sessionless_record_discharge_nothing(tmp_path):
    settled = {"baton-drain": LegResult("ran"), "orientation-refresh": LegResult("ran")}
    _write_record(tmp_path, "other", "99999999-2222-3333-4444-555555555555", settled)
    _write_record(tmp_path, "legacy", None, settled)
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    assert gate["applies"] is False
    assert all(d["discharged_by"] is None for d in directives)


def test_run_less_close_names_close_id_and_discharges_no_directive(tmp_path, monkeypatch):
    entry = "archive/completed/202610/20261013-adhoc-555555.md"
    monkeypatch.setattr(
        run_close, "run_less_close",
        lambda root, sid: run_close.RunClose(
            run_id=entry, completion_entry=entry,
            legs={"completion-entry": LegResult("ran", entry)},
        ),
    )
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    assert gate["close_id"] == entry
    assert gate["applies"] is True
    assert gate["outstanding"] == ["d3", "d4", "d5"]
    assert all(d["discharged_by"] is None for d in directives)


def test_reader_failure_is_indeterminate_and_never_raises(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(run_close, "session_run_closes", boom)
    directives = _directives()
    gate = _run_close_gate(tmp_path, tmp_path / ".git", SID, directives)
    assert gate["verdict"] == "indeterminate"
    assert "disk gone" in gate["reason"]
    assert all(d["discharged_by"] is None for d in directives)


def test_birth_floor_is_none_without_a_claim_dir(tmp_path):
    assert _session_birth_floor(tmp_path, SID) is None


def test_birth_floor_never_uses_mtime(tmp_path):
    claim = tmp_path / "coordinator-sessions" / SID
    claim.mkdir(parents=True)
    before = _session_birth_floor(tmp_path, SID)
    future = time.time() + 86400
    os.utime(claim, (future, future))
    assert _session_birth_floor(tmp_path, SID) == before


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def test_d5_carries_the_drain_counters_and_is_satisfied_only_by_a_clean_drain():
    clean = _by_id(_directives({"archived": 3, "candidates": 3, "skipped": 0, "failed": 0, "scan_skipped": 1}))["d5"]
    assert clean["drain"]["archived"] == 3
    assert clean["already_satisfied"] is True
    assert clean["cli"] == "sweep-terminal-sizings"

    errored = _by_id(_directives({"error": "RuntimeError: boom"}))["d5"]
    assert errored["already_satisfied"] is False
    failed = _by_id(_directives({"archived": 1, "failed": 2}))["d5"]
    assert failed["already_satisfied"] is False

    bare = _by_id(_directives())["d5"]
    assert "drain" not in bare and bare["already_satisfied"] is False


def _seeded_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_brief_runs_the_drain_in_process_and_reports_its_counters(tmp_path, monkeypatch):
    from coordinator_core.ops.fleet import terminal_sizing_drain

    repo = _seeded_repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    seen: list = []

    def fake_drain(root, *, dry_run=False):
        seen.append((Path(root), dry_run))
        return {"acted": [{"id": "x"}, {"id": "y"}], "candidates": [{}, {}], "skipped": [], "failed": [],
                "scan_skipped": [{"id": "z"}]}

    monkeypatch.setattr(terminal_sizing_drain, "drain", fake_drain)
    envelope = brief(repo, commit=True)

    d5 = {d["id"]: d for d in envelope["directives"]}["d5"]
    assert seen == [(repo, False)]
    assert d5["drain"] == {"archived": 2, "candidates": 2, "skipped": 0, "failed": 0, "scan_skipped": 1}


def test_a_drain_fault_is_advisory_and_never_fails_the_close(tmp_path, monkeypatch):
    from coordinator_core.ops.fleet import terminal_sizing_drain

    repo = _seeded_repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)

    def boom(root, *, dry_run=False):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(terminal_sizing_drain, "drain", boom)
    envelope = brief(repo, commit=True)

    d5 = {d["id"]: d for d in envelope["directives"]}["d5"]
    assert "drain exploded" in d5["drain"]["error"]
    assert d5["already_satisfied"] is False


def test_brief_without_commit_does_not_drain(tmp_path, monkeypatch):
    from coordinator_core.ops.fleet import terminal_sizing_drain

    repo = _seeded_repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.setattr(
        terminal_sizing_drain, "drain",
        lambda *a, **k: pytest.fail("a no-commit brief must not mutate state/sizings"),
    )
    envelope = brief(repo, commit=False)

    assert "drain" not in {d["id"]: d for d in envelope["directives"]}["d5"]


@pytest.mark.spawns_process
def test_brief_carries_gate_and_discharged_by_and_leaves_close_ledger_shape(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    _write_record(
        repo, RUN_ID, SID,
        {"baton-drain": LegResult("ran"), "orientation-refresh": LegResult("ran")},
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)

    envelope = brief(repo)

    assert envelope["gates"]["run_close"]["discharged"] == {
        "d3": RUN_ID, "d4": RUN_ID, "d5": RUN_ID,
    }
    directives = {d["id"]: d for d in envelope["directives"]}
    assert {i: directives[i]["discharged_by"] for i in ("d3", "d4", "d5")} == {
        "d3": RUN_ID, "d4": RUN_ID, "d5": RUN_ID,
    }
    for entry in envelope["decisions"]["close_ledger"]:
        assert set(entry) <= {"item", "status", "discharged_by", "evidence"}
