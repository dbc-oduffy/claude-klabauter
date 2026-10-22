"""Tests for the XS arm spine mint."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine
from coordinator_core.ops.dispatch_emit.spine_read import read_spine

SIZING_REL = "state/sizings/2026-10-01-xs-thing.yaml"
SIZING = {
    "deliverable_id": "dlv-xs-thing-abc123",
    "intent": "Fix the thing\nmore detail here",
    "exit_criterion": {"statement": "the thing passes: 'quoted' text", "accepted": {"pm_quote": "go"}},
}


def test_mint_round_trips_to_one_dispatchable_row(tmp_path):
    text, path = mint_xs_spine(
        SIZING, sizing_rel=SIZING_REL, writes=["a/b.py", "a/test_b.py"], out_dir=tmp_path
    )
    assert path == tmp_path / "2026-10-01-xs-thing.spine.md"
    path.write_text(text, encoding="utf-8", newline="\n")
    rows = read_spine(path)
    assert len(rows) == 1
    assert rows[0].id == "X1"
    assert list(rows[0].writes) == ["a/b.py", "a/test_b.py"]
    assert "the thing passes" in rows[0].body


def test_plan_body_beside_code_routes_to_sidecar_and_body_says_so(tmp_path):
    text, path = mint_xs_spine(
        SIZING,
        sizing_rel=SIZING_REL,
        writes=["src/x.py", "docs/plans/2026-10-14-p.md", "docs/plans/2026-10-14-p.md"],
        out_dir=tmp_path,
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    row = read_spine(path)[0]
    assert list(row.writes) == ["src/x.py", "docs/plans/2026-10-14-p.evidence.yaml"]
    assert (
        "docs/plans/2026-10-14-p.md is routed to docs/plans/2026-10-14-p.evidence.yaml: "
        "record plan evidence with coordinator-invoke plan.tasks.mutate evidence-append"
    ) in row.body


def test_problems_body_and_plans_only_sets_are_left_as_declared(tmp_path):
    text, path = mint_xs_spine(
        SIZING, sizing_rel=SIZING_REL, writes=["src/x.py", "docs/problems/2026-10-14-q.md"], out_dir=tmp_path
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    assert list(read_spine(path)[0].writes) == ["src/x.py", "docs/problems/2026-10-14-q.md"]
    text, path = mint_xs_spine(
        SIZING, sizing_rel=SIZING_REL, writes=["docs/plans/2026-10-14-p.md"], out_dir=tmp_path
    )
    assert "routed to" not in text
    assert "docs/plans/2026-10-14-p.md" in text


def test_frontmatter_cites_sizing_and_statement(tmp_path):
    text, _ = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.md"], out_dir=tmp_path)
    assert f"sizing_object: {SIZING_REL}" in text
    assert "deliverable_id: dlv-xs-thing-abc123" in text
    assert "run_id: 2026-10-01-xs-thing" in text
    assert "the thing passes: 'quoted' text" in text


def test_deliverable_id_omitted_when_absent(tmp_path):
    sizing = {k: v for k, v in SIZING.items() if k != "deliverable_id"}
    text, _ = mint_xs_spine(sizing, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path)
    assert "deliverable_id" not in text


def test_glob_writes_refused(tmp_path):
    with pytest.raises(im.GlobFootprintError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a/*.py"], out_dir=tmp_path)


def test_directory_writes_refused(tmp_path):
    with pytest.raises(im.DirectoryShapedFootprintError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a/dir/"], out_dir=tmp_path)


def test_empty_writes_refused(tmp_path):
    with pytest.raises(ValueError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=[], out_dir=tmp_path)


def test_minted_spine_derives_lightweight_review_tier(tmp_path):
    from coordinator_core.ops.dispatch_emit.emit import derive_review_tier

    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text("estimate:\n  tshirt: XS\n", encoding="utf-8")
    text, path = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path)
    assert derive_review_tier(path, repo_root=tmp_path, plan_text=text) == "lightweight"


def test_declared_gated_row_is_minted_withheld_never_dropped(tmp_path):
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import gated_rows
    from coordinator_core.ops.plan_tasks_render import load_rows

    gated = [{"title": "DoE dead-op prose", "owner_repo": "coordinator-content-repo", "requires": "landed-work"}]
    text, path = mint_xs_spine(
        SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path, gated=gated
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    exclusions: list = []
    rows = read_spine(path, exclusions=exclusions)
    assert [r.id for r in rows] == ["X1"]
    raw = {r["id"]: r for r in load_rows(text).rows}
    withheld = gated_rows(exclusions, raw)
    assert [(g.id, g.reason, g.owner_repo) for g in withheld] == [("X2", "external_gate", "coordinator-content-repo")]


def _gate_of(text: str, row_id: str) -> dict:
    from coordinator_core.ops.plan_tasks_render import load_rows

    raw = {r["id"]: r for r in load_rows(text).rows}
    return raw[row_id]["external_gate"][0]


def test_landed_work_gate_is_minted_with_a_deterministic_closure_key(tmp_path):
    gated = [{"title": "peer lands", "owner_repo": "coordinator-content-repo", "requires": "landed-work"}]
    text, _ = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path, gated=gated)
    again, _ = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path, gated=gated)
    key = _gate_of(text, "X2")["closure_key"]
    assert key == {"kind": "deliverable", "id": "dlv-xs-thing-abc123-x2"}
    assert _gate_of(again, "X2")["closure_key"] == key


def test_declared_closure_key_is_kept_and_malformed_one_is_replaced(tmp_path):
    good = {"title": "t", "owner_repo": "r", "closure_key": {"kind": "memo-thread", "id": "m.md"}}
    bad = {"title": "t", "owner_repo": "r", "closure_key": {"kind": "bogus", "id": "m"}}
    text, _ = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py"], out_dir=tmp_path, gated=[good, bad])
    assert _gate_of(text, "X2")["closure_key"] == {"kind": "memo-thread", "id": "m.md"}
    assert _gate_of(text, "X3")["closure_key"]["kind"] == "deliverable"


def test_closure_key_falls_back_to_plan_id_without_a_deliverable_id():
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import _landed_work_closure_key, xs_spine_plan_id

    stem = "2026-10-01-xs-thing"
    assert _landed_work_closure_key({}, {}, stem, "X2") == {
        "kind": "deliverable",
        "id": f"{xs_spine_plan_id(stem)}-x2",
    }


MEMO = ".coordinator-local/memo-outbox/engine-asks.md"
MEMO_SIZING = {
    "intent": "Ask the engine",
    "exit_criterion": {"statement": "the engine-asks memo is sent to claude-klabauter-em."},
}


def _spine_rows(tmp_path, sizing, writes, **kw):
    text, path = mint_xs_spine(sizing, sizing_rel=SIZING_REL, writes=writes, out_dir=tmp_path, **kw)
    path.write_text(text, encoding="utf-8", newline="\n")
    return read_spine(path), _raw_memo_to(text)


def _raw_memo_to(text):
    from coordinator_core.ops.plan_tasks_render import load_rows

    return {r["id"]: r.get("memo_to") for r in load_rows(text).rows}


def test_memo_draft_splits_into_memo_send_row_with_receiver_from_exit_criterion(tmp_path):
    rows, to = _spine_rows(tmp_path, MEMO_SIZING, ["a/ev.yaml", MEMO, "h/handoff.md"])
    by_id = {r.id: r for r in rows}
    assert list(by_id["X1"].writes) == ["a/ev.yaml", "h/handoff.md"]
    assert list(by_id["M1"].writes) == [MEMO]
    assert to["M1"] == "claude-klabauter-em" and to["X1"] is None


def test_sizing_memo_to_wins_over_draft_and_criterion(tmp_path):
    (tmp_path / ".coordinator-local" / "memo-outbox").mkdir(parents=True)
    (tmp_path / MEMO).write_text("---\nto: draft-em\n---\nbody\n", encoding="utf-8")
    rows, to = _spine_rows(tmp_path, {**MEMO_SIZING, "memo_to": "sizing-em"}, [MEMO], repo_root=tmp_path)
    assert [r.id for r in rows] == ["M1"] and to["M1"] == "sizing-em"


def test_draft_to_frontmatter_beats_exit_criterion(tmp_path):
    (tmp_path / ".coordinator-local" / "memo-outbox").mkdir(parents=True)
    (tmp_path / MEMO).write_text("---\nto: draft-em\n---\nbody\n", encoding="utf-8")
    _, to = _spine_rows(tmp_path, MEMO_SIZING, ["a.py", MEMO], repo_root=tmp_path)
    assert to["M1"] == "draft-em"


def test_unresolvable_receiver_is_a_named_refusal(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import MemoReceiverUnresolvedError

    with pytest.raises(MemoReceiverUnresolvedError):
        mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py", MEMO], out_dir=tmp_path)


def test_backslash_memo_path_splits_identically(tmp_path):
    rows, _ = _spine_rows(tmp_path, MEMO_SIZING, ["a.py", MEMO.replace("/", "\\")])
    assert {r.id: list(r.writes) for r in rows} == {"X1": ["a.py"], "M1": [MEMO]}


def test_sent_receipt_is_not_a_draft(tmp_path):
    receipt = ".coordinator-local/memo-outbox/sent/engine-asks.md"
    text, _ = mint_xs_spine(SIZING, sizing_rel=SIZING_REL, writes=["a.py", receipt], out_dir=tmp_path)
    assert "memo_to" not in text
