"""The re-judge route hands the terminal judge the plan's register rows verbatim."""
from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.reverify_delivery import compose_rejudge_script
from coordinator_core.ops.review_mint.execute_review import _prompt_literal
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment

_TEXT = "Let me \"open\" it\nfrom the menu"


def _fragment():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {
            "kind": "judge",
            "agents": [
                {"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge-result"}
            ],
        }
    )
    return fragment


def _script(tmp_path, plan_text):
    plan = tmp_path / "plan.md"
    plan.write_text(plan_text, encoding="utf-8")
    return compose_rejudge_script(
        fragment=_fragment(),
        stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path=str(plan),
        plan_id="p1",
        run_base_sha="a" * 40,
        head_sha="b" * 40,
        repo_root=tmp_path,
    )


def _register_plan(tmp_path):
    row = {
        "id": "a", "source_text": _TEXT, "anchor": "p.3", "surface": "ui", "status": "open",
        "claimed_by": ["plan.md"], "wired": False, "met_by": [], "last_progress_at": None,
        "ruling": None,
    }
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(
        "schema: sizing-object\n" + yaml.safe_dump({"requirement_register": {"rows": [row]}}), encoding="utf-8"
    )
    return '---\nplan_id: "p1"\nsizing_object: "state/sizings/s.yaml"\n---\nbody\n'


def test_rejudge_script_carries_register_rows_verbatim(tmp_path):
    script = _script(tmp_path, _register_plan(tmp_path))
    assert _prompt_literal(_TEXT)[1:-1] in script
    assert "anchor=p.3" in script and "surface=ui" in script


def test_plan_without_register_gets_pointer_tier_only(tmp_path):
    script = _script(tmp_path, "---\nplan_id: p1\n---\nbody\n")
    assert "TIER 1 (pointer)" in script
    assert "TIER 1 evidence, verbatim" not in script
