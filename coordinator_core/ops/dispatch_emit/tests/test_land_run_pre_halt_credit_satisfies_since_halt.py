"""A ledger-resume row met_by a named pre-halt commit owes no diff to land_run's since-halt check, so
the close lands with no declared product path changed; the same row without that credit still refuses."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
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
SHA = "2fd9d96c64"
NOTE = f"Re-rule R1 as met by commit {SHA}; its tests pass."
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
    detail: dict = {}
    return land_run._unchanged_since_halt(root, _resume(halt, **extra), verdict, manifest, None, detail)


def test_a_row_met_by_a_named_pre_halt_commit_owes_no_diff(declared):
    assert _check(declared, _verdict([SHA]), revise_notes=[NOTE]) == []


def test_the_same_row_without_a_revise_note_naming_the_commit_still_refuses(declared):
    assert _check(declared, _verdict([SHA])) == REFUSED
    assert _check(declared, _verdict([SHA]), revise_notes=["re-ruled met; tests pass"]) == REFUSED


def test_a_met_by_commit_nothing_names_still_refuses(declared):
    assert _check(declared, _verdict(["abcdef1234"]), revise_notes=[NOTE]) == REFUSED


def test_a_post_commit_row_gets_no_credit(declared):
    verdict = {"items": [{"id": "R1", "status": "post_commit", "met_by": [SHA]}]}
    assert _check(declared, verdict, revise_notes=[NOTE]) == REFUSED


def _terminal(root, token, resume, verdict) -> dict:
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": "run2", "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def test_land_run_lands_the_close_with_no_declared_product_path_changed(halted):  # noqa: F811
    resume = {**_composed(halted), "revise_notes": [NOTE]}
    refire = _refire(halted, edit=False)

    out = _terminal(halted, refire["token"], resume, _verdict([SHA]))

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert not landed & {"a.py", B}, landed


def test_land_run_still_refuses_the_uncredited_refire_that_changed_nothing(halted):  # noqa: F811
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, refire["token"], resume, _verdict([SHA]))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == REFUSED
    assert _git(halted, "rev-parse", "HEAD") == head
