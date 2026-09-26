"""
IBMDT-C22 item 19: `readers_debt` carries no improvement-queue clustering
leg and no judgment-point item disposing of surviving improvement-queue
entries under the four queue-terminus outcome classes — only a pre-fire
open-row count.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.backlog_grind_assemble import readers_debt


def _collect_debt_triage(
    tmp_path: Path, monkeypatch, *, records_by_family: dict[str, list[dict]] | None = None
) -> object:
    records_by_family = records_by_family or {}
    monkeypatch.setattr(readers_debt, "_wt_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        readers_debt,
        "load_family_records",
        lambda family, repo_root: records_by_family.get(family, []),
    )
    return readers_debt.collect("debt-triage")


def _the_jp(result) -> dict:
    for jp in result.judgment_points:
        if jp["id"] == "j-debt-triage-batched-pm-gate":
            return jp
    raise AssertionError("j-debt-triage-batched-pm-gate not found")


def test_cluster_candidates_helper_is_gone():
    assert not hasattr(readers_debt, "_cluster_candidates")


def test_no_clustering_import_left_behind():
    assert "detect_candidates" not in dir(readers_debt)
    assert not hasattr(readers_debt, "_SUPPRESSED_CLUSTER_SIGNAL")


def test_question_carries_no_item_five(tmp_path: Path, monkeypatch) -> None:
    result = _collect_debt_triage(tmp_path, monkeypatch)
    jp = _the_jp(result)
    question = jp["question"]
    assert "(5)" not in question
    assert "surviving" not in question
    assert "queue-terminus" not in question
    for expected in (
        "(1) approve closing",
        "(2) YAGNI/scope",
        "(3) prioritize immediate-action",
        "(4) agree deferral reasoning",
    ):
        assert expected in question


def test_evidence_carries_no_cluster_summary_only_open_row_count(
    tmp_path: Path, monkeypatch
) -> None:
    improvement_records = [
        {"path": "state/improvement-queue/a.yaml", "frontmatter": {}},
        {"path": "state/improvement-queue/b.yaml", "frontmatter": {}},
        {"path": "state/improvement-queue/c.yaml", "frontmatter": {"status": "closed"}},
    ]
    result = _collect_debt_triage(
        tmp_path, monkeypatch, records_by_family={"improvement-queue": improvement_records}
    )
    jp = _the_jp(result)
    evidence = jp["evidence"]
    # exactly the pre-fire open-row count -- two open, one closed and excluded.
    assert "improvement-queue open=2" in evidence
    assert "cluster" not in evidence
    assert "MIN_CLUSTER_SIZE" not in evidence


def test_collect_still_only_fires_on_debt_triage_cadence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(readers_debt, "_wt_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(readers_debt, "load_family_records", lambda *a, **k: [])
    for cadence in ("mise-en-place", "bug-sweep", "bug-blitz", "dogfood"):
        result = readers_debt.collect(cadence)
        assert result.directives == []
        assert result.judgment_points == []
