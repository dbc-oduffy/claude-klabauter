"""Replay of the 20261024 commit-v2 refire: an open non-EM row a blocking ledger item names by id, whose only
write an earlier foreign commit landed, must still reach the stage on the ordinary resume path."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/2026-10-24-incident.md"
SIZING_REL = "state/sizings/2026-10-24-incident.yaml"
HALTED_RUN = "warp-2026-10-24-incident-20261024T013305-dc22b4"
REMAINING = "X1: append the paired VERDICT line"


def _rows(*, with_t2: bool) -> str:
    def row(i: str, disposition: str, writes: list[str], extra: str = "") -> str:
        return (
            f"- id: {i}\n  title: t{i}\n  disposition: {disposition}\n  register_rows: [R1]\n"
            f"  writes: {json.dumps(writes)}\n{extra}"
        )

    body = (
        row("S2", "coded", ["docs/v.md"], "  performer: em\n  change_kind: verification\n")
        + row("D1c", "open", ["a.py"], "  change_kind: code-edit\n")
        + row("X1", "open", ["docs/v.md"], "  change_kind: verification\n  depends_on: [D1c]\n")
        + row("R1", "open", [], "  performer: em\n  kind: memo-send\n")
    )
    if with_t2:
        body += row("T2", "open", ["t2.py"], "  change_kind: code-edit\n")
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _commit(repo, message: str, *trailers: str) -> None:
    _git(repo, "add", "-A")
    args = ["commit", "-q", "-m", message]
    if trailers:
        args += ["-m", "\n".join(trailers)]
    _git(repo, *args)


def _incident_repo(repo, remaining: str = REMAINING, *, item: str = "R1", stage_x1: bool = False, add_t2: bool = True) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / PLAN_REL).write_text(_rows(with_t2=False), encoding="utf-8", newline="\n")
    _commit(repo, "plan")
    (repo / "docs" / "v.md").write_text("verdict\n", encoding="utf-8")
    _commit(repo, "S2: verdict")
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=SIZING_REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": item, "remaining": [remaining], "evidence": ""}],
    )
    staged = [{"id": "D1c", "writes": ["a.py"]}, {"id": "R1", "writes": []}]
    if stage_x1:
        staged.append({"id": "X1", "writes": ["docs/v.md"]})
    (record / "manifest.json").write_text(json.dumps({"rows": staged}), encoding="utf-8")
    message = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    head, _, trailers = message.partition("\n\n")
    _commit(repo, head, trailers, "Lands-Chunks: D1c")
    # Rows added after the halt record make `_ledger_halt_for`'s staged-subset test fail: fire 7's route.
    if add_t2:
        (repo / PLAN_REL).write_text(_rows(with_t2=True), encoding="utf-8", newline="\n")


def test_open_non_em_row_a_blocking_item_names_is_not_outside_credited(tmp_path):
    _incident_repo(tmp_path)
    got = resumable_plan({"plan": PLAN_REL}, SIZING_REL, tmp_path)
    assert got.ledger is None, got
    assert "X1" in got.incomplete, f"X1 outside-credited: incomplete={got.incomplete} landed_open={got.landed_open}"
    assert "X1" not in {i for i, _ in got.landed_open}, got.landed_open
