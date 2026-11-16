"""A refire of a halted run carries the run's live revise notes into its row briefs, stages a noted row
fresh instead of kept, and refuses an unchanged criterion_not_met refire before any manifest is written.

Every case drives `ask_stage._handler` over a prior run it staged itself, then halts.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_run, kept_work, requirements_ledger as rl, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
PRIOR = "r1"
RUN = "r2"
WRITES = {"C1": "docs/x/C1.md", "C2": "docs/x/C2.md"}
NOTE = "widen C1 to also cover docs/x/extra.md and re-run"


def _plan(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    rows = "".join(
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: {w}\n  writes:\n    - {w}\n"
        for rid, w in WRITES.items()
    )
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )


def _stage(tmp_path: Path, run_id: str) -> dict:
    return read_stage_reply(tmp_path, _handler({"run_id": run_id, "plan_path": PLAN_REL}, repo_root=tmp_path))


def _halted_prior(tmp_path: Path, halted: str = "criterion_not_met") -> None:
    _plan(tmp_path)
    assert "rows" in _stage(tmp_path, PRIOR)
    marker = {"status": halted_run.STATUS_HALTED, "halted": halted}
    (record_dir(tmp_path, PRIOR) / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def _note(tmp_path: Path, text: str = NOTE) -> None:
    line = json.dumps({"note": text, "at": "2026-11-16T00:00:00Z"})
    (record_dir(tmp_path, PRIOR) / rl.NOTES_FILE).write_text(line + "\n", encoding="utf-8", newline="\n")


def _refire(tmp_path: Path) -> dict:
    rl.start(tmp_path, RUN, resumed_from=PRIOR)
    return _stage(tmp_path, RUN)


def test_a_note_naming_a_row_reaches_its_brief_with_its_inherited_id(tmp_path):
    _halted_prior(tmp_path)
    _note(tmp_path)
    rows = {r["id"]: r for r in _refire(tmp_path)["rows"]}
    brief = (tmp_path / rows["C1"]["brief_path"]).read_text(encoding="utf-8")
    assert "## Revise notes (binding EM context)" in brief
    assert f"- {PRIOR}:N1: {NOTE}" in brief
    assert f"- {PRIOR}:N1: {NOTE}" in (tmp_path / rows["C2"]["brief_path"]).read_text(encoding="utf-8")


def test_a_retracted_note_leaves_the_refire_unchanged_and_refused(tmp_path):
    _halted_prior(tmp_path)
    _note(tmp_path)
    notes = record_dir(tmp_path, PRIOR) / rl.NOTES_FILE
    with notes.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"retracts": "N1", "reason": "wrong", "at": "2026-11-16T00:01:00Z"}) + "\n")
    reply = _refire(tmp_path)
    assert "refire unchanged" in json.dumps(reply)


def test_a_kept_row_named_by_a_note_is_staged_fresh_and_an_unnamed_one_stays_kept(tmp_path):
    _halted_prior(tmp_path)
    for rid in WRITES:
        for rel, text in ((WRITES[rid], "bytes"), (_dispatch_report_path(PLAN_REL, rid), f"DONE: {rid}")):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    kept_work.write(
        record_dir(tmp_path, PRIOR),
        {rid: {"paths": {WRITES[rid]: kept_work.fingerprint(tmp_path, WRITES[rid])}} for rid in WRITES},
        None,
    )
    _note(tmp_path, "C1 needs docs/x/other.md too")
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], list(WRITES), kept=tuple(WRITES), kept_run=PRIOR)
    rl.start(tmp_path, RUN, resumed_from=PRIOR)
    reply = read_stage_reply(
        tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": list(WRITES)}, repo_root=tmp_path)
    )
    rows = {r["id"]: r for r in reply["rows"]}
    assert "kept_from" not in rows["C1"]
    assert rows["C2"]["kept_from"] == PRIOR
    assert "Revise notes" in (tmp_path / rows["C1"]["brief_path"]).read_text(encoding="utf-8")


def test_an_unchanged_refire_is_refused_naming_the_brief_and_writes_no_manifest(tmp_path):
    _halted_prior(tmp_path)
    reply = _refire(tmp_path)
    assert "error" in reply
    assert "refire unchanged" in json.dumps(reply) and "briefs/C1.md" in json.dumps(reply)
    assert not (record_dir(tmp_path, RUN) / "manifest.json").exists()


def test_a_refire_with_a_note_is_not_refused(tmp_path):
    _halted_prior(tmp_path)
    _note(tmp_path)
    assert "error" not in _refire(tmp_path)


def test_an_unchanged_refire_of_another_halt_reason_is_not_refused(tmp_path):
    _halted_prior(tmp_path, halted="plan_gate")
    assert "error" not in _refire(tmp_path)
