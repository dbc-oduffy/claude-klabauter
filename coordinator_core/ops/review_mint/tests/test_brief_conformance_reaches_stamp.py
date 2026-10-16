"""A code-reviewer's brief_conformance rows must reach review_stamp's integration data:
the review prompt hands it the brief items, and bookkeeping reads rows from a sidecar's
frontmatter or its body section."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.review_mint.execute_review import (
    OperativeCriterion,
    compose_execute_review,
)
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _STAGE_SCHEMAS,
    _v5_fragment,
)
from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave

_ROWS = [
    {"brief_item": "a", "requirement": "r", "file_line": "f:1", "verified_how": "v", "status": "met"},
    {"brief_item": "b", "requirement": "r", "file_line": "f:2", "verified_how": "v", "status": "unmet"},
]


def _wave_block(**kw) -> str:
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        **kw,
    )
    return dict(phases)["Review wave"]


def test_code_reviewer_prompts_carry_the_brief_conformance_instruction():
    block = _wave_block(criterion=OperativeCriterion("the operative statement"))
    assert block.count("Brief conformance:") >= 2
    assert "frontmatter `brief_conformance:` list" in block
    assert "Operative exit criterion: the operative statement" in block


def _bookkeep(tmp_path, sidecar_text: str) -> dict:
    (tmp_path / ".git").mkdir()
    share = tmp_path / ".coordinator-local" / "subagent-share" / "sess-1"
    share.mkdir(parents=True)
    sidecar = share / "slice-reviewer.md"
    sidecar.write_text(sidecar_text, encoding="utf-8")
    return bookkeep_wave(
        [sidecar],
        repo_root=tmp_path,
        session_id="sess-1",
        plan_id="pln-example-a1b2c3",
        prep_sidecar=None,
        record_stem="pln-example-a1b2c3.review-wave-bookkeeping",
    )["brief_conformance"]


def test_rows_in_the_sidecar_body_section_reach_the_integration_count(tmp_path):
    text = (
        "---\nagent_type: coordinator:code-reviewer\napplied: 0\n---\n# Review\n\n"
        "## Findings\n\nNone.\n\n## Brief conformance\n\n```json\n"
        f"{json.dumps(_ROWS)}\n```\n\n## Verdict\n\nOK\n"
    )
    assert _bookkeep(tmp_path, text) == {"items": 2, "met": 1, "unmet": 1}


def test_frontmatter_rows_win_over_the_body_section(tmp_path):
    fm = yaml.safe_dump({"agent_type": "coordinator:code-reviewer", "brief_conformance": _ROWS[:1]})
    text = f"---\n{fm}---\n# Review\n\n## Brief conformance\n\n```json\n{json.dumps(_ROWS)}\n```\n"
    assert _bookkeep(tmp_path, text) == {"items": 1, "met": 1, "unmet": 0}


def test_a_sidecar_with_no_rows_anywhere_still_counts_zero(tmp_path):
    text = "---\nagent_type: coordinator:code-reviewer\n---\n# Review\n\n## Findings\n\nNone.\n"
    assert _bookkeep(tmp_path, text) == {"items": 0, "met": 0, "unmet": 0}
