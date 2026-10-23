"""A ledger-resume row an EM-performed row discharged between halts owes no diff to land_run's since-halt
check, so the close lands with no declared product path changed. An undischarged row whose met_by cites
no contrary sha is credited as already landed by the carrier commit (halt_landing.landing_credited); a
foreign sha is still refused, pinned in test_land_run_ledger_already_landed.py."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    B,
    _composed,
    _git,
    _refire,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
DISCHARGED = {"E1": "landed CLAUDE.md by hand at c72e924dbb"}
REFUSED = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _verdict(met_by) -> dict:
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def _resume(halt_sha: str, **extra) -> dict:
    return {"run_id": "r", "halt_sha": halt_sha, "blocking": ["R1"], "unlanded": ["C1"], **extra}


@pytest.fixture
def declared(tmp_path):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*a):
        return subprocess.run(["git", *a], cwd=str(tmp_path), check=True, capture_output=True, text=True,
                              **no_console_creationflags()).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "false")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    git("add", "prod.py")
    git("commit", "-q", "-m", "halt")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "R1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, git("rev-parse", "HEAD"), manifest


def _check(declared, verdict, **extra):
    root, halt, manifest = declared
    return land_run._unchanged_since_halt(root, _resume(halt, **extra), verdict, manifest, None, {})


def test_a_row_met_by_an_em_discharged_row_owes_no_diff(declared):
    assert _check(declared, _verdict(["E1"]), **{RESUME_LEDGER_EM_DISCHARGED_FIELD: DISCHARGED}) == []


def test_a_discharge_text_naming_the_register_row_owes_no_diff(declared):
    discharged = {"E1": "R1 landed by hand at c72e924dbb"}
    assert _check(declared, _verdict(["c72e924dbb"]), **{RESUME_LEDGER_EM_DISCHARGED_FIELD: discharged}) == []


def test_the_same_row_with_no_discharge_still_refuses(declared):
    assert _check(declared, _verdict(["E1"])) == REFUSED
    assert _check(declared, _verdict(["E1"]), **{RESUME_LEDGER_EM_DISCHARGED_FIELD: {}}) == REFUSED


def test_a_discharge_nothing_links_to_the_row_still_refuses(declared):
    discharged = {"E2": "did something else"}
    assert _check(declared, _verdict(["E1"]), **{RESUME_LEDGER_EM_DISCHARGED_FIELD: discharged}) == REFUSED


def test_a_row_id_that_only_contains_the_discharged_id_is_not_credited(declared):
    assert _check(declared, _verdict(["E10"]), **{RESUME_LEDGER_EM_DISCHARGED_FIELD: DISCHARGED}) == REFUSED


def test_a_post_commit_row_gets_no_credit(declared):
    verdict = {"items": [{"id": "R1", "status": "post_commit", "met_by": ["E1"]}]}
    assert _check(declared, verdict, **{RESUME_LEDGER_EM_DISCHARGED_FIELD: DISCHARGED}) == REFUSED


def _terminal(root, token, resume, verdict) -> dict:
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": "run2", "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def test_land_run_lands_the_close_for_an_em_discharged_row(halted):  # noqa: F811
    resume = {**_composed(halted), RESUME_LEDGER_EM_DISCHARGED_FIELD: DISCHARGED}
    refire = _refire(halted, edit=False)

    out = _terminal(halted, refire["token"], resume, _verdict(["E1"]))

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert not landed & {"a.py", B}, landed


def test_land_run_credits_the_undischarged_refire_to_the_carrier_commit(halted):  # noqa: F811
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, refire["token"], resume, _verdict(["E1"]))

    assert out["committed"] is True and out["already_landed"] is True, out
    assert out["sha"] == _git(halted, "log", "-1", "--format=%H", "--", "a.py").strip()
    assert _git(halted, "rev-parse", "HEAD") == head
