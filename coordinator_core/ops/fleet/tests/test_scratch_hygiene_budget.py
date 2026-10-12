"""fleet.scratch_hygiene is bounded by time: past the budget an entry is reported, not walked."""

from __future__ import annotations

import os
import time

from coordinator_core.ops.fleet import scratch_hygiene as op
from coordinator_core.ops.fleet.scratch_hygiene_plan import classify_entry


def _old_tree(root, name, files=3):
    d = root / "scratch" / name
    d.mkdir(parents=True)
    old = time.time() - 30 * 86400
    for i in range(files):
        f = d / f"f{i}.txt"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (old, old))
    os.utime(d, (old, old))
    return d


def test_entry_past_the_deadline_is_reported_unwalked(tmp_path):
    entry = _old_tree(tmp_path, "a")
    rec = classify_entry(entry, repo_root=tmp_path, deadline=time.monotonic() - 1)
    assert (rec["action"], rec["reason"], rec["files"]) == ("skipped-budget", "budget-exhausted", 0)
    assert entry.exists()


def test_zero_budget_apply_deletes_nothing_and_exits_one(tmp_path):
    entry = _old_tree(tmp_path, "a")
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": True, "budget_secs": 0})
    assert [r["action"] for r in out["records"] if r.get("kind") == "purge" and r["path"] == "scratch/a"] == ["skipped-budget"]
    assert out["contract_exit"] == 1
    assert entry.exists()


def test_apply_within_budget_deletes_the_gated_entry(tmp_path):
    entry = _old_tree(tmp_path, "a")
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": True, "budget_secs": 30})
    assert [r["action"] for r in out["records"] if r.get("kind") == "purge" and r["path"] == "scratch/a"] == ["deleted"]
    assert not entry.exists()


def test_negative_budget_is_a_bad_argument(tmp_path):
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "budget_secs": -1})
    assert out["exit_code"] == 2


def test_a_top_level_file_entry_is_deleted(tmp_path):
    (tmp_path / "scratch").mkdir()
    f = tmp_path / "scratch" / "probe.txt"
    f.write_text("x", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(f, (old, old))
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": True, "budget_secs": 30})
    assert [r["action"] for r in out["records"] if r.get("kind") == "purge"] == ["deleted"]
    assert not f.exists()
