"""Pins that the per-firing review record's home survives every reap leg.

The home is the plan's `<plan-stem>.evidence.yaml` `reviews:` list (APM ruling
2026-10-16-item-14-review-record-home), not a standalone record file. The prime exit criterion words the record as JSON;
the ruled home is a YAML list entry.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import reap_integrated_findings as integrated
from coordinator_core.ops.fleet import reap_unintegrated_findings as unintegrated
from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path

_OLD_DAYS = 400


_OLD_DATE = (datetime.now(timezone.utc).date() - timedelta(days=_OLD_DAYS)).isoformat()


def _old_date() -> str:
    return _OLD_DATE


def _age(path: Path) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=_OLD_DAYS)).timestamp()
    os.utime(path, (ts, ts))


def _home(root: Path) -> Path:
    plan = root / "docs" / "plans" / f"{_old_date()}-aged-plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# plan\n", encoding="utf-8")
    home = evidence_sidecar_path(plan)
    home.write_text(
        "reviews:\n- {leg: code-review, plan_id: p, findings_count: 1,"
        " max_severity: minor, applied: 1}\n",
        encoding="utf-8",
    )
    _age(plan)
    _age(home)
    return home


def _decoys(root: Path) -> tuple[Path, Path]:
    rest = root / "state" / "review-trail" / f"{_old_date()}-decoy.json"
    rest.parent.mkdir(parents=True, exist_ok=True)
    rest.write_text("{}", encoding="utf-8")
    finding = (
        root / ".coordinator-local" / "review-trail" / "findings"
        / f"{_old_date()}-decoy.md"
    )
    finding.parent.mkdir(parents=True, exist_ok=True)
    finding.write_text("# Findings\n\nunintegrated\n", encoding="utf-8")
    _age(rest)
    _age(finding)
    return rest, finding


def _dry(module, handler_name: str, root: Path, monkeypatch) -> set[str]:
    monkeypatch.setattr(module, "main_worktree_root", lambda _common: root)
    monkeypatch.setattr(module, "check_repo_root", lambda _param, _common: None)
    handler = getattr(module, handler_name)
    result = asyncio.run(handler({"dry_run": True}, repo_root=root / ".git"))
    assert result["exit_code"] == 0 and result["dry_run"] is True
    return {c["id"] for c in result["candidates"]}


@pytest.mark.parametrize(
    ("module", "handler", "decoy_index"),
    [
        (unintegrated, "_handler_review_trail_rest", 0),
        (unintegrated, "_handler", 1),
        # No positive control: a marker-present decoy is not seeded here, so this
        # row proves absence only if the leg's scan can emit candidates at all.
        (integrated, "_handler", None),
    ],
)
def test_record_home_is_never_a_reap_candidate(
    tmp_path, monkeypatch, module, handler, decoy_index
):
    home = _home(tmp_path)
    decoys = _decoys(tmp_path)

    ids = _dry(module, handler, tmp_path, monkeypatch)

    assert home.relative_to(tmp_path).as_posix() not in ids
    assert not any(i.endswith(".evidence.yaml") for i in ids)
    if decoy_index is not None:
        assert decoys[decoy_index].relative_to(tmp_path).as_posix() in ids, (
            "positive control: the leg must still reap its own aged decoy"
        )
    assert home.exists()
