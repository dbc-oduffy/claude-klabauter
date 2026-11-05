"""A reserved run id whose gate refused for a spent token is retired: the refire mints a fresh run id carrying its notes."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_gate import TOKEN_SPENT_FILE
from coordinator_core.ops.dispatch_emit.tests.test_refire_carries_notes_every_mint_path import (  # noqa: F401
    LIVE_A,
    _seed_notes,
    _spy_texts,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    REL,
    composed,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_reserved_refire_reads_halt_record_notes import (  # noqa: F401
    SLICE,
    _slice_sizing,
)


def _retire(repo):
    rl.start(repo, SLICE)
    rl.bind(repo, SLICE, REL, ask_text="the ask")
    (record_dir(repo, SLICE) / TOKEN_SPENT_FILE).write_text("{}\n", encoding="utf-8")
    (record_dir(repo, SLICE) / "halted.json").write_text(
        json.dumps({"status": "resumed", "sizing_rel": REL}), encoding="utf-8"
    )


def _fire(repo):
    return op._dispatch_emit({"ask": True, "sizing_path": REL, "reserved_run_id": SLICE}, repo_root=repo)


def test_a_spent_reserved_run_mints_a_fresh_id_carrying_its_notes(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _slice_sizing(repo)
    _retire(repo)
    _seed_notes(repo, SLICE)
    reply = _fire(repo)
    assert reply["run_id"] != SLICE
    assert rl.read_run(rl.run_dir(repo, reply["run_id"]))[rl.RESUMED_FROM_FIELD] == SLICE
    assert f"> {LIVE_A}" in seen["script"] + "\n".join((seen["kw"].get("staged_out") or {}).values())
    assert f"{SLICE}.workflow.mjs" not in str(reply.get("output_path", ""))


def test_a_spent_reserved_run_without_notes_still_mints_fresh_and_names_it(repo, composed):  # noqa: F811
    _slice_sizing(repo)
    _retire(repo)
    reply = _fire(repo)
    assert reply["run_id"] != SLICE
    assert rl.read_run(rl.run_dir(repo, reply["run_id"]))[rl.RESUMED_FROM_FIELD] == SLICE


def test_an_unspent_reserved_run_keeps_its_id(repo, composed):  # noqa: F811
    _slice_sizing(repo)
    rl.start(repo, SLICE)
    reply = _fire(repo)
    assert reply["run_id"] == SLICE
