"""dispatch.ask_gate's design-registry leg: brief, minted rows, pointer line, unparseable halt."""

from __future__ import annotations

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import design_conformance as dc
from coordinator_core.ops.dispatch_emit.ask_contract import GateVerdict
from coordinator_core.ops.dispatch_emit.ask_gate import _handler, gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401

REGISTRY = """schema: design-registry/1
entries:
  - id: settings
    board: https://claude.ai/artifact/fixture-canvas-001
    artboards: [route-settings-390]
    harvest: docs/design/x/harvest/
    sources:
      - "src/app/settings/**"
    status: designed
"""
SCOPE = ["src/app/settings/page.tsx"]


def _git(repo):
    (repo / ".git").mkdir(exist_ok=True)


def _registry(repo, text=REGISTRY):
    path = repo / "docs" / "design" / "registry.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rows(repo):
    register = rr.read_register((repo / REL).read_text(encoding="utf-8"))
    return [dict(r) for r in register.rows] if register is not None else []


def test_hits_appear_in_the_design_brief_and_serialise_under_the_shared_key(repo):  # noqa: F811
    _registry(repo)
    _put(repo)
    verdict = gate(repo, REL, writes=SCOPE)
    assert verdict.halt is None
    assert "Entry settings" in verdict.design_brief and "fixture-canvas-001" in verdict.design_brief
    assert verdict.to_json()[dc.DESIGN_BRIEF_KEY] == verdict.design_brief


def test_scope_includes_acceptance_row_writes(repo):  # noqa: F811
    _registry(repo)
    _put(repo, acceptance_rows=[{"id": "A1", "writes": SCOPE}])
    assert "Entry settings" in gate(repo, REL).design_brief


def test_a_run_bound_call_mints_one_row_per_hit_and_a_refire_adds_none(repo):  # noqa: F811
    _git(repo)
    _registry(repo)
    _put(repo)
    gate(repo, REL, writes=SCOPE, mint_design_rows=True)
    first = [r for r in _rows(repo) if dc.entry_id(r)]
    assert [dc.entry_id(r) for r in first] == ["settings"]
    gate(repo, REL, writes=SCOPE, mint_design_rows=True)
    assert [r for r in _rows(repo) if dc.entry_id(r)] == first


def test_an_unbound_call_writes_nothing(repo):  # noqa: F811
    _git(repo)
    _registry(repo)
    _put(repo)
    before = (repo / REL).read_text(encoding="utf-8")
    reply = _handler({"sizing_path": REL, "writes": SCOPE}, repo_root=repo)
    assert dc.DESIGN_BRIEF_KEY in reply
    assert (repo / REL).read_text(encoding="utf-8") == before


def test_a_registry_with_no_hit_gives_the_pointer_line_and_an_empty_scope_gives_none(repo):  # noqa: F811
    _registry(repo)
    _put(repo)
    assert gate(repo, REL, writes=["src/other/x.ts"]).design_brief.startswith("Design registry: docs/design/registry.yaml")
    assert gate(repo, REL).design_brief == ""


def test_no_registry_gives_no_key(repo):  # noqa: F811
    _put(repo)
    verdict = gate(repo, REL, writes=SCOPE)
    assert verdict.design_brief == "" and dc.DESIGN_BRIEF_KEY not in verdict.to_json()


def test_a_malformed_registry_with_a_scope_halts_and_names_the_repair(repo):  # noqa: F811
    _registry(repo, "schema: nope\n")
    _put(repo)
    verdict = gate(repo, REL, writes=SCOPE, mint_design_rows=True)
    assert verdict.halt["kind"] == "refusal"
    assert "registry-unparseable" in verdict.halt["reason"] and "repair docs/design/registry.yaml" in verdict.halt["reason"]
    assert gate(repo, REL).halt is None


def test_halted_verdicts_carry_no_brief(repo):  # noqa: F811
    _registry(repo)
    _put(repo, tshirt="XL", route="spec-dispatch")
    verdict = gate(repo, REL, writes=SCOPE)
    assert verdict.halt is not None and verdict.design_brief == ""


def test_gate_verdict_default_brief_is_empty():
    assert GateVerdict("s", None).design_brief == ""


UNREG_URL = "https://claude.ai/artifact/unreg-canvas-777"
UNREG_ANCHOR = "design-registry:canvas-unreg-canvas-777"


def _anchors(repo):
    return [str(r.get(rr.ROW_ANCHOR) or "") for r in _rows(repo)]


def test_an_unregistered_board_in_the_intent_mints_a_canvas_row_with_empty_scope(repo):  # noqa: F811
    _git(repo)
    _registry(repo)
    _put(repo, intent=f"build the nudge from {UNREG_URL}")
    verdict = gate(repo, REL, mint_design_rows=True)
    assert verdict.halt is None
    assert _anchors(repo).count(UNREG_ANCHOR) == 1
    assert "unreg-canvas-777" in verdict.design_brief and "register" in verdict.design_brief
    gate(repo, REL, mint_design_rows=True)
    assert _anchors(repo).count(UNREG_ANCHOR) == 1


def test_a_board_named_in_a_register_row_source_text_mints_a_canvas_row(repo):  # noqa: F811
    _git(repo)
    _put(
        repo,
        requirement_register={"rows": [{
            "id": "R1", "source_text": f"see {UNREG_URL}", "anchor": "", "surface": "visual",
            "status": "open", "claimed_by": [],
        }]},
    )
    gate(repo, REL, mint_design_rows=True)
    assert UNREG_ANCHOR in _anchors(repo)


def test_a_registered_board_outside_scope_mints_no_canvas_duplicate(repo):  # noqa: F811
    _git(repo)
    _registry(repo)
    _put(repo, intent="restyle https://claude.ai/artifact/fixture-canvas-001")
    verdict = gate(repo, REL, writes=["src/app/other/page.tsx"], mint_design_rows=True)
    assert not [a for a in _anchors(repo) if a.startswith("design-registry:canvas-")]
    assert "Unregistered canvas boards" not in verdict.design_brief


def test_no_registry_file_still_briefs_an_unregistered_board(repo):  # noqa: F811
    _put(repo, intent=f"build from {UNREG_URL}")
    verdict = gate(repo, REL)
    assert "unreg-canvas-777" in verdict.design_brief and "no registry file" in verdict.design_brief


def test_a_sizing_naming_no_board_and_no_scope_gets_no_brief(repo):  # noqa: F811
    _registry(repo)
    _put(repo)
    assert gate(repo, REL).design_brief in ("", None)
