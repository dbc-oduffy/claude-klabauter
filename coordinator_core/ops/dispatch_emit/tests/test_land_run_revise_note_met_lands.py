"""A workflow.revise note the checker judges met under its note id (N1) lands; unmet or foreign ids still refuse."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_land_run_review_floor import (  # noqa: F401
    CODE,
    RUN,
    SELF,
    _stage,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _note(root, text: str = "Prefer the shorter name.") -> None:
    path = root / rl.run_dir(root, RUN).relative_to(root) / rl.NOTES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"note": text, "at": "2026-10-30T00:00:00Z"}) + "\n")


def _land(root, token: str, items: list) -> dict:
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "prep": {"run_base_sha": None, "product_files": [CODE], "foreign_claims": [], "slice_files": [CODE]},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
        "wave_reviews": [],
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review,
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": {"items": items},
        },
        repo_root=root / ".git",
    )


R1_MET = {"id": "R1", "status": "met", "wired": True}


def test_a_revise_note_judged_met_as_n1_lands(repo):  # noqa: F811
    token = _stage(repo, CODE)
    _note(repo)

    out = _land(repo, token, [R1_MET, {"id": "N1", "status": "met"}])

    assert out.get("committed") is True and out.get("refused") is None, out


def test_a_revise_note_judged_not_met_still_refuses(repo):  # noqa: F811
    token = _stage(repo, CODE)
    _note(repo)

    out = _land(repo, token, [R1_MET, {"id": "N1", "status": "not_met"}])

    assert out.get("committed") is False and out.get("refused") == "ledger-unmet", out
    assert "N1: " + rl.NOTE_NOT_MET_REASON in out["error"]


def test_a_foreign_id_still_refuses_as_no_register_row(repo):  # noqa: F811
    token = _stage(repo, CODE)
    _note(repo)

    out = _land(repo, token, [R1_MET, {"id": "N1", "status": "met"}, {"id": "N2", "status": "met"}])

    assert out.get("committed") is False and out.get("refused") == "ledger-unmet", out
    assert "N2: " + rl.NOT_A_ROW_REASON in out["error"] and "N1:" not in out["error"]


def test_a_note_id_with_no_note_stored_refuses(repo):  # noqa: F811
    token = _stage(repo, CODE)

    out = _land(repo, token, [R1_MET, {"id": "N1", "status": "met"}])

    assert out.get("refused") == "ledger-unmet", out
    assert "N1: " + rl.NOT_A_ROW_REASON in out["error"]
