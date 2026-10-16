"""design_conformance: brief text, row minting and the met-evidence rule."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core import reviewer_vocabulary as rv
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import design_conformance as dc
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-16-design.yaml"
BOARD = "https://claude.ai/design/p/abc123"
OTHER_BOARD = "https://claude.ai/design/p/zzz999"
HIT = {
    "id": "settings", "board_url": BOARD, "artboards": ["Desktop", "Mobile"], "status": "designed",
    "harvest_path": "docs/design/settings/harvest.json", "paths": ["src/app/(app)/settings/page.tsx"],
}


def _sizing():
    return {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, "ask-20261016T000000-abc123")
    rl.bind(tmp_path, "ask-20261016T000000-abc123", REL, ask_text="the ask")
    return tmp_path


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _row(repo):
    dc.mint_rows(repo, REL, [HIT])
    return next(r for r in _rows(repo) if r[rr.ROW_ANCHOR] == dc.ANCHOR_PREFIX + "settings")


def _write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _record(repo, rel="reviews/fe.md", *, reviewer=None, agent_type=None, verdict="APPROVED", body=BOARD):
    fm = []
    if reviewer:
        fm.append(f"reviewer: {reviewer}")
    if agent_type:
        fm.append(f"agent_type: {agent_type}")
    if verdict:
        fm.append(f"verdict: {verdict}")
    return _write(repo, rel, "---\n" + "\n".join(fm) + f"\n---\nReviewed {body}\n")


def test_constants_and_vocabulary():
    assert dc.ANCHOR_PREFIX == "design-registry:" and dc.DESIGN_BRIEF_KEY == "design_brief"
    assert dc.DESIGN_SURFACE in rr.SURFACES
    assert rv.SENIOR_FRONT_END in rv.DELEGATE_REVIEWERS


def test_bare_agent_type():
    assert rv.bare_agent_type("coordinator:senior-front-end") == "senior-front-end"
    assert rv.bare_agent_type("senior-front-end") == "senior-front-end"
    assert rv.bare_agent_type(None) is None and rv.bare_agent_type(3) is None


def test_brief_text_hits_pointer_and_absent_registry():
    text = dc.brief_text([HIT], "docs/design/registry.yaml")
    for part in (BOARD, "Desktop, Mobile", "designed", "docs/design/settings/harvest.json",
                 "src/app/(app)/settings/page.tsx", "harvest.json", "design-deviation", "front-end",
                 "docs/design/registry.yaml"):
        assert part in text
    pointer = dc.brief_text([], "docs/design/registry.yaml")
    assert "docs/design/registry.yaml" in pointer and "\n" not in pointer
    assert dc.brief_text([HIT], None) == "" and dc.brief_text([], "") == ""


def test_brief_text_omits_harvest_when_absent():
    bare = {k: v for k, v in HIT.items() if k != "harvest_path"}
    assert "Harvest:" not in dc.brief_text([bare], "r.yaml")


def test_mint_is_idempotent_and_the_register_validates(repo):
    before = len(_rows(repo))
    added = dc.mint_rows(repo, REL, [HIT, {**HIT, "id": "billing"}])
    assert len(added) == 2 and len(_rows(repo)) == before + 2
    assert dc.mint_rows(repo, REL, [HIT, {**HIT, "id": "billing"}]) == []
    assert len(_rows(repo)) == before + 2
    row = _rows(repo)[-2]
    assert row[rr.ROW_ANCHOR] == "design-registry:settings" and row[rr.ROW_SURFACE] == "visual"
    assert row[rr.ROW_STATUS] == rr.STATUS_OPEN and BOARD in row[rr.ROW_TEXT]
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    assert validate_frontmatter(doc, rl._SIZING_SCHEMA) == []


def test_sidecar_by_reviewer_passes(repo):
    row = _row(repo)
    rec = _record(repo, reviewer="senior-front-end")
    assert dc.evidence_refusal(repo, row, {"met_by": [rec]}, None) is None


def test_sidecar_by_agent_type_only_passes(repo):
    row = _row(repo)
    rec = _record(repo, agent_type="coordinator:senior-front-end", verdict="APPROVED_WITH_NOTES")
    assert dc.evidence_refusal(repo, row, {"met_by": rec}, None) is None


def test_wrong_reviewer_fails(repo):
    row = _row(repo)
    rec = _record(repo, reviewer="staff-eng")
    assert "senior-front-end" in dc.evidence_refusal(repo, row, {"met_by": [rec]}, None)


@pytest.mark.parametrize("verdict", ["REJECTED", "requires-changes", "BLOCKED", "PIVOT", None])
def test_refusing_or_absent_verdict_fails(repo, verdict):
    row = _row(repo)
    rec = _record(repo, reviewer="senior-front-end", verdict=verdict)
    assert "verdict" in dc.evidence_refusal(repo, row, {"met_by": [rec]}, None)


def test_a_different_board_fails(repo):
    row = _row(repo)
    rec = _record(repo, reviewer="senior-front-end", body=OTHER_BOARD)
    assert "board" in dc.evidence_refusal(repo, row, {"met_by": [rec]}, None)


def test_missing_file_and_empty_met_by_fail(repo):
    row = _row(repo)
    assert "no file" in dc.evidence_refusal(repo, row, {"met_by": ["reviews/none.md"]}, None)
    assert "met_by" in dc.evidence_refusal(repo, row, {}, None)
    assert dc.evidence_refusal(repo, row, {"met_by": ["../escape.md"]}, None) is not None


def test_status_in_the_registry_never_meets_the_row(repo):
    row = _row(repo)
    assert dc.evidence_refusal(repo, row, {"status": "met", "met_by": []}, None) is not None


def _deviation(path):
    return f"# plan\n\n```yaml design-deviation\n- entry: settings\n  ratified_by: {path}\n```\n"


def test_ratified_deviation_in_plan_text_passes(repo):
    row = _row(repo)
    pm = _write(repo, "state/pm/ratify.md", "PM: the deviation stands.\n")
    assert dc.evidence_refusal(repo, row, {"met_by": [pm]}, _deviation(pm)) is None


def test_ratification_cited_for_another_entry_fails(repo):
    row = _row(repo)
    pm = _write(repo, "state/pm/ratify.md", "PM: ok\n")
    text = _deviation(pm).replace("entry: settings", "entry: billing")
    assert dc.evidence_refusal(repo, row, {"met_by": [pm]}, text) is not None


def test_uncited_ratification_file_fails(repo):
    row = _row(repo)
    pm = _write(repo, "state/pm/ratify.md", "PM: ok\n")
    assert dc.evidence_refusal(repo, row, {"met_by": [pm]}, None) is not None


def test_ratification_under_docs_plans_fails(repo):
    row = _row(repo)
    plan = _write(repo, "docs/plans/self.md", "the plan ratifies itself\n")
    why = dc.evidence_refusal(repo, row, {"met_by": [plan]}, _deviation(plan))
    assert why is not None and "plan" in why


def test_registry_entry_ratification_passes(repo, monkeypatch):
    row = _row(repo)
    pm = _write(repo, "state/pm/ratify.md", "PM: ok\n")
    monkeypatch.setattr(dc, "_registry_ratification", lambda root, eid: pm if eid == "settings" else None)
    assert dc.evidence_refusal(repo, row, {"met_by": [pm]}, None) is None
    other = _write(repo, "state/pm/other.md", "x\n")
    assert dc.evidence_refusal(repo, row, {"met_by": [other]}, None) is not None


def test_refusal_is_one_sentence_naming_the_row(repo):
    row = _row(repo)
    why = dc.evidence_refusal(repo, row, {"met_by": []}, None)
    assert row[rr.ROW_ID] in why and "settings" in why
