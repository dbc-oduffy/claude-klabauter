"""design_route contract: payloads, shape errors, board paths, GateVerdict key."""

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as ac
from coordinator_core.ops.dispatch_emit import design_route as dr

APPROVAL = {"by": "pm", "pm_ruling": "approved", "ruling_ref": "chat", "board_url": "https://claude.ai/artifact/abc", "on": "2026-10-16"}


def _block(route="canvas", by="pm", ref="PM authorizes", **extra):
    authority = {"by": by}
    if ref is not None:
        authority["ref"] = ref
    return {"route": route, "authority": authority, **extra}


def test_absent_is_none_none():
    assert dr.read({}, "state/sizings/x.yaml") == (None, None)


def test_canvas_unapproved_payload():
    payload, bad = dr.read({"name": "n1", "design_route": _block()}, "state/sizings/x.yaml")
    assert bad is None
    assert payload == {
        "route": "canvas",
        "authority": {"by": "pm", "ref": "PM authorizes"},
        "driver": dr.DRIVERS["canvas"],
        "board": {"source": "docs/design/n1/project/", "harvest": "docs/design/n1/harvest/", "url": None},
        "approved": False,
    }


def test_canvas_approved_payload_and_flag():
    sizing = {"name": "n1", "design_route": _block(approval=APPROVAL)}
    payload, _ = dr.read(sizing, "s.yaml")
    assert payload["approved"] is True and payload["board"]["url"] == APPROVAL["board_url"]


@pytest.mark.parametrize("route", ["reviewer", "none"])
def test_non_canvas_payload_has_no_board(route):
    payload, bad = dr.read({"design_route": _block(route=route, by="scout", ref=None)}, "s.yaml")
    assert bad is None
    assert payload == {"route": route, "authority": {"by": "scout"}, "driver": dr.DRIVERS[route]}


@pytest.mark.parametrize(
    "block",
    [
        "canvas",
        _block(route="bogus"),
        _block(by="boss"),
        _block(by="em", ref=None),
        _block(by="pm", ref="  "),
        {"route": "canvas"},
        _block(extra_key=1),
        _block(route="reviewer", approval=APPROVAL),
        _block(approval={**APPROVAL, "by": "apm"}),
        _block(approval={**APPROVAL, "board_url": ""}),
    ],
)
def test_malformed_shapes_refuse(block):
    assert dr.block_errors(block)
    payload, bad = dr.read({"design_route": block}, "s.yaml")
    assert payload is None and bad


def test_board_paths_from_name_and_stem():
    assert dr.board_paths({"name": "foo"}, "state/sizings/bar.yaml")["source"] == "docs/design/foo/project/"
    assert dr.board_paths({}, "state/sizings/bar.yaml") == {
        "source": "docs/design/bar/project/",
        "harvest": "docs/design/bar/harvest/",
    }


def test_touchpoint_names_record_and_resubmit():
    text = dr.approval_touchpoint("state/sizings/bar.yaml")
    assert "design_approval" in text and "resubmit engine-ask" in text and "state/sizings/bar.yaml" in text


def test_verdict_emits_key_only_when_set():
    assert "design_route" not in ac.GateVerdict(arm="S", halt=None).to_json()
    out = ac.GateVerdict(arm=None, halt={"kind": ac.HALT_DESIGN_REVIEW}, design_route={"route": "none"}).to_json()
    assert out[dr.DESIGN_ROUTE_KEY] == {"route": "none"}
    assert ac.HALT_DESIGN_REVIEW == "design-review"
