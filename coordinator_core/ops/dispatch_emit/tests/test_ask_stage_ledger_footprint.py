"""ask_stage stages the ledger-named plan body into X1 and owes CLAUDE.md to the EM."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage

SIZING_REL = "state/sizings/2026-10-22-ledger-fixture.yaml"


def _repo(root, text="strip docs/plans/p.md and edit CLAUDE.md"):
    for rel in ("docs/plans/p.md", "CLAUDE.md", "pkg/a.py"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8", newline="\n")
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "estimate": {"tshirt": "XS"},
        "requirement_register": {
            "rows": [{"id": "R1", "source_text": text, "status": "open", "anchor": "relayed"}]
        },
    }
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_mixed_set_declares_plan_body_and_owes_e1(tmp_path):
    _repo(tmp_path)
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    declared = {w for r in m.rows for w in r.writes}
    assert "docs/plans/p.md" in declared
    assert "CLAUDE.md" not in {w for r in m.rows if r.id != "E1" for w in r.writes}
    assert [(o.id, o.reason) for o in m.owed] == [("E1", "em-performed")]


def test_claude_md_only_set_raises_with_owed_em_ids(tmp_path):
    _repo(tmp_path, "edit CLAUDE.md")
    with pytest.raises(AskStageError) as exc:
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["CLAUDE.md"])
    assert exc.value.owed_em_ids == ["E1"]
