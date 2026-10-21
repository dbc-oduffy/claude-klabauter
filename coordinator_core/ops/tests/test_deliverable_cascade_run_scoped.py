"""
coordinator_core.ops.tests.test_deliverable_cascade_run_scoped -- `cascade_run`
advances only the records a run's manifest names and reads no other sizing.

Run (from repo root):
    python -m pytest coordinator_core/ops/tests/test_deliverable_cascade_run_scoped.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _init_repo,
    _seed_handoff,
    _seed_sizing,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DID = "dlv-run-scoped-000000"
_PLAN = "docs/plans/2026-01-01-run-scoped.md"
_CORPUS = 1250


def _seed_run(repo: Path) -> list:
    _seed_sizing(repo, "20260101-run-a.yaml", deliverable_id=_DID)
    _seed_sizing(repo, "20260101-run-b-slice.yaml", deliverable_id=_DID)
    return ["state/sizings/20260101-run-a.yaml", "state/sizings/20260101-run-b-slice.yaml"]


def _status(repo: Path, rel: str) -> str:
    return yaml.safe_load((repo / rel).read_text(encoding="utf-8"))["status"]


def test_cascade_run_reads_only_the_named_records(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rels = _seed_run(repo)
    for i in range(_CORPUS):
        _seed_sizing(repo, f"20250101-pad-{i:04d}.yaml", deliverable_id=f"dlv-pad-{i:06d}")

    reads: list = []
    original = cascade_mod._read_sizing_meta

    def _counting(file_path):
        reads.append(str(file_path))
        return original(file_path)

    object.__setattr__(cascade_mod._SIZING_KIND, "reader", _counting)
    try:
        result = cascade_mod.cascade_run(
            repo, _DID, rels, "abc1234", repo_root=repo / ".git", plan_path=_PLAN
        )
    finally:
        object.__setattr__(cascade_mod._SIZING_KIND, "reader", original)

    assert len(result["advanced"]) == 2
    assert 0 < len(reads) <= len(rels)
    assert all("pad-" not in r for r in reads)
    assert sorted(result["mutated_paths"]) == sorted(rels)


def test_cascade_run_outcome_equals_cascade_terminal_sync(tmp_path):
    outcomes = {}
    for arm in ("sync", "run"):
        repo = tmp_path / arm
        _init_repo(repo)
        rels = _seed_run(repo)
        _seed_sizing(repo, "20250101-other.yaml", deliverable_id="dlv-other-000000")
        if arm == "sync":
            result = cascade_mod.cascade_terminal_sync(
                {
                    "deliverable_id": _DID,
                    "source_kind": "plan",
                    "source_path": _PLAN,
                    "target_kind": "sizing",
                },
                repo_root=repo / ".git",
            )
        else:
            result = cascade_mod.cascade_run(
                repo, _DID, rels, "abc1234", repo_root=repo / ".git", plan_path=_PLAN
            )
        outcomes[arm] = (
            result["exit_code"],
            sorted(Path(e["path"]).name for e in result["advanced"]),
            sorted(e["message"] for e in result["advanced"]),
            result["refused"],
            result["already_advanced"],
            result["scan_incomplete"],
            [(r, _status(repo, r), yaml.safe_load((repo / r).read_text())["plan"]) for r in rels],
            _status(repo, "state/sizings/20250101-other.yaml"),
        )
    assert outcomes["run"] == outcomes["sync"]
    assert outcomes["run"][-1] == "routed"


def test_cascade_run_second_call_is_a_noop(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rels = _seed_run(repo)
    cascade_mod.cascade_run(repo, _DID, rels, "abc1234", repo_root=repo / ".git", plan_path=_PLAN)
    again = cascade_mod.cascade_run(
        repo, _DID, rels, "abc1234", repo_root=repo / ".git", plan_path=_PLAN
    )
    assert again["advanced"] == []
    assert again["candidates_matched"] == 0


def test_cascade_run_names_unreadable_and_out_of_kind_records(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rels = _seed_run(repo)
    result = cascade_mod.cascade_run(
        repo,
        _DID,
        rels + ["state/sizings/missing.yaml", "docs/plans/x.md"],
        "abc1234",
        repo_root=repo / ".git",
        plan_path=_PLAN,
    )
    assert len(result["advanced"]) == 2
    assert result["scan_incomplete"] is True
    assert {u["path"] for u in result["unreadable"]} == {
        "state/sizings/missing.yaml",
        "docs/plans/x.md",
    }


def test_cascade_run_skips_a_record_of_another_deliverable(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-foreign.yaml", deliverable_id="dlv-foreign-000000")
    result = cascade_mod.cascade_run(
        repo,
        _DID,
        ["state/sizings/20260101-foreign.yaml"],
        "abc1234",
        repo_root=repo / ".git",
        plan_path=_PLAN,
    )
    assert result["candidates_matched"] == 0
    assert _status(repo, "state/sizings/20260101-foreign.yaml") == "routed"


def test_cascade_run_handoff_record_with_live_successor_is_refused(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    cand = _seed_handoff(repo, "20260101-baton.md", deliverable_id=_DID)
    rel = cand.relative_to(repo).as_posix()
    succ = repo / "state" / "handoffs" / "20260101-succ.md"
    succ.write_text(
        "---\n"
        'title: "S"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\nstatus: open\n'
        f'predecessor: "{rel}"\ndeployment_state: ready_to_fire\ndeliverable_id: dlv-succ-0\n'
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    result = cascade_mod.cascade_run(
        repo, _DID, [rel], "abc1234", repo_root=repo / ".git", plan_path=_PLAN
    )
    assert result["advanced"] == []
    assert "live successor" in result["refused"][0]["reason"]
