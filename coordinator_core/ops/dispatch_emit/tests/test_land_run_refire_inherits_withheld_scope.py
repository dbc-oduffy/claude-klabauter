"""AC2/AC4: a land_run refire lands the paths its halted predecessor withheld or its reviewers fixed; a foreign hold still refuses."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    DELIVERY_NOTE_DIR,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.refire_inherited_scope import ORIGIN_REVIEWER_APPLIED
from coordinator_core.ops.dispatch_emit.shipped_claims import SHIPPED_CLAIM_REFUSED
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401  (fixture)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = prod.RUN
_STAMP = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
PRED = f"ask-{_STAMP}-bbbbbb"
FOREIGN = f"ask-{_STAMP}-cccccc"
OWN = "pkg/model.py"
INHERITED = "pkg/carried.py"
NOTE = f"{DELIVERY_NOTE_DIR}/{RUN}.md"
SIZING = "state/sizings/2026-10-25-inherit.yaml"
HALT_REASON = "content-withheld"


@pytest.fixture(autouse=True)
def _ledger_open(monkeypatch):
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))


@pytest.fixture(autouse=True)
def _sessions_live(monkeypatch):
    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)


def _stage(root) -> None:
    prod._author(root, OWN, "# own\n")
    prod._author(root, INHERITED, "# carried\n")
    prod._author(root, NOTE, f"## What shipped\n\n- `{OWN}`\n- `{INHERITED}`\n")
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="x1", paths=(OWN,)), ChunkCommit(id="docs", title="docs", paths=(NOTE,))),
        plan_path=None,
    )
    prod._write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker)
    prod._write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    prod._write(root, prod.SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")
    prod._write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": SIZING}))


def _predecessor_halt(root, run_id: str, withheld: dict) -> None:
    prod._write(root, f"{RECORD_DIR_ROOT}/{run_id}/halted.json", json.dumps({
        "status": "halted", "halted": "commit", "sizing_rel": SIZING, "run_artifacts": {"withheld": withheld},
    }))
    halted_withheld_index.add_run(root, run_id, list(withheld))


def _resume() -> dict:
    return {"run_id": PRED, "sizing": SIZING, "run_artifacts": {"withheld": {INHERITED: HALT_REASON}}}


def test_refire_lands_the_path_its_predecessor_withheld(repo):  # noqa: F811
    _stage(repo)
    _predecessor_halt(repo, PRED, {INHERITED: HALT_REASON})

    out = prod._land(repo, resume_ledger=_resume())

    assert out["committed"] is True, out
    assert {OWN, INHERITED} <= set(prod._tree(repo))
    assert out["shipped_claims"]["unbacked"] == []


def test_the_same_landing_without_a_resume_leaves_the_path_unbacked(repo):  # noqa: F811
    _stage(repo)

    out = prod._land(repo)

    assert out.get("refused") == SHIPPED_CLAIM_REFUSED, out
    assert INHERITED not in prod._tree(repo)


def test_refire_lands_a_reviewer_applied_path(repo, monkeypatch):  # noqa: F811
    _stage(repo)
    monkeypatch.setattr(land_run, "inherited_scope", lambda root, resume: {INHERITED: ORIGIN_REVIEWER_APPLIED})

    out = prod._land(repo, resume_ledger={"run_id": PRED, "sizing": SIZING})

    assert out["committed"] is True, out
    assert INHERITED in prod._tree(repo)


def test_a_path_a_different_halted_run_holds_still_refuses(repo):  # noqa: F811
    _stage(repo)
    _predecessor_halt(repo, PRED, {INHERITED: HALT_REASON})
    _predecessor_halt(repo, FOREIGN, {INHERITED: HALT_REASON})
    prod._write(repo, f"{RECORD_DIR_ROOT}/{FOREIGN}/manifest.json", json.dumps({"rows": [{"id": "F1", "writes": [INHERITED]}]}))
    prod._write(repo, f"{RECORD_DIR_ROOT}/{FOREIGN}/run.json", json.dumps({"sizing_rel": "state/sizings/other.yaml"}))

    out = prod._land(repo, resume_ledger=_resume())

    assert out.get("refused") == "peer-hunks-co-declared", out  # the peer-hold gate fires before the shipped-claim check
    assert INHERITED not in prod._tree(repo)
