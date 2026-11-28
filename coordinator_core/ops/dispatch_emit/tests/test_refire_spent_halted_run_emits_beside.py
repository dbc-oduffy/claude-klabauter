"""A refire of a halted run whose script is spent emits beside it; an unspent halted run still rebinds."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path
from coordinator_core.ops.dispatch_emit.tests.test_refire_resumes_halted_run import (  # noqa: F401
    OLDER,
    _fire,
    _halt,
    _marker,
    repo,
)


def _script(repo, landed):
    script = repo / RUN_DIR_ROOT / f"{OLDER}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("// emission\n", encoding="utf-8", newline="")
    receipt = emission_receipt_path(script)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"landed": {"sha": "d" * 40}} if landed else {}), encoding="utf-8")
    return script


def test_a_spent_halted_run_emits_beside_its_script(repo):
    _halt(repo)
    script = _script(repo, landed=True)
    before = script.read_bytes()
    reply = _fire(repo)
    assert reply["run_id"] != OLDER
    redirect = reply[OUTPUT_PATH_REDIRECT_KEY]
    assert redirect["halted_run"] == OLDER
    assert redirect["reason"] == "run-spent"
    assert redirect["to"] != redirect["from"]
    assert script.read_bytes() == before
    assert (rl.read_run(rl.run_dir(repo, reply["run_id"])) or {}).get(rl.RESUMED_FROM_FIELD) == OLDER
    assert _marker(repo)["status"] != "resumed"


def test_an_unspent_halted_run_still_rebinds(repo):
    _halt(repo)
    _script(repo, landed=False)
    reply = _fire(repo)
    assert reply["run_id"] == OLDER
    assert OUTPUT_PATH_REDIRECT_KEY not in reply
