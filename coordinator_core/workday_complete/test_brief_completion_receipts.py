"""coordinator_core.workday_complete.test_brief_completion_receipts — the day's receipts gate.

Run scoped only:
    python -m pytest coordinator_core/workday_complete/test_brief_completion_receipts.py -q
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.completion_receipts.day import receipts_for_day
from coordinator_core.workday_complete import brief as wc_brief

DAY = "2026-10-01"


def _receipt(root: Path, rid: str, *, concluded: str = f"{DAY}T10:00:00Z", supersedes=None,
             verdict="agent-delivered") -> None:
    path = root / "state/completion-receipts" / concluded[:7] / f"{rid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "schema: completion-receipt",
        f"receipt_id: {rid}",
        f"concluded_at: '{concluded}'",
        "baton_id: bat-1",
        "deliverable_id: null",
        f"verdict: {verdict}",
    ]
    if supersedes:
        lines.append(f"supersedes: {supersedes}")
    lines += ["---", "", "prose", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _entry(root: Path, name: str, rid: str) -> None:
    path = root / "archive/completed" / DAY[:7] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: x\nreceipts:\n  - receipt_id: {rid}\n    path: p\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_one_covered_one_uncovered(tmp_path):
    _receipt(tmp_path, "rcp-a-000001")
    _receipt(tmp_path, "rcp-b-000002")
    _entry(tmp_path, "e.md", "rcp-a-000001")
    out = receipts_for_day(tmp_path, DAY)
    assert out["uncovered"] == ["rcp-b-000002"]
    assert [r["receipt_id"] for r in out["receipts"]] == ["rcp-a-000001", "rcp-b-000002"]
    assert out["receipts"][0]["baton_id"] == "bat-1"


def test_superseded_listed_only_as_head(tmp_path):
    _receipt(tmp_path, "rcp-a-000001")
    _receipt(tmp_path, "rcp-a-000002", supersedes="rcp-a-000001", verdict="human-approved")
    out = receipts_for_day(tmp_path, DAY)
    assert [r["receipt_id"] for r in out["receipts"]] == ["rcp-a-000002"]


def test_other_day_excluded(tmp_path):
    _receipt(tmp_path, "rcp-a-000001", concluded="2026-10-02T10:00:00Z")
    assert receipts_for_day(tmp_path, DAY)["receipts"] == []


def test_absent_dir_yields_empty(tmp_path):
    assert receipts_for_day(tmp_path, DAY) == {"day": DAY, "receipts": [], "uncovered": []}


def test_gate_present_on_real_brief(monkeypatch):
    monkeypatch.setattr(
        wc_brief,
        "resolve_operator_config",
        lambda env=None: {"settings_home": "", "claude_klabauter_bin": "", "claude_klabauter_root": "", "content_root": ""},
    )
    monkeypatch.setattr(
        wc_brief,
        "_compute_open_day_goals",
        lambda: {"today": [], "stale": [], "unreadable_error": None},
    )
    seen = {}

    def fake(for_date):
        seen["d"] = for_date
        return {"day": for_date, "receipts": [], "uncovered": []}

    monkeypatch.setattr(wc_brief, "_compute_completion_receipts_gate", fake)
    _, envelope = wc_brief.brief(decisions={}, for_date=DAY)
    assert envelope["gates"]["completion_receipts"]["day"] == DAY
    assert seen["d"] == DAY


def test_read_failure_lands_as_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(wc_brief, "receipts_for_day", boom)
    monkeypatch.setattr(wc_brief, "_resolve_repo_common_dir_for_ceremony", lambda: Path("."))
    monkeypatch.setattr(wc_brief, "main_worktree_root", lambda d: Path("."))
    assert wc_brief._compute_completion_receipts_gate(DAY) == {"error": "nope"}
