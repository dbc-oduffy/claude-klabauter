"""A row whose recorded return is not DONE is never kept: the default refire re-dispatches it.

Both sides: a DONE row with its writes present stays kept; a PARTIAL row does not, whether its kept.json
entry carries every declared write or none.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, halted_record_ledger, halted_run, kept_work, row_returns
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261117T000000-bbbbbb"
C0_WRITES = ["pkg/c0.py"]
C1_WRITES = ["pkg/c1.py", "pkg/c1_test.py"]
PARTIAL = "PARTIAL: C1\ngate-blocker: needs-dependent: C0b live test waits on it"


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _row(row_id: str, writes: list[str], disposition: str = "open") -> str:
    listed = "".join(f"    - {w}\n" for w in writes)
    return f"- id: {row_id}\n  title: T{row_id}\n  disposition: {disposition}\n  writes:\n{listed}"


@pytest.fixture
def halted(repo):  # noqa: F811
    """A refused-halt run record: C0 (DONE) and C1 both keep every declared write, on disk; C9 is coded."""
    _put(repo, tshirt="S", route="spec-dispatch")
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    body = _row("C9", ["pkg/c9.py"], "coded") + _row("C0", C0_WRITES) + _row("C1", C1_WRITES)
    plan.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted="refusal", sizing_rel=REL, plan_rel=PLAN_REL)
    for rel in (*C0_WRITES, *C1_WRITES):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(b"x = 1\n")
    kept_work.write(
        record,
        {
            "C0": {"paths": {p: kept_work.fingerprint(repo, p) for p in C0_WRITES}},
            "C1": {"paths": {p: kept_work.fingerprint(repo, p) for p in C1_WRITES}},
        },
        None,
    )
    return repo, record


def test_a_done_row_with_its_writes_stays_kept(halted):
    repo, record = halted
    row_returns.write(record, {"C0": "DONE: C0", "C1": "DONE: C1"})
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert sink == [(["C0", "C1"], RUN)] and v.resume_rows == ["C0", "C1"]


def test_default_refire_redispatches_a_partial_row_holding_every_declared_write(halted):
    repo, record = halted
    row_returns.write(record, {"C0": "DONE: C0", "C1": PARTIAL})
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert sink == [(["C0"], RUN)], sink
    assert v.resume_rows == ["C0", "C1"]


def test_a_writes_empty_partial_entry_is_not_kept(halted):
    repo, record = halted
    returns = {"C0": "DONE: C0", "C1": PARTIAL}
    row_returns.write(record, returns)
    entry = {"paths": {}, "return": row_returns.digest(PARTIAL)}
    assert not kept_work.still_kept(repo, entry, [], "C1", record_dir=record)
    done = {"paths": {}, "return": row_returns.digest("DONE: C0")}
    assert kept_work.still_kept(repo, done, [], "C0", record_dir=record)


def test_a_checkpoint_carrying_none_of_the_declared_writes_is_not_kept(halted):
    repo, record = halted
    row_returns.write(record, {"C0": "DONE: C0", "C1": "DONE: C1"})
    other = {"paths": {"pkg/other.py": kept_work.fingerprint(repo, "pkg/c0.py")}}
    (repo / "pkg/other.py").write_bytes(b"x = 1\n")
    assert not kept_work.still_kept(repo, other, [], "C1", record_dir=record)


def test_record_kept_leaves_a_partial_row_out_of_kept_json(halted):
    repo, record = halted
    owner = {p: "C0" for p in C0_WRITES} | {p: "C1" for p in C1_WRITES}
    withheld = {p: kept_work.HALT_RECORD_ONLY for p in owner}
    (repo / ".git" / "objects").mkdir(parents=True, exist_ok=True)
    out = halted_record_ledger._record_kept(
        repo, f"{RECORD_DIR_ROOT}/{RUN}", owner, withheld, {}, [],
        consumed_returns={"C0": "DONE: C0", "C1": PARTIAL}, common_dir=repo / ".git",
    )
    assert out["kept"]["chunks"] == ["C0"], out


def _checkpoint(repo, owed: str | None):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*a):
        subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    trailers = f"Checkpoint-Plan: {PLAN_REL}" + (f"\nCheckpoint-Gate-Owed: {owed}" if owed else "")
    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", "checkpoint(wave 0): 2 rows — C0, C1", "-m", trailers)


def test_resume_landed_state_does_not_land_a_gate_owed_row(halted):
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    repo, _record = halted
    _checkpoint(repo, "C1")
    state = resume_landed_state(repo, PLAN_REL)
    assert "C0" in state.rows and "C1" not in state.rows and "C1" not in state.row_shas


def test_resume_landed_state_lands_both_rows_without_a_gate_owed_trailer(halted):
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    repo, _record = halted
    _checkpoint(repo, None)
    assert {"C0", "C1"} <= resume_landed_state(repo, PLAN_REL).rows


def test_default_refire_redispatches_a_gate_owed_partial_checkpoint_row(halted):
    repo, record = halted
    row_returns.write(record, {"C0": "DONE: C0", "C1": PARTIAL})
    kept_work.write(record, {"C0": {"paths": {p: kept_work.fingerprint(repo, p) for p in C0_WRITES}}}, None)
    _checkpoint(repo, "C1")
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert "C1" in v.resume_rows and "C1" not in {r for rows, _ in sink for r in rows}, (v.resume_rows, sink)
