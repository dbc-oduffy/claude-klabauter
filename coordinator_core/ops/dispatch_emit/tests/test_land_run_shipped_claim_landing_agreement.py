"""dispatch.land_run hands the shipped-claim gate its own paths, stranded rows and an owner lookup."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, land_run, run_token, shipped_claims
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    DELIVERY_NOTE_DIR,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.shipped_claims import NOT_DECLARED, ROW_GRADE_OWED, SHIPPED_CLAIM_REFUSED
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401  (fixture)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_LANDING = "pkg/shipped.py"
_PEER_FILE = "pkg/peer_owned.py"
_PARTIAL_FILE = "pkg/partial_row.py"
_PARTIAL_REPORT = "reports/C3.md"
_UNDONE = "Not done: one optional test could not be built"
_SIBLING = "warp-2026-10-24-sibling-halted-aaaaaa-20261024T000000-bbbbbb"


@pytest.fixture(autouse=True)
def _ledger_open(monkeypatch):
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))


def _note(repo, *claimed: str) -> None:
    body = "".join(f"- `{p}`\n" for p in claimed)
    prod._write(repo, f"{DELIVERY_NOTE_DIR}/{prod.RUN}.md", f"## What shipped\n\n{body}")


def _ask(repo, chunks: tuple) -> None:
    marker = f"{RUN_DIR_ROOT}/{prod.RUN}/commit-request.txt"
    prod._write(repo, marker, render_marker(CommitRequest(chunks=chunks, plan_path=prod.PLAN)) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{prod.RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    prod._write(repo, f"{RECORD_DIR_ROOT}/{prod.RUN}/manifest.json", json.dumps(manifest.to_json()))
    prod._write(repo, prod.SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{prod.RUN}/manifest.json\n")


def _two_row_run(repo) -> None:
    prod._author(repo, _LANDING, "X = 1\n")
    prod._author(repo, _PARTIAL_FILE, "Y = 1\n")
    prod._write(repo, _PARTIAL_REPORT, f"PARTIAL: delivered edits, one test left\n\n{_UNDONE}\n")
    _ask(repo, (
        ChunkCommit(id="C4", title="t4", paths=(_LANDING,)),
        ChunkCommit(id="C3", title="t3", paths=(_PARTIAL_FILE,), report=_PARTIAL_REPORT),
    ))


def test_a_claim_on_a_file_a_halted_sibling_withholds_commits_and_names_the_owner(repo, monkeypatch):  # noqa: F811
    prod._author(repo, _LANDING, "X = 1\n")
    prod._write(repo, _PEER_FILE, "P = 1\n")
    prod._ask_script(repo, _LANDING)
    _note(repo, _LANDING, _PEER_FILE)
    halted_withheld_index.add_run(repo, _SIBLING, [_PEER_FILE])
    real = shipped_claims.shipped_gate
    spawned: list = []

    def gate(*args, owners=None, **kw):
        def guarded(paths):
            def no_spawn(*a, **k):
                spawned.append(a)
                raise AssertionError("owners started a process")

            with monkeypatch.context() as m:
                m.setattr(subprocess, "Popen", no_spawn)
                return owners(paths)

        return real(*args, owners=guarded if owners else None, **kw)

    monkeypatch.setattr(shipped_claims, "shipped_gate", gate)

    out = prod._land(repo)

    assert out["committed"] is True, out
    assert out["shipped_claims"]["peer_owned"] == [{"path": _PEER_FILE, "owner": _SIBLING}]
    assert _PEER_FILE not in prod._tree(repo) and _LANDING in prod._tree(repo)
    assert spawned == []


def test_a_claim_on_a_partial_rows_own_file_names_the_grade_decision_then_lands_once_credited(repo):  # noqa: F811
    _two_row_run(repo)
    _note(repo, _LANDING, _PARTIAL_FILE)
    prod._record_dir(repo)
    head = prod._git(["rev-parse", "HEAD"], repo).strip()
    token = run_token.mint(repo, prod.RUN, prod.SCRIPT, session_id=prod.SELF)

    out = prod._land(repo, incomplete_chunks=["C3"], run_token=token)

    assert out["committed"] is False and out["refused"] == ROW_GRADE_OWED, out
    err = out["error"]
    assert "C3" in err and _PARTIAL_FILE in err and "credit_chunks" in err and "optional test" in err, err
    assert out["shipped_claims"]["row_grade_owed"][0]["chunk"] == "C3"
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head

    prod._write(
        repo, f"{RECORD_DIR_ROOT}/{prod.RUN}/credit.jsonl",
        json.dumps({"chunk": "C3", "evidence": "edits verified", "at": "2026-10-24T00:00:00Z"}) + "\n",
    )
    again = prod._land(repo, incomplete_chunks=["C3"], run_token=token)

    assert again["committed"] is True, again
    assert _PARTIAL_FILE in prod._tree(repo)


def test_a_declared_file_the_landing_withholds_still_refuses_unbacked_with_source_and_reason(repo):  # noqa: F811
    prod._author(repo, _LANDING, "X = 1\n")
    prod._ask_script(repo, _LANDING)
    _note(repo, _LANDING)

    out = prod._land(repo, withhold_paths=[{"path": _LANDING, "reason": "peer owns this"}])

    assert out["committed"] is False and out["refused"] == SHIPPED_CLAIM_REFUSED, out
    assert "What shipped" in out["error"] and "peer owns this" in out["error"], out["error"]
    assert "peer_owned" not in out["shipped_claims"]


def test_an_unlanded_file_no_row_declares_refuses_unbacked_naming_the_missing_row(repo):  # noqa: F811
    prod._author(repo, _LANDING, "X = 1\n")
    prod._write(repo, _PEER_FILE, "P = 1\n")
    prod._ask_script(repo, _LANDING)
    _note(repo, _LANDING, _PEER_FILE)

    out = prod._land(repo)

    assert out["committed"] is False and out["refused"] == SHIPPED_CLAIM_REFUSED, out
    assert "What shipped" in out["error"] and _PEER_FILE in out["error"], out["error"]
    assert NOT_DECLARED in out["error"], out["error"]


def test_a_held_chunks_declared_file_refuses_unbacked_naming_the_held_chunk(repo):  # noqa: F811
    _two_row_run(repo)
    _note(repo, _LANDING, _PARTIAL_FILE)
    prod._record_dir(repo)

    out = prod._land(repo, held=[{"chunk": "C3", "reason": "waiting"}])

    assert out["committed"] is False and out["refused"] == SHIPPED_CLAIM_REFUSED, out
    assert "What shipped" in out["error"] and "held chunk C3" in out["error"], out["error"]
