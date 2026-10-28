"""A spinoff chain's close resolves the consumed (head) handoff's plan, never the chain root's.

Pins: brief() over a root->head chain naming different plans resolves the head's plan, claims and
stamps only that plan, and emits the `governing-plan-root-vs-consumed` point naming both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony import wsc_disposition

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

POINT_ID = "governing-plan-root-vs-consumed"
ROOT_PLAN = "docs/plans/2026-10-01-root-peer-plan.md"
HEAD_PLAN = "docs/plans/2026-10-02-head-own-plan.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _chain(tmp_path: Path, root_plan: str | None, head_plan: str | None) -> tuple[str, str]:
    for plan in (ROOT_PLAN, HEAD_PLAN):
        _write(tmp_path / plan, f"# {plan}\n")
    root_rel = "tasks/handoffs/2026-10-01-root.md"
    head_rel = "tasks/handoffs/2026-10-02-head.md"
    for rel, plan in ((root_rel, root_plan), (head_rel, head_plan)):
        gp = f"governing_plan: {plan}\n" if plan else ""
        _write(tmp_path / rel, f"---\ntitle: h\n{gp}---\nbody\n")
    return root_rel, head_rel


def _brief(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: tuple[str, ...]) -> dict:
    gate = wsc.SessionShapeGate(
        sid="testsid-root-vs-consumed",
        disposition=wsc_disposition.PREDECESSOR_CONSUMED,
        consumed_handoff=paths[0],
        diagnostics=[],
        consumed_handoff_paths=paths,
        detection={},
    )
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root, history=None: gate)
    return wsc.brief(decisions={}, repo_root=tmp_path)


def _point(brief: dict) -> dict | None:
    return next((jp for jp in brief["judgment_points"] if jp["id"] == POINT_ID), None)


def _directive_text(brief: dict) -> str:
    return repr([d for d in brief["directives"] if d["id"] in ("d-claim-plan-execution-lock", "d-stamp-plan-implemented")])


def test_disagreeing_chain_resolves_head_and_emits_point(monkeypatch, tmp_path):
    root_rel, head_rel = _chain(tmp_path, ROOT_PLAN, HEAD_PLAN)
    brief = _brief(monkeypatch, tmp_path, (root_rel, head_rel))
    resolution = brief["preflight"]["governing_plan_resolution"]
    assert resolution["source"] == "handoff_frontmatter"
    assert Path(resolution["path"]).name == Path(HEAD_PLAN).name
    text = _directive_text(brief)
    assert Path(HEAD_PLAN).name in text
    assert Path(ROOT_PLAN).name not in text
    point = _point(brief)
    assert point is not None
    assert point["recommendation"] is None
    assert ROOT_PLAN in point["evidence"] and HEAD_PLAN in point["evidence"]


def test_agreeing_chain_emits_no_point(monkeypatch, tmp_path):
    root_rel, head_rel = _chain(tmp_path, HEAD_PLAN, HEAD_PLAN)
    brief = _brief(monkeypatch, tmp_path, (root_rel, head_rel))
    assert _point(brief) is None


def test_single_handoff_emits_no_point(monkeypatch, tmp_path):
    root_rel, _ = _chain(tmp_path, ROOT_PLAN, HEAD_PLAN)
    assert _point(_brief(monkeypatch, tmp_path, (root_rel,))) is None


def test_head_without_plan_does_not_fall_back_to_root(monkeypatch, tmp_path):
    root_rel, head_rel = _chain(tmp_path, ROOT_PLAN, None)
    brief = _brief(monkeypatch, tmp_path, (root_rel, head_rel))
    assert brief["preflight"]["governing_plan_resolution"]["source"] == "none"
    assert Path(ROOT_PLAN).name not in _directive_text(brief)
    assert _point(brief) is not None
