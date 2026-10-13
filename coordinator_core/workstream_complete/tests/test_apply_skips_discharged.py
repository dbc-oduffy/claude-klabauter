"""apply never dispatches a directive carrying `discharged_by`, keeps its token slot, and
never re-ships a run-shipped baton."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.ops.dispatch_emit.run_close import close_job_path
from coordinator_core.workstream_complete import apply as wsc_apply

ENTRY = "archive/completed/2026-10-13/entry-abc123.md"


def _d(did: str, cli: str, args: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": did, "cli": cli, "args": args or [], **extra}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake(directive: dict[str, Any], *, args: Any = None) -> dict[str, Any]:
        seen.append({"id": directive["id"], "args": list(args if args is not None else directive["args"])})
        return {"id": directive["id"], "cli": directive["cli"], "args": list(args or []),
                "exit_code": 0, "stdout": "", "stderr": "", "exit_class": "returned"}

    monkeypatch.setattr(wsc_apply, "_dispatch_directive", fake)
    monkeypatch.setattr(wsc_apply, "_emit_progress", lambda message: None)
    return seen


def _run(directives: list[dict[str, Any]], root: Path) -> tuple[int, dict[str, Any]]:
    return wsc_apply._execute_directives(directives, [], {}, repo_root=root)


def test_discharged_directive_is_never_dispatched(calls, tmp_path):
    code, report = _run(
        [
            _d("d-structural-index-refresh", "structural-index-refresh", discharged_by="run-1"),
            _d("d-ceremony-reindex", "scip-rebuild-at-ceremony", discharged_by=None),
        ],
        tmp_path,
    )
    assert [c["id"] for c in calls] == ["d-ceremony-reindex"]
    assert report["discharged"] == {"d-structural-index-refresh": "run-1"}
    assert "d-structural-index-refresh" not in report["landed"]
    assert code == int(wsc_apply.WorkstreamApplyExitCode.SUCCESS)


def test_run_less_entry_path_reaches_dependant_token(calls, tmp_path):
    _, report = _run(
        [
            _d("d-complete-entry", "coordinator-complete-entry", discharged_by=ENTRY),
            _d("d-fold", "coordinator-fold-execution-record", ["{d-complete-entry.entry_path}"]),
        ],
        tmp_path,
    )
    assert calls == [{"id": "d-fold", "args": [ENTRY]}]
    assert report["completion_entry_path"] == ENTRY


def test_plan_run_entry_path_is_read_from_the_close_job_record(calls, tmp_path):
    record = tmp_path / close_job_path("run-9")
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"run_id": "run-9", "legs": {}, "completion_entry": ENTRY}), encoding="utf-8")
    _, report = _run(
        [_d("d-complete-entry", "coordinator-complete-entry", discharged_by="run-9")], tmp_path
    )
    assert calls == []
    assert report["completion_entry_path"] == ENTRY


def test_discharged_entry_with_no_resolvable_path_fails_a_dependant_loud(calls, tmp_path):
    code, report = _run(
        [
            _d("d-complete-entry", "coordinator-complete-entry", discharged_by="run-missing"),
            _d("d-fold", "coordinator-fold-execution-record", ["{d-complete-entry.entry_path}"]),
        ],
        tmp_path,
    )
    assert calls == []
    assert [f["id"] for f in report["failed"]] == ["d-fold"]
    assert code != int(wsc_apply.WorkstreamApplyExitCode.SUCCESS)


def test_landed_token_resolves_through_a_discharged_producer(calls, tmp_path):
    _run(
        [
            _d("d-a", "structural-index-refresh", discharged_by="run-1"),
            _d("d-b", "scip-rebuild-at-ceremony", ["{d-a.landed}", "--x"]),
        ],
        tmp_path,
    )
    assert calls == [{"id": "d-b", "args": ["--x"]}]


def test_discharged_directive_with_unadmitted_cli_does_not_refuse_the_run(calls, tmp_path):
    code, report = _run(
        [_d("d-ghost", "not-in-manifest", discharged_by="run-1"), _d("d-b", "structural-index-refresh")],
        tmp_path,
    )
    assert [c["id"] for c in calls] == ["d-b"]
    assert code == int(wsc_apply.WorkstreamApplyExitCode.SUCCESS)


def test_discharged_post_close_directive_is_not_dispatched(calls):
    results = wsc_apply._run_post_close_directives(
        [
            _d("d-baton-chain-closure", "baton-chain-closure", discharged_by="run-1"),
            _d("d-ceremony-reindex", "scip-rebuild-at-ceremony"),
        ]
    )
    assert [c["id"] for c in calls] == ["d-ceremony-reindex"]
    assert results[0] == {"id": "d-baton-chain-closure", "discharged_by": "run-1"}


def test_run_shipped_baton_is_absent_from_the_tail_dispositions():
    shipped = {"disposition": "shipped", "shipped_in": "abc"}
    decisions = {
        "subject": "s",
        "handoff_dispositions": {"a.md": shipped, "b.md": shipped},
    }
    filtered = wsc_apply._undischarged_ship_dispositions(
        decisions, {"d-ship-consumed-handoff:a.md": "run-1", "d-sweep-terminal-handoffs": "run-1"}
    )
    assert set(filtered["handoff_dispositions"]) == {"b.md"}
    assert set(decisions["handoff_dispositions"]) == {"a.md", "b.md"}
    assert wsc_apply._undischarged_ship_dispositions(decisions, {}) is decisions
