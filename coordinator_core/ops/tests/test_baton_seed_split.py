"""Tests for the baton.seed_split op."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.ops import baton_seed_split as op

SEED = """---
kind: roadmap-seed
title: Women first renders
created: 2026-10-07
handoff_id: hnd-women-first-aaaaaa
deployment_state: awaiting_gate
deliverable_id: dlv-women-first-abcdef
blocking_notes: Gated on Example-Game-Repo's women's pipeline
blocked_by: []
---

## Scope

- men comparison view
- women render batch
- per-signal bars

## Anti-scope

- nothing public
"""


def _plain_rmw(path, mutate, *, repo_root):
    old = path.read_text(encoding="utf-8")
    new = mutate(old)
    if new != old:
        path.write_text(new, encoding="utf-8", newline="\n")
    return new


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))
    monkeypatch.setattr(op, "locked_rmw", _plain_rmw)


def _seed(root: Path, text: str = SEED) -> Path:
    p = root / "state" / "handoffs" / "2026-10-07-women.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_splits_the_gate_free_lines_and_links_both_ways(tmp_path):
    p = _seed(tmp_path)
    r = op._handler({"seed": "hnd-women-first-aaaaaa", "gate_free": ["men comparison", "per-signal"],
                     "today": "2026-10-08"}, tmp_path)
    assert r["exit_code"] == 0 and r["applied"] is True and len(r["moved"]) == 2
    new = tmp_path / r["new_seed"]
    nfm = _parse_frontmatter(new.read_text(encoding="utf-8"))
    assert nfm["deployment_state"] == "ready_to_fire" and nfm["kind"] == "roadmap-seed"
    assert nfm["split_from"] == "hnd-women-first-aaaaaa" and nfm["handoff_id"] == r["new_handoff_id"]
    assert not {"blocking_notes", "blocked_by", "deliverable_id"} & set(nfm)
    assert "- men comparison view" in new.read_text(encoding="utf-8")
    ofm = _parse_frontmatter(p.read_text(encoding="utf-8"))
    assert ofm["deployment_state"] == "awaiting_gate" and ofm["split_into"] == [r["new_handoff_id"]]
    body = p.read_text(encoding="utf-8")
    assert "men comparison" not in body.split("## Split")[0] and "women render batch" in body and "## Split" in body


def test_refuses_without_writing(tmp_path):
    p = _seed(tmp_path)
    cases = [
        ({"seed": "hnd-women-first-aaaaaa", "gate_free": ["not in the seed"]}, "not found in the seed body"),
        ({"seed": "hnd-women-first-aaaaaa", "gate_free": []}, "at least one"),
        ({"seed": "nope", "gate_free": ["x"]}, "seed not found"),
    ]
    for params, needle in cases:
        r = op._handler(params, tmp_path)
        assert r["exit_code"] == 1 and needle in r["error"]
    assert p.read_text(encoding="utf-8") == SEED
    assert [f.name for f in p.parent.iterdir()] == [p.name]


def test_refuses_a_seed_that_is_not_awaiting_gate(tmp_path):
    _seed(tmp_path, SEED.replace("awaiting_gate", "ready_to_fire"))
    r = op._handler({"seed": "hnd-women-first-aaaaaa", "gate_free": ["men"]}, tmp_path)
    assert r["exit_code"] == 1 and "not awaiting_gate" in r["error"]


def test_destination_collision_leaves_original_untouched(tmp_path):
    p = _seed(tmp_path)
    (p.parent / "2026-10-08-women-first-renders-gate-free-part.md").write_text("x", encoding="utf-8")
    r = op._handler({"seed": "hnd-women-first-aaaaaa", "gate_free": ["men"], "today": "2026-10-08"}, tmp_path)
    assert r["exit_code"] == 1 and "already exists" in r["error"]
    assert p.read_text(encoding="utf-8") == SEED
