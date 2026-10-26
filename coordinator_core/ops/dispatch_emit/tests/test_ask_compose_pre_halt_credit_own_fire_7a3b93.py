"""A met blocking row cited to a checkpoint this fire made since the halt stays met with no revise note naming a sha.
The 7a3b93 incident shape; the composed script runs under node with every agent leg stubbed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_SINCE_HALT_SHAS_FIELD
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_all_landed_ledger as _base
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_all_landed_ledger import _labels, _script
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    readers_cfg,
    stage_root,
    stage_reply,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OWN = "1a8340e0cf"
_PRE = "57e71fe96d"
_FILE = "coordinator_core/ops/dispatch_emit/halt_baseline.py"
_DEMOTED = ["met_by: the commit sha that carries the credited work"]
_RESUME = {
    "run_id": "ask-prior", "plan": _base._PLAN, "blocking": ["R1"], "halt_sha": "7ed10ac381" + "0" * 30,
    "unlanded": [], "revise_notes": ["R1 is ruled met by the work landed in this fire"],
    "remaining": {"R1": {"remaining": ["update the sizing"], "evidence": "no sha here"}},
}


def _met(met_by):
    return {"items": [{
        "id": "R1", "status": "met", "wired": True, "met_by": met_by,
        "evidence": "final edits land with the terminal commit",
    }]}


def _drive(monkeypatch, verify, resume, *, review_paths=()):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    disk = {**_base._DISK, "review_declared_paths": list(review_paths)}
    gate_reply = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": resume,
        "tshirt": "M", "route": "spec-dispatch", "stage_reply": stage_reply(disk),
    }
    cfg = {
        "token": _base._TOKEN, "verify": verify, "commit": _base._LANDED, **readers_cfg(),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply},
    }
    proc = subprocess.run(
        [node, "-"], input=_base._NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(_script(monkeypatch)), "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_demoted(out):
    item = out["result"]["halted_by"]["items"][0]
    assert item["status"] == "not_met"
    assert item["remaining"] == _DEMOTED
    assert "commit" not in _labels(out)


def test_a_met_by_naming_a_since_halt_checkpoint_stays_met_with_no_note_sha(monkeypatch):
    resume = {**_RESUME, RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: [_OWN + "0" * 30]}
    out = _drive(monkeypatch, _met([_PRE, _OWN]), resume)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)
    assert "halt-commit" not in _labels(out)


def test_the_same_met_by_with_no_since_halt_source_is_demoted(monkeypatch):
    _assert_demoted(_drive(monkeypatch, _met([_PRE, _OWN]), _RESUME))


def test_an_unknown_sha_is_demoted_beside_a_since_halt_list(monkeypatch):
    resume = {**_RESUME, RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: [_OWN + "0" * 30]}
    _assert_demoted(_drive(monkeypatch, _met(["abcdef1234"]), resume))


def test_a_sha_free_file_met_by_on_the_xs_tail_path_is_demoted_though_declared(monkeypatch):
    out = _drive(monkeypatch, _met([_FILE]), _RESUME, review_paths=[_FILE])
    _assert_demoted(out)


def test_the_in_run_checkpoint_shas_feed_the_since_halt_list(monkeypatch):
    script = _script(monkeypatch)
    assert "..._checkpointShas]" in script
    assert "const _sinceCredit = " in script


def test_the_footprint_flag_is_set_only_inside_the_review_block_and_read_by_the_demotion(monkeypatch):
    script = _script(monkeypatch)
    assert script.count("_footprintReviewed = true;") == 1
    flag = script.index("_footprintReviewed = true;")
    gate = script.index("|| _codeReviewOwed)) {")
    phase = script.index("phase('Review wave')")
    empty_wave = script.index("_reviewWave = [];")
    assert gate < phase < flag < empty_wave
    assert "_footprintReviewed && " in script
    assert "_manifest?.review_declared_paths ?? [], _footprintReviewed" in script
