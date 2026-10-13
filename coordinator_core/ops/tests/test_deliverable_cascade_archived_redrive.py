"""Regression pin: the deliverable cascade re-drives an already-archived plan to a shipped sizing.

The fixture writes register rows directly, so it does not consume the rejudge writeback.
A still-partial row refuses with the `ship_text` reason; once every row is met and wired the
same call, fed the plan's `archive/specs/` path, flips the sizing and keeps the `docs/plans/` FK.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_deliverable_cascade_archived_redrive.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DELIVERABLE = "dlv-redrive-000000"
_ORIGIN = "docs/plans/2026-10-13-redrive.md"
_ARCHIVED = "archive/specs/2026-10/2026-10-13-redrive.md"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _sizing(row_status: str, wired: bool) -> str:
    met = 1 if row_status == "met" else 0
    return (
        "schema: sizing-object\n"
        "intent: Test intent, verbatim.\n"
        "estimate:\n  tshirt: M\n  provisional: true\n"
        "route: plan\n"
        "detents: []\n"
        "fork: null\n"
        "xl_exit: null\n"
        "status: routed\n"
        "premise:\n  provenance: read\n  evidence: test fixture\n"
        f"deliverable_id: {_DELIVERABLE}\n"
        "requirement_register:\n"
        "  sources:\n"
        "  - doc: state/sizings/x.yaml\n    ref: pm_verbatims turn 1\n"
        "  rows:\n"
        "  - id: T10\n"
        "    source_text: the vote page matches the canvas\n"
        "    surface: internal\n"
        "    anchor: test\n"
        f"    status: {row_status}\n"
        "    claimed_by: []\n"
        f"    wired: {'true' if wired else 'false'}\n"
        "    last_progress_at: '2026-10-13T12:00:00Z'\n"
        + (f"    met_by:\n    - {_ORIGIN}@{'a' * 40}\n" if row_status == "met" else "")
        + "  rollup:\n"
        f"    open: 0\n    partial: {1 - met}\n    met: {met}\n"
        "    deferred: 0\n    waived: 0\n    unclaimed: 0\n    unwired: 0\n"
        f"    coverage:\n      met: {met}\n      total: 1\n"
        "    computed_at: '2026-10-13T12:00:00Z'\n"
    )


def _redrive(repo: Path) -> dict:
    return asyncio.run(
        cascade_mod._handler(
            {
                "deliverable_id": _DELIVERABLE,
                "source_kind": "plan",
                "source_path": _ARCHIVED,
                "target_kind": "sizing",
            },
            repo_root=repo / ".git",
        )
    )


def test_archived_plan_redrive_refuses_partial_then_ships_once_met(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = repo / "state" / "sizings" / "20261013-redrive.yaml"
    sizing.parent.mkdir(parents=True)

    sizing.write_text(_sizing("partial", False), encoding="utf-8")
    refused = _redrive(repo)
    assert refused["exit_code"] == 1
    assert refused["advanced"] == []
    assert "row T10 (internal) is partial" in refused["refused"][0]["reason"]
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["status"] == "routed"

    sizing.write_text(_sizing("met", True), encoding="utf-8")
    shipped = _redrive(repo)
    assert shipped["exit_code"] == 0
    assert len(shipped["advanced"]) == 1
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["status"] == "shipped"
    assert doc["plan"] == _ORIGIN


def test_rejudge_writeback_alone_drives_archived_plan_sizing_to_shipped(tmp_path):
    from coordinator_core.ops import review_stamp
    from coordinator_core.session import record_homes

    plan_id = "pln-redrive-chain-000001"
    sizing_rel = "state/sizings/20261013-redrive.yaml"
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = repo / sizing_rel
    sizing.parent.mkdir(parents=True)
    sizing.write_text(_sizing("partial", False), encoding="utf-8")
    plan = repo / _ARCHIVED
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f"---\ntitle: T\nstatus: executing\nplan_id: {plan_id}\nsizing_object: {sizing_rel}\n"
        f"register_claims:\n  sizing: {sizing_rel}\n  rows: [T10]\nreview_stamp:\n"
        "  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
        "  criterion:\n    status: met\n    observation: o\n    sidecar: null\n"
        "  stamped_at: '2026-10-01T00:00:00Z'\n---\n\nBody.\n",
        encoding="utf-8",
    )
    record = Path(record_homes.home_dir(str(repo), "delivery-verdicts")) / "2026-10" / "redrive.rejudge.md"
    record.parent.mkdir(parents=True)
    record.write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "kind": "criterion-rejudge",
                "plan_id": plan_id,
                "head_sha": "abc",
                "recorded_at": "2026-10-02T00:00:00.000000Z",
                "criterion": {
                    "status": "met",
                    "observation": "judged",
                    "sidecar": None,
                    "register_rows": [
                        {"id": "T10", "claim": "this-plan", "status": "met", "wired": True,
                         "surface": "internal", "observed_ref": "x.py::f"}
                    ],
                },
            }
        )
        + "---\n",
        encoding="utf-8",
    )

    refused = _redrive(repo)
    assert refused["exit_code"] == 1 and "row T10 (internal) is partial" in refused["refused"][0]["reason"]

    result = review_stamp.rejudge(plan, repo)
    assert result["status"] == "rejudged" and result.get("register_sizing") == sizing_rel
    reg = yaml.safe_load(sizing.read_text(encoding="utf-8"))["requirement_register"]
    assert (reg["rows"][0]["status"], reg["rows"][0]["wired"]) == ("met", True)
    assert reg["rollup"]["met"] == 1 and reg["rollup"]["partial"] == 0 and reg["rollup"]["open"] == 0
    assert reg["rollup"]["unwired"] == 0 and reg["rollup"]["coverage"] == {"met": 1, "total": 1}

    shipped = _redrive(repo)
    assert shipped["exit_code"] == 0 and len(shipped["advanced"]) == 1
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["status"] == "shipped" and doc["plan"] == _ORIGIN
