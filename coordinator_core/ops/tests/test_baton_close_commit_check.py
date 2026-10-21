"""Pins `baton_close_commit_check`: the recheck table, the batched predicate, and leg (b) parity.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_baton_close_commit_check.py -q
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from coordinator_core import dag
from coordinator_core.ops import baton_close_commit_check as chk
from coordinator_core.ops import handoff_children as hc_mod
from coordinator_core.ops.baton_close_commit_contract import RefusalReason as R
from coordinator_core.ops.baton_close_report_decide import decide
from coordinator_core.ops.baton_close_report_readers import (
    read_baton_headers,
    successor_index,
)

PLAN = "docs/plans/p.md"


def _ev(done=True, status="implemented", cls="plan", target=PLAN):
    return {"class": cls, "field": "governing_plan", "target_path": target,
            "target_status": status, "done": done}


def _header(status="open"):
    return {"path": "state/handoffs/a.md", "status": status, "predecessor": None,
            "governing_plan": PLAN, "origin_plan_id": None, "sizing_object": None,
            "roadmap_id": None, "origin_goal_id": []}


# ---- recheck -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fresh, expected",
    [
        (("closable", [_ev()]), None),
        (("residue", "claimed", [_ev()]), R.CLAIMED),
        (("residue", "live_successor", [_ev()]), R.LIVE_SUCCESSOR),
        (("residue", "evidence_conflict", [_ev()]), R.EVIDENCE_STALE),
        (("residue", "target_missing", [_ev(done=False, status="target_missing")]), R.EVIDENCE_STALE),
        (("residue", "spinoff_kind", [_ev()]), R.PREDICATE_REFUSED),
        (("residue", "no_deliverable_id", [_ev()]), R.NO_DELIVERABLE_ID),
        (("no_evidence",), R.EVIDENCE_STALE),
        (("closable", [_ev(target="docs/plans/other.md")]), R.EVIDENCE_STALE),
        (("closable", [_ev(cls="sizing")]), R.EVIDENCE_STALE),
    ],
)
def test_recheck_table(fresh, expected):
    got = chk.recheck([_ev()], fresh)
    assert (got[0] if got else None) == expected
    if got:
        assert got[1]


def test_recheck_ignores_a_not_done_caller_item_and_extra_fresh_items():
    caller = [_ev(), _ev(done=False, status="draft", target="docs/plans/q.md")]
    fresh = ("closable", [_ev(), _ev(cls="goal", target="goal-x")])
    assert chk.recheck(caller, fresh) is None


# ---- select fixtures -----------------------------------------------------------------------


def _baton(root: Path, name: str, *, extra="", state="ready_to_fire", did="dlv-aaaaaa",
           status="open", crlf=False, kind=None) -> str:
    lines = [
        f'title: "T {name}"', "created: 2026-01-01", "branch: work/t/2026-01-01",
        f"status: {status}", 'predecessor: "none"', f"governing_plan: {PLAN}",
        f"deployment_state: {state}",
    ]
    if did:
        lines.append(f"deliverable_id: {did}")
    if kind:
        lines.append(f"kind: {kind}")
    if extra:
        lines.append(extra)
    text = "---\n" + "\n".join(lines) + "\n---\n\n# Handoff\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    d = root / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(text.encode("utf-8"))
    return f"state/handoffs/{name}"


def _world(root: Path, rels: list[str], ev=None):
    """Fresh close_report-style readings for `rels` under root."""
    headers, _ = read_baton_headers(str(root))
    texts = {}
    for p in sorted((root / "state" / "handoffs").glob("*.md")):
        texts[os.path.abspath(p)] = p.read_text(encoding="utf-8", errors="replace")
    fms = {r: dag._read_meta(str(root / r)) for r in rels}
    evidence = {r: [_ev()] for r in rels if r in headers}
    if ev:
        evidence.update(ev)
    return headers, successor_index(headers), evidence, fms, texts


def _select(root: Path, rels, rows=None, ev=None, session_id=""):
    (root / ".git").mkdir(exist_ok=True)
    headers, succ, evidence, fms, texts = _world(root, rels, ev)
    rows = rows if rows is not None else [{"handoff_path": r, "evidence": [_ev()]} for r in rels]
    return chk.select(rows, headers, succ, evidence, fms, texts, root / ".git", root, session_id)


def _reasons(refused):
    return {r["handoff_path"]: r["reason"] for r in refused}


# ---- select --------------------------------------------------------------------------------


def test_closable_baton_flips_with_its_own_deliverable_id(tmp_path):
    a = _baton(tmp_path, "a.md", did="dlv-own-111111")
    flips, refused, already = _select(tmp_path, [a])
    assert flips == [{"path": a, "deliverable_id": "dlv-own-111111"}]
    assert refused == [] and already == []


def test_crlf_baton_flips(tmp_path):
    a = _baton(tmp_path, "a.md", crlf=True)
    flips, refused, _ = _select(tmp_path, [a])
    assert [f["path"] for f in flips] == [a], refused


def test_every_refusal_reason_in_one_batch(tmp_path):
    ok = _baton(tmp_path, "ok.md", did="dlv-ok-000001")
    claimed = _baton(tmp_path, "claimed.md", status="claimed", state="in_flight")
    continued = _baton(tmp_path, "continued.md", state="continued")
    nodid = _baton(tmp_path, "nodid.md", did="")
    spin = _baton(tmp_path, "spin.md", kind="spinoff", did="dlv-spin-00001")
    stale = _baton(tmp_path, "stale.md", did="dlv-stale-00001")
    done = _baton(tmp_path, "done.md", state="shipped", status="closed")
    parent = _baton(tmp_path, "parent.md", did="dlv-parent-0001")
    _baton(tmp_path, "child.md", did="dlv-child-00001")
    (tmp_path / "state/handoffs/child.md").write_text(
        (tmp_path / "state/handoffs/child.md").read_text().replace(
            'predecessor: "none"', 'predecessor: "state/handoffs/parent.md"'),
        encoding="utf-8",
    )
    rels = [ok, claimed, continued, nodid, spin, stale, done, parent]
    rows = [{"handoff_path": r, "evidence": [_ev()]} for r in rels]
    rows += [{"handoff_path": "state/handoffs/ghost.md", "evidence": [_ev()]},
             {"handoff_path": None}, {"handoff_path": ok, "evidence": [_ev()]}]
    flips, refused, already = _select(
        tmp_path, rels, rows=rows,
        ev={stale: [_ev(done=False, status="draft")], claimed: [_ev()],
            spin: [{**_ev(), "field": "plan.deliverable_id"}]},
    )
    got = _reasons(refused)
    assert [f["path"] for f in flips] == [ok]
    assert got[claimed] == "claimed"
    assert got[continued] == "continued"
    assert got[nodid] == "no_deliverable_id"
    assert got[spin] == "predicate_refused"
    assert got[stale] == "evidence_stale"
    assert got[parent] == "live_successor"
    assert got["state/handoffs/ghost.md"] == "not_live"
    assert got[""] == "bad_row"
    assert [a["handoff_path"] for a in already] == [done]
    assert len(refused) + len(flips) + len(already) == len(rows) - 1  # duplicate dropped


def test_spinoff_closing_on_its_own_governing_plan_flips(tmp_path):
    a = _baton(tmp_path, "a.md", kind="spinoff", did="dlv-spin-00001")
    rows = [{"handoff_path": a, "evidence": [_ev()]}]
    flips, refused, _ = _select(tmp_path, [a], rows=rows)
    assert [f["path"] for f in flips] == [a], refused


def test_spinoff_closing_only_via_deliverable_join_refuses(tmp_path):
    a = _baton(tmp_path, "a.md", kind="spinoff", did="dlv-spin-00001")
    join = {**_ev(), "field": "plan.deliverable_id"}
    rows = [{"handoff_path": a, "evidence": [join]}]
    flips, refused, _ = _select(tmp_path, [a], rows=rows, ev={a: [join]})
    assert flips == [] and _reasons(refused) == {a: "predicate_refused"}


def test_non_dlv_spinoff_refuses_no_deliverable_id(tmp_path):
    a = _baton(tmp_path, "a.md", kind="spinoff", did="legacy-1")
    flips, refused, _ = _select(tmp_path, [a])
    assert flips == [] and _reasons(refused) == {a: "no_deliverable_id"}


def test_dlv_spinoff_with_continuation_successor_refuses_predicate_refused_naming_kind(tmp_path):
    a = _baton(tmp_path, "a.md", kind="spinoff", did="dlv-spin-00001",
               extra="handoff_id: hnd-spin-a1b2c3")
    _edge_baton(tmp_path, "child.md", "predecessor_id", "hnd-spin-a1b2c3")
    join = {**_ev(), "field": "plan.deliverable_id"}
    flips, refused, _ = _select(tmp_path, [a], ev={a: [join]})
    assert flips == []
    assert _reasons(refused) == {a: "predicate_refused"}
    assert "spinoff" in refused[0]["detail"]


def test_spinoff_on_own_plan_with_continuation_successor_refuses_live_successor(tmp_path):
    a = _baton(tmp_path, "a.md", kind="spinoff", did="dlv-spin-00001",
               extra="handoff_id: hnd-spin-a1b2c3")
    _edge_baton(tmp_path, "child.md", "predecessor_id", "hnd-spin-a1b2c3")
    flips, refused, _ = _select(tmp_path, [a])
    assert flips == [] and _reasons(refused) == {a: "live_successor"}


def test_handoff_kind_leg_d_applies_so_close_refusal_may_test_kind_unconditionally():
    assert chk._HANDOFF_KIND.predicate_legs["d"].applies


def test_unreadable_frontmatter_is_refused(tmp_path):
    a = _baton(tmp_path, "a.md")
    flips, _, _ = _select(tmp_path, [a])
    assert flips
    (tmp_path / "state/handoffs/a.md").write_text(
        (tmp_path / "state/handoffs/a.md").read_text().replace("title:", "title: [", 1),
        encoding="utf-8",
    )
    headers, succ, evidence, _, texts = _world(tmp_path, [a])
    flips, refused, _ = chk.select(
        [{"handoff_path": a, "evidence": [_ev()]}], headers, succ, evidence, {a: {}}, texts,
        tmp_path / ".git", tmp_path,
    )
    assert flips == [] and _reasons(refused) == {a: "unreadable_frontmatter"}


def test_live_successor_in_the_same_call_is_refused_by_pre_flip_corpus(tmp_path):
    parent = _baton(tmp_path, "parent.md", did="dlv-parent-0001")
    child = _baton(tmp_path, "child.md", did="dlv-child-00001")
    (tmp_path / "state/handoffs/child.md").write_text(
        (tmp_path / "state/handoffs/child.md").read_text().replace(
            'predecessor: "none"', 'predecessor: "state/handoffs/parent.md"'),
        encoding="utf-8",
    )
    flips, refused, _ = _select(tmp_path, [parent, child])
    assert [f["path"] for f in flips] == [child]
    assert _reasons(refused) == {parent: "live_successor"}


def test_empty_corpus_texts_fail_closed(tmp_path):
    a = _baton(tmp_path, "a.md")
    (tmp_path / ".git").mkdir(exist_ok=True)
    headers, succ, evidence, fms, _ = _world(tmp_path, [a])
    flips, refused, _ = chk.select(
        [{"handoff_path": a, "evidence": [_ev()]}], headers, succ, evidence, fms, {},
        tmp_path / ".git", tmp_path,
    )
    assert flips == [] and _reasons(refused) == {a: "predicate_refused"}


def test_live_claimant_refuses_and_own_claim_clears(tmp_path, monkeypatch):
    a = _baton(tmp_path, "a.md")
    import coordinator_core.ops.deliverable_cascade as casc
    monkeypatch.setattr(casc, "_claimant", lambda p, r: "sess-1")
    monkeypatch.setattr(casc, "resolve_live_session_ids", lambda: frozenset({"sess-1"}))
    flips, refused, _ = _select(tmp_path, [a])
    assert flips == [] and _reasons(refused) == {a: "predicate_refused"}
    flips, refused, _ = _select(tmp_path, [a], session_id="sess-1")
    assert [f["path"] for f in flips] == [a]


def test_select_reads_no_baton_file_it_was_not_handed(tmp_path):
    a = _baton(tmp_path, "a.md")
    before = (tmp_path / a).read_bytes()
    _select(tmp_path, [a])
    assert (tmp_path / a).read_bytes() == before


# ---- leg (b) parity against has_live_children_from_metas ----------------------------------


def _edge_baton(root, name, edge_key, edge_val, state="ready_to_fire", status="open"):
    text = (
        f'---\ntitle: "{name}"\ncreated: 2026-01-01\nbranch: work/t/2026-01-01\n'
        f'status: {status}\ndeployment_state: {state}\ndeliverable_id: dlv-succ-000001\n'
        f'{edge_key}: "{edge_val}"\n---\n\n# H\n'
    ).replace("\n", "\r\n" if name.startswith("crlf") else "\n")
    (root / "state" / "handoffs" / name).write_bytes(text.encode())


def test_leg_b_parity_with_the_unprefiltered_oracle(tmp_path):
    (tmp_path / ".git").mkdir()
    target = _baton(tmp_path, "target.md", extra="handoff_id: hnd-target-a1b2c3")
    for i in range(6):
        _baton(tmp_path, f"pad-{i}.md", did=f"dlv-pad-{i:06d}")
    cases = {
        "by-path.md": ("predecessor", "state/handoffs/target.md", "ready_to_fire", "open", True),
        "by-name.md": ("predecessor", "target.md", "ready_to_fire", "open", True),
        "crlf-by-id.md": ("predecessor_id", "hnd-target-a1b2c3", "ready_to_fire", "open", True),
        "terminal.md": ("predecessor", "target.md", "shipped", "closed", False),
        "continued.md": ("predecessor", "target.md", "continued", "open", False),
        "unrelated.md": ("predecessor", "state/handoffs/pad-1.md", "ready_to_fire", "open", False),
    }
    for name, (k, v, st, stat, _) in cases.items():
        _edge_baton(tmp_path, name, k, v, st, stat)

    rels = [target]
    headers, succ, evidence, fms, texts = _world(tmp_path, rels)
    oracle = asyncio.run(
        hc_mod.has_live_children_from_metas(
            str(tmp_path / target), tmp_path / ".git",
            edge_kinds=hc_mod.CONCLUSION_EDGE_KINDS, metas=None,
        )
    )
    mine = chk._leg_b_refusals([target], tmp_path, texts, fms)
    assert oracle["exit_code"] == 0 and target in mine
    expected = sorted(str(tmp_path / "state/handoffs" / n) for n, c in cases.items() if c[-1])
    assert sorted(oracle["children"]) == expected
    assert mine[target] == (
        f"has a live successor/continuation (referenced by: {sorted(oracle['children'])})"
    )


def test_leg_b_with_no_live_successor_matches_oracle_absent(tmp_path):
    (tmp_path / ".git").mkdir()
    target = _baton(tmp_path, "target.md")
    _baton(tmp_path, "pad.md", did="dlv-pad-000001")
    _, _, _, fms, texts = _world(tmp_path, [target])
    oracle = asyncio.run(
        hc_mod.has_live_children_from_metas(
            str(tmp_path / target), tmp_path / ".git",
            edge_kinds=hc_mod.CONCLUSION_EDGE_KINDS, metas=None,
        )
    )
    assert oracle["exit_code"] == 1
    assert chk._leg_b_refusals([target], tmp_path, texts, fms) == {}
