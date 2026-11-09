"""A refire that re-sizes a halted run to a new sizing keeps every open revise-added row: it reaches the
refired run's register, its requirements view and its judgement; a met row is not re-carried."""

from __future__ import annotations

import os

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import halted_run, op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    BASE,
    REL,
    _halt,
    composed,
    repo,
)

NEW_REL = "state/sizings/2026-10-16-refire-resized.yaml"
R_ONE = "revise one: keep the schema stable"
R_TWO = "revise two: also log the refire"
R_MET = "revise met: already delivered"


def _register(repo, rel):
    return rr.read_register((repo / rel).read_text(encoding="utf-8")).rows


def _resize(repo):
    """A newer sizing for the same ask whose register holds no revise row."""
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc.pop(rr.REGISTER_KEY, None)
    (repo / NEW_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    os.utime(repo / NEW_REL, ns=(10**18, 10**18))


def _refire_resized(repo):
    _resize(repo)
    assert halted_run.halted_run_to_resume(repo, NEW_REL, run_id=ASK) == ASK
    reply = op._dispatch_emit({"ask": True, "sizing_path": NEW_REL}, repo_root=repo)
    assert reply["run_id"] != ASK
    return reply["run_id"]


def _revise(repo, text):
    return rl.revise(repo, ASK, verbatim=text, sender="example-game-repo")[2]


def _texts(repo, run_id):
    ledger = rl.read_ledger(repo, rl.run_dir(repo, run_id))
    return {i.text.strip(): i.id for i in ledger.items}


def test_two_revise_rows_survive_a_resized_refire_and_are_judged(repo, composed):  # noqa: F811
    _halt(repo, ASK, BASE)
    _revise(repo, R_ONE)
    _revise(repo, R_TWO)
    new_id = _refire_resized(repo)

    ids = _texts(repo, new_id)
    assert {R_ONE, R_TWO} <= set(ids)
    assert {r[rr.ROW_TEXT] for r in _register(repo, NEW_REL)} >= {R_ONE, R_TWO}
    view = (rl.run_dir(repo, new_id) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert f"> {R_ONE}" in view and f"> {R_TWO}" in view

    ledger = rl.read_ledger(repo, rl.run_dir(repo, new_id))
    verdict = {"items": [{"id": ids[R_ONE], "status": "met", "met_by": "x"}]}
    failed = {f["id"] for f in rl.judge(ledger, verdict)["failures"]}
    assert ids[R_TWO] in failed
    every = {"items": [{"id": i.id, "status": "met", "met_by": "x"} for i in ledger.items]}
    assert rl.judge(ledger, every)["pass"] is True


def test_a_met_revise_row_is_not_recarried(repo, composed):  # noqa: F811
    _halt(repo, ASK, BASE)
    _revise(repo, R_ONE)
    met_id = _revise(repo, R_MET)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    for row in doc[rr.REGISTER_KEY][rr.ROWS_KEY]:
        if row[rr.ROW_ID] == met_id:
            row[rr.ROW_STATUS] = rr.STATUS_MET
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    new_id = _refire_resized(repo)
    ids = _texts(repo, new_id)
    assert R_ONE in ids and R_MET not in ids


def test_rebinding_again_does_not_duplicate_the_carried_rows(repo, composed):  # noqa: F811
    _halt(repo, ASK, BASE)
    _revise(repo, R_ONE)
    new_id = _refire_resized(repo)
    rl.bind(repo, new_id, NEW_REL)
    texts = [r[rr.ROW_TEXT] for r in _register(repo, NEW_REL)]
    assert texts.count(R_ONE) == 1
