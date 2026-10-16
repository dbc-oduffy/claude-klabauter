"""design_route contract: payloads, shape errors, bare-string wrap, board paths, GateVerdict key."""

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as ac
from coordinator_core.ops.dispatch_emit import design_route as dr

BOARD = "https://claude.ai/artifact/abc"
APPROVAL = {"by": "pm", "at": "2026-10-16T23:00:00Z"}


def _block(route="canvas", authority="PM authorizes", board_url=None, approval=None):
    return {"route": route, "authority": authority, "board_url": board_url, "approval": approval}


def test_absent_is_none_none():
    assert dr.read({}, "state/sizings/x.yaml") == (None, None)
    assert dr.read({"design_route": None}, "x.yaml") == (None, None)


def test_canvas_unapproved_payload():
    payload, bad = dr.read({"name": "n1", "design_route": _block()}, "state/sizings/x.yaml")
    assert bad is None
    assert payload == {
        "route": "canvas",
        "authority": "PM authorizes",
        "driver": dr.DRIVERS["canvas"],
        "board": {"source": "docs/design/n1/project/", "harvest": "docs/design/n1/harvest/", "url": None},
        "approved": False,
    }


def test_canvas_approved_payload_and_flag():
    sizing = {"name": "n1", "design_route": _block(board_url=BOARD, approval=APPROVAL)}
    payload, _ = dr.read(sizing, "s.yaml")
    assert payload["approved"] is True and payload["board"]["url"] == BOARD


def test_bare_string_reads_with_null_authority():
    payload, bad = dr.read({"name": "n1", "design_route": "canvas"}, "s.yaml")
    assert bad is None and payload["authority"] is None
    assert payload["approved"] is False and payload["board"]["url"] is None


@pytest.mark.parametrize("route", ["reviewer", "none"])
def test_non_canvas_payload_has_no_board(route):
    payload, bad = dr.read({"design_route": _block(route=route, authority=None)}, "s.yaml")
    assert bad is None
    assert payload == {"route": route, "authority": None, "driver": dr.DRIVERS[route]}


def test_wrap():
    assert dr.wrap("none") == {"route": "none", "authority": None, "board_url": None, "approval": None}
    block = _block(board_url=BOARD)
    assert dr.wrap(block) == block and dr.wrap(block) is not block


@pytest.mark.parametrize("block", ["canvas", "reviewer", "none", _block(), _block(board_url=BOARD, approval=APPROVAL)])
def test_valid_shapes(block):
    assert dr.block_errors(block) == []


@pytest.mark.parametrize(
    "block",
    [
        "bogus",
        7,
        _block(route="bogus"),
        _block(authority="  "),
        _block(authority={"by": "pm"}),
        _block(board_url=""),
        {"route": "canvas"},
        {**_block(), "extra_key": 1},
        _block(route="reviewer", board_url=BOARD, approval=APPROVAL),
        _block(board_url=BOARD, approval={**APPROVAL, "by": "apm"}),
        _block(board_url=BOARD, approval={**APPROVAL, "at": ""}),
        _block(board_url=BOARD, approval={**APPROVAL, "board_url": BOARD}),
        _block(board_url=BOARD, approval={"by": "pm"}),
        _block(approval=APPROVAL),
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


def test_touchpoint_names_record_call_and_refire():
    text = dr.approval_touchpoint("state/sizings/bar.yaml")
    for needle in ("dispatch.ask_gate", "design_approval", "board_url", "pm_ruling", "ruling_ref", "refire engine-ask", "state/sizings/bar.yaml"):
        assert needle in text
    assert "write" not in text.lower() and " into " not in text


def _payload(**block_extra):
    return dr.read({"name": "n1", "design_route": _block(**block_extra)}, "s.yaml")[0]


def test_stage_refusal_unapproved_canvas():
    text = dr.stage_refusal(_payload())
    assert text == (
        "design-review: route canvas, board docs/design/n1/project/; the PM has not approved the canvas board"
    )
    assert dr.STAGE_REFUSAL_PREFIX == "design-review:"
    assert "PM authorizes" not in text


def test_stage_refusal_none_otherwise():
    assert dr.stage_refusal(None) is None
    assert dr.stage_refusal(_payload(board_url=BOARD, approval=APPROVAL)) is None
    for route in ("reviewer", "none"):
        assert dr.stage_refusal(_payload(route=route, authority=None)) is None


def test_next_action_names_board_review_record_and_refire():
    text = dr.next_action("state/sizings/bar.yaml")
    for needle in ("Artifact", '"design"', "PM", "dispatch.ask_gate", "design_approval", "refire engine-ask", "state/sizings/bar.yaml"):
        assert needle in text
    assert "\n" not in text


def test_with_route_new_block_has_all_four_keys():
    assert dr.with_route(None, "reviewer") == {"route": "reviewer", "authority": None, "board_url": None, "approval": None}
    assert dr.with_route(None, "canvas", "q")["authority"] == "q"


def test_with_route_keeps_board_and_approval_only_when_route_unchanged():
    prior = _block(board_url=BOARD, approval=APPROVAL)
    kept = dr.with_route(prior, "canvas", "q")
    assert kept["board_url"] == BOARD and kept["approval"] == APPROVAL
    dropped = dr.with_route(prior, "reviewer", "q")
    assert dropped["board_url"] is None and dropped["approval"] is None
    assert dr.with_route("canvas", "canvas")["approval"] is None


def test_with_route_refuses_malformed():
    with pytest.raises(ValueError):
        dr.with_route(None, "bogus")
    with pytest.raises(ValueError):
        dr.with_route(None, "canvas", " ")


def test_verdict_emits_key_only_when_set():
    assert "design_route" not in ac.GateVerdict(arm="S", halt=None).to_json()
    out = ac.GateVerdict(arm=None, halt={"kind": ac.HALT_DESIGN_REVIEW}, design_route={"route": "none"}).to_json()
    assert out[dr.DESIGN_ROUTE_KEY] == {"route": "none"}
    assert ac.HALT_DESIGN_REVIEW == "design-review"
