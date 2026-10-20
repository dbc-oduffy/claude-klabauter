"""A run stranded at the commit stage by a since-base-overlap refusal re-lands through the existing
commit-halt refire: the refusal names `sibling_claims.reland_remedy(sizing)`, the re-fired run lands only its
own product, and a live sibling run's dirty path stays dirty throughout."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import review_edits, run_token, sibling_claims, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_commit_halt_refire_e2e import (  # noqa: F401
    _SID,
    PLAN,
    RESIDUE,
    RUN2,
    SCRIPT2,
    SIZING,
    _git,
    _second_run,
    _write,
    halted,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIBLING_RUN = "ask-20260101T000000-bbbbbb"
SIBLING_PATH = "docs/sibling.md"
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _sibling_live(root) -> None:
    _write(root, SIBLING_PATH, "sibling dirt\n")
    _write(
        root, f"{RUN_DIR_ROOT}/{SIBLING_RUN}/manifest.json",
        json.dumps({"rows": [{"id": "C1", "writes": [SIBLING_PATH]}], "doc_writes": []}),
    )
    run_token.mint(root, SIBLING_RUN, "sibling.mjs", session_id=_SID)


def test_no_reland_op_is_registered_the_refire_is_the_supported_path():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert not [name for name in OP_MODULE_MAP if "reland" in name.lower()]
    assert "dispatch.ask_gate" in OP_MODULE_MAP
    remedy = sibling_claims.reland_remedy(SIZING)
    assert "dispatch.ask_gate" in remedy and SIZING in remedy


def _commit(root, token: str, inline_review: dict, **extra) -> dict:
    params = {
        "script_path": SCRIPT2, "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
        "inline_review": inline_review, "session_id": _SID, "ledger_verdict": _MET,
        "doc_paths": [SIBLING_PATH],
    }
    params.update(extra)
    return terminal_commit._handler(params, repo_root=root / ".git")


def _landed(root, out: dict) -> set:
    shas = [out["sha"]] + ([out["process_sha"]] if out.get("process_sha") else [])
    return {p for sha in shas for p in _git(root, "show", "--name-only", "--format=", sha).split()}


def test_a_stranded_run_refires_and_lands_its_own_product_without_the_siblings_dirt(halted, monkeypatch):
    base = _git(halted, "rev-parse", "HEAD").strip()
    _write(halted, RESIDUE, "b = 9  # foreign\n")
    _git(halted, "commit", "-q", "-am", "foreign edit after the run base")
    _write(halted, RESIDUE, "b = 2  # reworded by the reviewer\n")
    _sibling_live(halted)
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {RESIDUE: "reviewer"})

    refused = _commit(halted, _second_run(halted), {"integration_stem": "rev-stem", "slices": 1, "fixes": 1},
                      run_base_sha=base)
    assert refused.get("refused") == "since-base-overlap", refused
    assert sibling_claims.reland_remedy(SIZING) in refused["error"]
    assert SIZING in refused["error"] and "dispatch.ask_gate" in refused["error"]
    assert SIBLING_PATH not in json.dumps(refused.get("findings", refused.get("error")))

    verdict = gate(halted, SIZING)
    assert verdict.halt is None and verdict.resume_ledger is not None, verdict.halt
    out = _commit(halted, _second_run(halted), verdict.resume_ledger["inline_review"])

    assert out["committed"] is True, out
    assert RESIDUE in _landed(halted, out)
    assert SIBLING_PATH not in _landed(halted, out)
    assert _git(halted, "status", "--porcelain", "--", RESIDUE).strip() == ""
    assert SIBLING_PATH in _git(halted, "status", "--porcelain", "--", SIBLING_PATH)
    assert (halted / SIBLING_PATH).read_text(encoding="utf-8") == "sibling dirt\n"
    assert PLAN in out["rows_coded"]
