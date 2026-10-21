"""The completion-entry leg writes its entry once, with bytes equal to the four-write result."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_close_completion as rcc

PLAN = "docs/plans/p.md"
CRIT = {"status": "met", "completion_summary": "Shipped it for real."}


def _make_root(base: Path) -> Path:
    (base / ".git").mkdir(parents=True)
    (base / "docs" / "plans").mkdir(parents=True)
    (base / PLAN).write_text('---\ntitle: "Shipped the thing"\n---\n\nbody\n', encoding="utf-8")
    return base


@pytest.fixture
def pair(tmp_path):
    return _make_root(tmp_path / "a"), _make_root(tmp_path / "b")


def _four_writes(root: Path, run_id: str) -> str:
    from coordinator_core.ops.ceremony.completion_entry import (
        fill_authored_surfaces,
        fill_completion_entry_residues,
        scaffold_completion_entry,
    )

    title = "Shipped the thing"
    scaffold = scaffold_completion_entry(root, title)
    rel = scaffold["completion_entry_path"].replace("\\", "/")
    fill_authored_surfaces(root, rel, title=title, nature=None, prose=CRIT["completion_summary"])
    fill_completion_entry_residues(root, rel, "", chain_terminal=False, sid=rcc.AUTHORED_BY)
    assert rcc._stamp_run_id(root, rel, run_id)
    return rel


def test_leg_bytes_equal_four_write_golden_with_one_replace(pair, monkeypatch):
    leg_root, golden_root = pair
    golden_rel = _four_writes(golden_root, "r1")

    real = os.replace
    hits = []

    def spy(src, dst, *a, **k):
        hits.append(Path(dst).name)
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy)
    result, paths = rcc.completion_entry_leg(leg_root, "r1", PLAN, CRIT)
    monkeypatch.setattr(os, "replace", real)

    # The leg writes the per-run path; the golden's four writes land at the bare slug.
    assert result.status == "ran" and paths == [rcc._run_entry_rel("Shipped the thing", "r1")]
    entry_name = Path(paths[0]).name
    assert hits.count(entry_name) == 1
    assert (leg_root / paths[0]).read_bytes() == (golden_root / golden_rel).read_bytes()

    again, again_paths = rcc.completion_entry_leg(leg_root, "r1", PLAN, CRIT)
    assert again.status == "discharged" and again_paths == []


def test_refusal_leaves_no_entry(pair):
    root, _ = pair
    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, {"status": "met"})
    assert result.status == "failed" and paths == []
    assert not (root / "archive").exists() or not list((root / "archive").rglob("*.md"))
