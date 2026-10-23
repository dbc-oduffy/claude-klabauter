"""The 2026-10-22 example-cockpit-repo 57c72c re-fire rebuilt under tmp_path from quoted facts: discharged em rows plus
two rows held by sibling plans. With the sibling-held rows the zero-dispatchable refusal is correct (unlanded
work); without them the same plan closes through the all-landed path. Nothing reads cockpit."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_KIND_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git
from coordinator_core.ops.dispatch_emit.tests.test_refire_zero_dispatchable_already_landed_incident import (
    _checkpoint,
    _discharge,
    _init,
    _register,
)

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REFIRE_RUN = "ask-20261022T210944-57c72c"
ZERO_ROWS = "the spine derives zero dispatchable rows"
SIB_PEOPLE = "docs/plans/sib-people.md"
SIB_RAIL = "docs/plans/sib-rail.md"
WITHHELD = {
    "src/components/cockpit/group-pm/group-em-panel.tsx": "halt-record-only",
    "src/components/gsd/SessionBlock.test.tsx": "halt-record-only",
}


def _spine(repo, rel, rows, header="run_id: fixture\n"):
    """`rows` is `[(id, fields)]` with `fields` a dict of extra row YAML lines (already formatted, list values as
    pre-rendered text)."""
    body = ""
    for row_id, fields in rows:
        body += f"- id: {row_id}\n  title: T{row_id}\n"
        for key, value in fields.items():
            body += f"  {key}: {value}\n"
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{header}---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


def _cockpit_57c72c(repo, *, sibling_rows):
    ids = ["R1", "R2", "R3", "R4", "R5"]
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_register(ids))
    _init(repo)
    _checkpoint(repo, ["C1"], "wave0")
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()
    open_row = {"disposition": "open", "writes": "\n    - some/x.py"}
    rows = [
        ("C1", {"disposition": "coded", "disposition_ref": first, "writes": "\n    - some/c1.py", "register_rows": "[R1]"}),
        ("C11", {**open_row, "register_rows": "[R2]"}),
        ("C15", {**open_row, "register_rows": "[R3]"}),
        ("C13", {**open_row, "performer": "em", "register_rows": "[R4]"}),
        ("C20", {**open_row, "performer": "em", "register_rows": "[R5]"}),
    ]
    if sibling_rows:
        rows += [
            ("C16", {"disposition": "open", "writes": "[]", "register_rows": "[R2]",
                     "depends_on_plan": f"\n    - plan: {SIB_PEOPLE}\n      chunk: C10"}),
            ("C21", {"disposition": "open", "agent_type": "coordinator:test-runner", "writes": "[]",
                     "register_rows": "[R3]", "depends_on_plan": f"\n    - plan: {SIB_RAIL}\n      chunk: C1"}),
        ]
        _spine(repo, SIB_PEOPLE, [("C10", {"disposition": "open", "writes": "\n    - some/p.py"})])
        _spine(repo, SIB_RAIL, [("C1", {"disposition": "open", "writes": "\n    - some/r.py"})])
    _spine(repo, PLAN_REL, rows)
    _checkpoint(repo, ["C11", "C15"], "wave1")
    _discharge(repo, ["C13", "C20"])
    record = repo / RECORD_DIR_ROOT / REFIRE_RUN
    write_halt_marker(record, halted="commit", sizing_rel=REL, plan_rel=PLAN_REL)
    halted = record / "halted.json"
    payload = json.loads(halted.read_text(encoding="utf-8"))
    payload["run_artifacts"] = {"withheld": WITHHELD}
    halted.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", halted_record_message(REFIRE_RUN, {"halted": "commit", "plan": PLAN_REL}))


def _refusal(repo, out):
    with pytest.raises(AskStageError) as err:
        stage(
            repo, run_id=REFIRE_RUN, plan_rel=PLAN_REL, writes=[], cross_repo_approved=False, session_id=None,
            only_rows=out["resume_rows"],
        )
    return str(err.value)


def test_1_sibling_held_rows_take_the_row_resume_and_the_stage_refuses_them(repo):  # noqa: F811
    _cockpit_57c72c(repo, sibling_rows=True)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and out["arm"] == "m_plus", out
    assert not out.get("resume_ledger"), out
    assert out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["C16", "C21"], out
    assert _refusal(repo, out).startswith(ZERO_ROWS)


def test_2_refusal_names_each_held_row_and_the_sibling_row_it_waits_on(repo):  # noqa: F811
    _cockpit_57c72c(repo, sibling_rows=True)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    message = _refusal(repo, out)
    assert "C16" in message and "sib-people.md" in message and "C10" in message
    assert "C21" in message and "sib-rail.md" in message and "C1" in message


def test_3_control_without_sibling_rows_closes_through_the_all_landed_path(repo):  # noqa: F811
    _cockpit_57c72c(repo, sibling_rows=False)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and out["arm"] == "m_plus", out
    assert out["resume_ledger"][RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND, out
    assert [r["id"] for r in read_reply(repo, out["manifest_handle"])["rows"]] == [], out
