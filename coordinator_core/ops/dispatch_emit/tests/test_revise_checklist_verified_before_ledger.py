"""An executor reply that omits or marks unmet a revise checklist item is caught before the ledger leg."""

from __future__ import annotations

import json
import shutil

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_refire_carries_notes_every_mint_path import _spy_texts
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    BASE,
    _fire,
    _halt,
    composed,
    repo,
)

N1 = "N1-MARK write the use_restrictions writers"
N2 = "N2-MARK update the readers too\nsecond line of N2"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node evaluates the composed JS")


def _seed(repo):
    notes = rl.run_dir(repo, ASK) / rl.NOTES_FILE
    notes.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"note": n, "at": f"2026-11-20T10:0{i}:00Z"}) for i, n in enumerate((N1, N2))]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _script(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed(repo)
    _fire(repo)
    return seen["script"]


def test_the_receipt_runs_after_the_waves_and_its_gaps_join_the_ledger_before_the_fix_legs(repo, monkeypatch):  # noqa: F811
    script = _script(repo, monkeypatch)
    assert script.index("await Promise.all(Object.values(_rows));") < script.index("_reviseGaps = await _reviseReceipt()")
    push = script.index("for (const g of _reviseGaps)")
    assert script.index("_normaliseLedger();", script.index("const _normaliseLedger")) < push
    assert push < script.index("const _ledgerBlocking")
    assert "status: 'not_met'" in script[push:push + 400] and "remaining: [g.item]" in script[push:push + 400]


def test_a_fresh_fire_composes_no_receipt(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _fire(repo)
    assert "_reviseGaps" not in seen["script"] and "_reviseReceipt" not in seen["script"]
