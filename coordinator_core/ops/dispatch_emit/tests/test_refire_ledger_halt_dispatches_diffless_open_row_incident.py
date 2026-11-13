"""The ee83ba incident shape: a ledger-check halt (blocking T16) whose plan has seven unlanded rows, four of them
finished and kept (C1, C3, C4, C6) and C8 open with no diff, T16's remaining work naming C8's two files.

The default refire must stage C8 and the other open rows to run and the four kept rows as kept, so the run
dispatches the open rows only. The halted run's `kept.json` rides the gate's snapshot for a row resume only
(`gate` fills `kept_sink` when `resume_rows` is set); a ledger resume dropped it, so every unlanded row,
finished ones included, was staged to run again and the refire re-ran a full pass whose executors rewrote
finished work instead of reaching the one open row. The cause is neither a stale stored script (its
fingerprint carries the plan sha) nor the stage dropping C8: the stage kept C8 and lost the kept rows.

The empty-ledger and partial-ledger incident tests missed it because their halted runs hold no `kept.json`
and they assert staged row ids and `unlanded`, never whether a staged row runs or is kept."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_UNLANDED_FIELD,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED_RUN = "warp-2026-09-23-incident-20261113T142416-ee83ba"
REFIRE_RUN = "warp-2026-09-23-incident-20261113T182500-aaaaaa"
ROWS = ("C1", "C2", "C3", "C4", "C6", "C7", "C8")
KEPT = ("C1", "C3", "C4", "C6")
C8_FILES = ["scripts/test_setup.py", "coordinator/bin/test_fan_out_dispatch.py"]
REMAINING = f"Retarget {C8_FILES[0]} stubs and {C8_FILES[1]}::test_threshold_resolution_order to the seam (C8)"


def _writes(row_id: str) -> list[str]:
    return C8_FILES if row_id == "C8" else [f"some/{row_id}.py"]


def _incident_repo(repo, *, remaining: str = REMAINING) -> None:  # noqa: F811
    """Seven open rows all staged by the halted run, C1/C3/C4/C6 kept, C8 untouched since the base commit."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  register_rows: [T16]\n  writes:\n"
        + "".join(f"    - {w}\n" for w in _writes(i))
        for i in ROWS
    )
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n")
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    for i in ROWS:
        for w in _writes(i):
            (repo / w).parent.mkdir(parents=True, exist_ok=True)
            (repo / w).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for i in KEPT:
        (repo / _writes(i)[0]).write_text("x = 2\n", encoding="utf-8")
        report = repo / _dispatch_report_path(PLAN_REL, i)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(f"DONE: {i}\n", encoding="utf-8")
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("T16",),
        blocking_items=[{"id": "T16", "remaining": [remaining], "evidence": "8 scoped failures"}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": i, "writes": _writes(i)} for i in ROWS]}), encoding="utf-8"
    )
    kept_work.write(
        record, {i: {"paths": {_writes(i)[0]: kept_work.fingerprint(repo, _writes(i)[0])}} for i in KEPT}, None
    )
    write_ledger_halt(
        ledger_halt_path(repo, PLAN_REL),
        LedgerHalt(HALTED_RUN, PLAN_REL, REL, ("T16",), remaining={"T16": {"remaining": [remaining]}}),
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL}))


def _refire(repo) -> tuple[dict, dict[str, str | None]]:  # noqa: F811
    """The default refire's reply and `{staged row id: kept_from}`."""
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    rows = read_reply(repo, reply["manifest_handle"])["rows"]
    return reply, {r["id"]: r.get("kept_from") for r in rows}


def _dispatched(staged: dict[str, str | None]) -> set[str]:
    return {i for i, kept_from in staged.items() if not kept_from}


def test_refire_dispatches_the_open_diffless_row_and_only_the_open_rows(repo):  # noqa: F811
    _incident_repo(repo)
    reply, staged = _refire(repo)
    assert reply["resume_ledger"][RESUME_LEDGER_UNLANDED_FIELD] == list(ROWS)
    assert set(staged) == set(ROWS)
    assert _dispatched(staged) == {"C2", "C7", "C8"}
    assert {i: staged[i] for i in KEPT} == {i: HALTED_RUN for i in KEPT}


def test_a_kept_row_the_remaining_work_names_is_dispatched_again(repo):  # noqa: F811
    _incident_repo(repo, remaining="C4 still fails: some/C4.py must be retargeted")
    _, staged = _refire(repo)
    assert _dispatched(staged) == {"C2", "C4", "C7", "C8"}
    assert {staged[i] for i in ("C1", "C3", "C6")} == {HALTED_RUN}


def test_a_kept_row_whose_bytes_moved_is_dispatched_again(repo):  # noqa: F811
    _incident_repo(repo)
    (repo / "some/C3.py").write_text("x = 3\n", encoding="utf-8")
    _, staged = _refire(repo)
    assert _dispatched(staged) == {"C2", "C3", "C7", "C8"}
