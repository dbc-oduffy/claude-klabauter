"""Gate acceptance oracle: each design_route value surfaces in the verdict; an unapproved canvas halts design-review."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401

BOARD_URL = "https://claude.ai/artifact/fixture-canvas-001"
SOURCE = "docs/design/canvas-fixture/project/"
HARVEST = "docs/design/canvas-fixture/harvest/"
APPROVAL = {
    "by": "pm",
    "pm_ruling": "approved",
    "ruling_ref": "chat 2026-10-16",
    "board_url": BOARD_URL,
    "on": "2026-10-16",
}


def _route(route, by="pm", approval=None):
    block = {"route": route, "authority": {"by": by, "ref": "PM authorizes the Claude Design canvas"}}
    if approval is not None:
        block["approval"] = approval
    return block


def _sized(repo, **over):
    """An S sizing arms without a baton mint, so these cases need no git tree and no schema write."""
    _put(repo, tshirt="S", route="spec-dispatch", name="canvas-fixture", **over)


def _handoffs(repo):
    home = repo / "state" / "handoffs"
    return sorted(p.name for p in home.iterdir()) if home.is_dir() else []


@pytest.mark.parametrize(
    "route,by,approval",
    [("canvas", "pm", APPROVAL), ("reviewer", "em", None), ("none", "scout", None)],
)
def test_each_route_surfaces_with_its_authority(repo, route, by, approval):  # noqa: F811
    _sized(repo, design_route=_route(route, by, approval))
    verdict = gate(repo, REL, writes=["src/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    payload = verdict.to_json()["design_route"]
    assert payload["route"] == route
    assert payload["authority"]["by"] == by


def test_unapproved_canvas_halts_design_review_before_any_baton(repo):  # noqa: F811
    (repo / ".git").mkdir(exist_ok=True)
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, name="canvas-fixture", design_route=_route("canvas"))
    verdict = gate(repo, REL)
    assert verdict.arm is None and verdict.baton is None
    assert verdict.halt["kind"] == "design-review"
    assert "design_approval" in verdict.halt["touchpoint"]
    board = verdict.to_json()["design_route"]["board"]
    assert board["source"] == SOURCE and board["harvest"] == HARVEST
    assert _handoffs(repo) == []


def test_an_unapproved_canvas_halts_every_arm_not_only_m_plus(repo):  # noqa: F811
    _sized(repo, design_route=_route("canvas"))
    verdict = gate(repo, REL, writes=["src/a.py"])
    assert verdict.arm is None and verdict.halt["kind"] == "design-review"


def test_unapproved_canvas_is_refused_at_emit_with_a_design_review_line(repo):  # noqa: F811
    _sized(repo, design_route=_route("canvas"))
    with pytest.raises(SizingFireRefused) as err:
        _gate_sizing_at_emit(repo, REL, ["src/a.py"])
    line = err.value.fields[0]
    assert line.startswith("design-review:")
    assert "canvas" in line and "pm" in line and SOURCE in line


def test_approved_canvas_arms_and_carries_the_board_url(repo):  # noqa: F811
    _sized(repo, design_route=_route("canvas", approval=APPROVAL))
    verdict = gate(repo, REL, writes=["src/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    payload = verdict.to_json()["design_route"]
    assert payload["board"]["url"] == BOARD_URL and payload["approved"] is True


def test_a_sizing_without_design_route_has_no_key_and_no_design_halt(repo):  # noqa: F811
    _sized(repo)
    assert "design_route" not in yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    verdict = gate(repo, REL, writes=["src/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    assert "design_route" not in verdict.to_json()
