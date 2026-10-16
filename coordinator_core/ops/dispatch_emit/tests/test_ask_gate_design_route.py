"""Gate acceptance oracle: each design_route value surfaces in the verdict; an unapproved canvas arms carrying its payload."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401

BOARD_URL = "https://claude.ai/artifact/fixture-canvas-001"
SOURCE = "docs/design/canvas-fixture/project/"
HARVEST = "docs/design/canvas-fixture/harvest/"
APPROVAL = {"by": "pm", "at": "2026-10-16T23:00:00Z"}


def _route(route, by="pm", approval=None):
    return {
        "route": route,
        "authority": f"{by} authorizes the Claude Design canvas",
        "board_url": BOARD_URL if approval is not None else None,
        "approval": approval,
    }


def _sized(repo, **over):
    """An S sizing arms without a baton mint, so these cases need no git tree and no schema write."""
    _put(repo, tshirt="S", route="spec-dispatch", name="canvas-fixture", **over)


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
    assert payload["authority"] == f"{by} authorizes the Claude Design canvas"


def test_an_unapproved_canvas_arms_with_the_payload_and_no_board_url(repo):  # noqa: F811
    _sized(repo, design_route=_route("canvas"))
    verdict = gate(repo, REL, writes=["src/a.py"])
    assert verdict.halt is None and verdict.arm is not None
    payload = verdict.to_json()["design_route"]
    assert payload["approved"] is False and payload["board"]["url"] is None
    assert payload["board"]["source"] == SOURCE and payload["board"]["harvest"] == HARVEST


def test_unapproved_canvas_is_not_refused_at_emit(repo):  # noqa: F811
    _sized(repo, design_route=_route("canvas"))
    _gate_sizing_at_emit(repo, REL, ["src/a.py"])


def test_malformed_design_route_still_refuses_at_the_gate(repo):  # noqa: F811
    _sized(repo, design_route={"route": "bogus", "authority": "x", "board_url": None, "approval": None})
    verdict = gate(repo, REL, writes=["src/a.py"])
    assert verdict.arm is None and verdict.halt["kind"] == "refusal"


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
