"""A review wave's applied count comes from the landed tree: a ``handed-to-em`` (ASK) finding is never
applied, and a standing REQUIRES_CHANGES verdict names the findings still owed."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import terminal_commit_review as tcr

_SIDECAR = "reviewer.md"


def _sidecar(root: Path, rows: list, verdict: str = "REQUIRES_CHANGES") -> dict:
    ledger = json.dumps(rows)
    (root / _SIDECAR).write_text(
        f"---\nverdict: {verdict}\nfindings_ledger: {{rows: {len(rows)}, verified_at: 2026-11-10T00:00:00Z}}\n---\n\n"
        f"## Findings Ledger\n\n```json\n{ledger}\n```\n",
        encoding="utf-8", newline="",
    )
    return {"wave_sidecar_paths": [_SIDECAR], "fixes": len(rows), "prep": {"slice_files": ["a.py"]}}


def _seven(root: Path, *, ask_status: str = "handed-to-em") -> list:
    (root / "a.py").write_text("x = 'new'\n", encoding="utf-8", newline="")
    rows = [{"id": "finding-1", "file": "a.py", "before": "old", "after": "new", "status": "applied", "verified": True}]
    for n in range(2, 8):
        rows.append({
            "id": f"finding-{n}", "file": "a.py", "before": f"b{n}", "after": f"never-landed-{n}",
            "status": ask_status, "verified": True,
        })
    return rows


def test_one_applied_and_six_ask_counts_one_and_names_six_owed(tmp_path):
    review = _sidecar(tmp_path, _seven(tmp_path))
    applied, owed = tcr.review_applied_in_tree(tmp_path, review)
    assert applied == 1
    assert owed == [f"{_SIDECAR}:finding-{n}" for n in range(2, 8)]
    out = tcr._stage_returns(review, tmp_path)
    assert out["fixes_applied"] == 1
    assert len(out["review_owed"]) == 6
    assert "6 finding(s)" in tcr.review_owed_refusal(tmp_path, review)


def test_a_status_less_ask_row_with_proposed_text_is_not_in_the_tree_so_not_applied(tmp_path):
    rows = _seven(tmp_path)
    for r in rows[1:]:
        del r["status"]
    applied, owed = tcr.review_applied_in_tree(tmp_path, _sidecar(tmp_path, rows))
    assert (applied, len(owed)) == (1, 6)


def test_the_applied_fix_rows_helper_excludes_ask_open_and_em_rejected(tmp_path):
    rows = _seven(tmp_path)
    rows[2]["status"] = "open"
    rows[3]["status"] = "em-rejected"
    _sidecar(tmp_path, rows)
    text = (tmp_path / _SIDECAR).read_text(encoding="utf-8")
    assert [r["id"] for _, r in tcr._applied_fix_rows(text)] == ["finding-1"]


def test_an_approved_verdict_with_every_fix_present_refuses_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x = 'new'\n", encoding="utf-8", newline="")
    rows = [{"id": "finding-1", "file": "a.py", "before": "old", "after": "new", "status": "applied", "verified": True}]
    review = _sidecar(tmp_path, rows, verdict="APPROVED")
    assert tcr.review_applied_in_tree(tmp_path, review) == (1, [])
    assert tcr.review_owed_refusal(tmp_path, review) is None


def test_requires_changes_with_all_fixes_in_tree_and_rejected_rows_owes_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x = 'new'\n", encoding="utf-8", newline="")
    rows = [
        {"id": "finding-1", "file": "a.py", "before": "old", "after": "new", "status": "applied", "verified": True},
        {"id": "finding-2", "file": "a.py", "before": "q", "after": "r", "status": "em-rejected", "verified": True, "reason": "no"},
    ]
    review = _sidecar(tmp_path, rows)
    assert tcr.review_owed_refusal(tmp_path, review) is None


def test_an_applied_row_whose_text_is_absent_from_the_tree_is_owed(tmp_path):
    (tmp_path / "a.py").write_text("x = 'old'\n", encoding="utf-8", newline="")
    rows = [{"id": "finding-1", "file": "a.py", "before": "old", "after": "new", "status": "applied", "verified": True}]
    review = _sidecar(tmp_path, rows)
    assert tcr.review_applied_in_tree(tmp_path, review) == (0, [f"{_SIDECAR}:finding-1"])


def test_without_a_readable_ledger_the_reviewer_count_stands(tmp_path):
    review = {"wave_sidecar_paths": ["missing.md"], "fixes": 3, "prep": {}}
    assert tcr.review_applied_in_tree(tmp_path, review) is None
    assert tcr._stage_returns(review, tmp_path)["fixes_applied"] == 3
    assert tcr.review_owed_refusal(tmp_path, review) is None


def test_the_landing_gate_refuses_a_standing_requires_changes_on_any_arm(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    review = _sidecar(tmp_path, _seven(tmp_path))
    out = refusals._tests_not_run_refusal(tmp_path, {"arm": "s"}, None, review)
    assert out["committed"] is False
    assert out["refused"] == refusals.REFUSED_REVIEW_OWED
    assert "6 finding(s)" in out["error"]


def test_the_landing_gate_passes_an_approved_review(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    (tmp_path / "a.py").write_text("x = 'new'\n", encoding="utf-8", newline="")
    rows = [{"id": "finding-1", "file": "a.py", "before": "old", "after": "new", "status": "applied", "verified": True}]
    review = _sidecar(tmp_path, rows, verdict="APPROVED")
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest

    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), plan_path=None, version=1)
    assert refusals._tests_not_run_refusal(tmp_path, {"arm": "s"}, request, review) is None
