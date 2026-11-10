"""A refire of a never-staged `status: draft` plan stages its open rows when the gate's resume snapshot
dropped them with no recorded reason; every zero-row refusal names each excluded row with its reason."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r1"
LANDED = "landed at abc1234"


def _row(rid: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n{extra}"
    )


def _plan(tmp_path: Path, rows: str, status: str = "draft") -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\nstatus: {status}\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )


def _stage(tmp_path: Path, only_rows=None) -> dict:
    params = {"run_id": RUN, "plan_path": PLAN_REL}
    if only_rows is not None:
        params["only_rows"] = only_rows
    reply = _handler(params, repo_root=tmp_path)
    return reply if "error" in reply else read_reply(tmp_path, reply["manifest_handle"])


def test_refire_of_a_draft_plan_stages_rows_the_snapshot_dropped_without_a_reason(tmp_path):
    _plan(tmp_path, _row("C1") + _row("C2"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C1", "C2"], [])
    reply = _stage(tmp_path, ["C1", "C2"])
    assert "error" not in reply, reply
    assert {r["id"] for r in reply["rows"]} == {"C1", "C2"}


def test_refire_of_a_draft_plan_still_drops_rows_the_gate_reasoned_landed(tmp_path):
    _plan(tmp_path, _row("C1") + _row("C2"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C1", "C2"], [], reasons={"C1": LANDED, "C2": LANDED})
    err = _stage(tmp_path, ["C1", "C2"])["error"]
    assert "zero dispatchable rows" in err
    assert f"C1 ({LANDED})" in err and f"C2 ({LANDED})" in err


def test_refire_of_a_staged_plan_does_not_restage_unreasoned_drops(tmp_path):
    _plan(tmp_path, _row("C1") + _row("C2"), status="executing")
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C1", "C2"], [])
    err = _stage(tmp_path, ["C1", "C2"])["error"]
    assert "zero dispatchable rows" in err
    assert "C1 (outside only_rows)" in err and "C2 (outside only_rows)" in err


def test_zero_row_refusal_names_coded_rows_with_their_disposition(tmp_path):
    _plan(tmp_path, _row("C1", "  disposition: coded\n  disposition_ref: abc1234\n") + _row("C2", "  disposition: coded\n"))
    err = _stage(tmp_path)["error"]
    assert "zero dispatchable rows" in err
    assert "C1 [disposition: coded]" in err and "C2 [disposition: coded]" in err
