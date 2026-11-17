"""A refire of a halted run whose kept rows sit on disk uncommitted: the kept credit, not the ledger, skips Execute.

The halted run's shape: kept.json fingerprints C0 (one path) and C1 (two of its three declared writes), the
kept bytes are on disk and uncommitted, the row resume (no resume_ledger) stages both rows `kept_from`, and the
composed Execute replays a kept row as `DONE ... kept from` with no agent. A row whose declared writes are not
all among its kept paths must not take that credit.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage, halted_run, kept_work, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_resume import _script
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL

pytestmark = pytest.mark.complete

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261117T000000-bbbbbb"
C0_WRITES = ["pkg/c0_test.py"]
C1_WRITES = ["pkg/c1.py", "pkg/c1_test.py", "pkg/c1_extra.py"]
C1_KEPT = C1_WRITES[:2]


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _row(row_id: str, disposition: str, writes: list[str]) -> str:
    listed = "".join(f"    - {w}\n" for w in writes)
    return f"- id: {row_id}\n  title: T{row_id}\n  disposition: {disposition}\n  writes:\n{listed}"


@pytest.fixture
def halted(repo):  # noqa: F811
    """A refused-halt run record: C0 and C1 kept and on disk, C1 short of its declared writes; C9 coded."""
    _put(repo, tshirt="S", route="spec-dispatch")
    body = _row("C9", "coded", ["pkg/c9.py"]) + _row("C0", "open", C0_WRITES) + _row("C1", "open", C1_WRITES)
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted="refusal", sizing_rel=REL, plan_rel=PLAN_REL)
    for rel in (*C0_WRITES, *C1_KEPT):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(b"x = 1\n")
    kept_work.write(
        record,
        {
            "C0": {"paths": {p: kept_work.fingerprint(repo, p) for p in C0_WRITES}},
            "C1": {"paths": {p: kept_work.fingerprint(repo, p) for p in C1_KEPT}},
        },
        None,
    )
    return repo


def _snapshot(repo, kept: list[str]):
    resume_snapshot.write(repo, NEW_RUN, PLAN_REL, ["C9"], ["C0", "C1"], kept=kept, kept_run=RUN)
    snap = resume_snapshot.read(repo, NEW_RUN, PLAN_REL)
    assert snap is not None and snap.kept == frozenset(kept)
    for row in ("C0", "C1"):
        report = repo / _dispatch_report_path(PLAN_REL, row)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(f"DONE: {row}\n", encoding="utf-8", newline="\n")
    return snap


def test_the_resume_is_a_row_resume_not_a_ledger_resume(halted):
    """The ledger's `unlanded` gate (`_ledgerExec`) is not in play: no resume_ledger, both rows open."""
    v = gate(halted, REL, run_id=NEW_RUN, kept_sink=[])
    assert v.halt is None and v.resume_ledger is None
    assert v.resume_rows == ["C0", "C1"]


def test_a_kept_row_missing_declared_writes_is_not_credited_kept(halted):
    """Both sides: C0 (every declared write kept and on disk) is kept; C1 (c1_extra.py never written) is not."""
    sink: list = []
    gate(halted, REL, run_id=NEW_RUN, kept_sink=sink)
    assert sink == [(["C0"], RUN)], sink


def test_stage_marks_kept_from_only_for_rows_whose_declared_writes_all_hold(halted):
    snap = _snapshot(halted, ["C0", "C1"])
    c0 = ask_stage._kept_row_holds(halted, snap, "C0", _dispatch_report_path(PLAN_REL, "C0"))
    c1 = ask_stage._kept_row_holds(halted, snap, "C1", _dispatch_report_path(PLAN_REL, "C1"))
    assert (c0, c1) == (True, False)


def test_composed_execute_replays_a_kept_from_row_without_an_agent(monkeypatch):
    """The only Execute path that credits a row done with no agent: a staged `kept_from` row."""
    script = _script(monkeypatch)
    assert "r.kept_from ? _keptRun(r) :" in script
    assert "return 'DONE: ' + r.id + ' kept from ' + r.kept_from + ' (report on disk)';" in script
