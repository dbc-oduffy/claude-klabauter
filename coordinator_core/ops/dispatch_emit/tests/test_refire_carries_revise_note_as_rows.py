"""A live revise note that no spine row executes is owed as a row on the refired run, whether the refire keeps
its sizing or re-sizes to a new one; a note a row already executes, or that was retracted, is not owed again."""

from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import revise_absorb as ra
from coordinator_core.ops.dispatch_emit.tests.test_revise_absorb import PLAN, REL, RUN, _pending, _repo

NOTE = "also add the targeted_test_launcher key to the sizing schema"
NEW_RUN = "warp-2026-10-17-resized-abc123-20261017T000000-aaaaaa"
NEW_REL = "state/sizings/2026-10-17-resized.yaml"
NEW_PLAN = "docs/plans/2026-10-17-resized.md"

SPINE = """## Tasks

```yaml plan-tasks
- id: C1
  title: existing row
  writes:
    - coordinator_core/ops/dispatch_emit/op.py
  body: |
    {body}
```
"""


def _note(root, text=NOTE, run=RUN):
    out = rl._handler({"run_id": run, "note": text}, repo_root=root)
    assert out["ok"] is True
    return out


def _spine(root, plan=PLAN, body="do the thing"):
    path = root / plan
    indented = body.replace("\n", "\n    ")
    path.write_text(path.read_text(encoding="utf-8") + SPINE.format(body=indented), encoding="utf-8")


def _resized_run(root):
    """A refired run on a re-sized sizing and plan whose run.json names the halted predecessor."""
    sizing = yaml.safe_load((root / REL).read_text(encoding="utf-8"))
    sizing["plan"] = NEW_PLAN
    (root / NEW_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = (
        f"title: p2\nregister_claims:\n  sizing: {NEW_REL}\n  rows: [R1]\n"
        f"  pins: {{\"R1\": \"{rr.row_pin('build the op')}\"}}\n"
    )
    (root / NEW_PLAN).write_text("---\n" + fm + "---\n# body\n", encoding="utf-8")
    rl.start(root, NEW_RUN, resumed_from=RUN)
    rl.bind(root, NEW_RUN, NEW_REL)


def _owed(reply):
    return [r for r in reply["rows"] if r.get("kind") == ra.NOTE_KIND]


def test_a_note_only_revise_is_owed_as_a_row_on_a_same_sizing_refire(tmp_path):
    root = _repo(tmp_path)
    _note(root)

    reply = _pending(root)

    assert [(r["id"], r["text"]) for r in _owed(reply)] == [("N1", NOTE)]
    assert reply["drift"] == [] and reply["plan"] == PLAN


def test_a_note_survives_a_resized_refire_keyed_by_note_id_not_sizing_path(tmp_path):
    root = _repo(tmp_path)
    _note(root)
    _resized_run(root)

    reply = ra._pending_handler({"run_id": NEW_RUN, "plan": NEW_PLAN}, repo_root=root)

    assert [(r["id"], r["text"]) for r in _owed(reply)] == [(f"{RUN}:N1", NOTE)]
    assert reply["sizing"] == NEW_REL


def test_a_row_carrying_the_note_marker_executes_it_on_either_sizing(tmp_path):
    root = _repo(tmp_path)
    _note(root)
    _spine(root, body=f"{ra.NOTE_ROW_MARKER}N1\n{NOTE}")
    assert _owed(_pending(root)) == []

    _resized_run(root)
    _spine(root, plan=NEW_PLAN, body=f"{ra.NOTE_ROW_MARKER}{RUN}:N1\n{NOTE}")
    assert _owed(ra._pending_handler({"run_id": NEW_RUN, "plan": NEW_PLAN}, repo_root=root)) == []


def test_a_marker_for_another_note_does_not_execute_this_one(tmp_path):
    root = _repo(tmp_path)
    _note(root)
    _note(root, "second note without a path")
    _spine(root, body=f"{ra.NOTE_ROW_MARKER}N1\nx")

    assert [r["id"] for r in _owed(_pending(root))] == ["N2"]


def test_a_marker_prefix_of_a_longer_note_key_does_not_execute_it(tmp_path):
    root = _repo(tmp_path)
    _note(root)
    _spine(root, body=f"{ra.NOTE_ROW_MARKER}N10\nx")

    assert [r["id"] for r in _owed(_pending(root))] == ["N1"]


def test_a_note_naming_a_rows_declared_path_is_executed_by_that_row(tmp_path):
    root = _repo(tmp_path)
    _note(root, "make coordinator_core/ops/dispatch_emit/op.py log the refire")
    _spine(root)

    assert _owed(_pending(root)) == []


def test_a_retracted_note_is_not_owed(tmp_path):
    root = _repo(tmp_path)
    _note(root)
    out = rl._handler(
        {"run_id": RUN, "retract_note": {"note_id": "N1", "reason": "applied by hand"}}, repo_root=root,
    )
    assert out["ok"] is True

    assert _owed(_pending(root)) == []


def test_a_note_rides_a_register_refusal_so_it_is_never_dropped(tmp_path):
    root = _repo(tmp_path)
    sizing = yaml.safe_load((root / REL).read_text(encoding="utf-8"))
    (root / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    _note(root)
    plan = root / PLAN
    plan.write_text(plan.read_text(encoding="utf-8").replace("rows: [R1]", "rows: not-a-list"), encoding="utf-8")

    reply = _pending(root)

    assert "error" not in reply and [r["id"] for r in _owed(reply)] == ["N1"]
    assert "not a list" in reply["register_refusal"]


def test_a_run_with_no_notes_keeps_the_register_reply_and_refusal(tmp_path):
    root = _repo(tmp_path)

    assert _pending(root)["rows"] == []
    assert "error" in _pending(root, plan="docs/plans/other.md")


def test_the_tail_prompt_tells_the_author_to_mark_note_rows_and_not_claim_them():
    js = ra.absorb_tail_js(plan_path=PLAN, run_id=RUN)

    assert ra.NOTE_ROW_MARKER in js and "never a note id" in js
