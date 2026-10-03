"""Opt-in chatty emit: roster, nonces, overseer; non-chatty bytes unchanged."""

from __future__ import annotations

import json
import re

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import chatty
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW


def _rows():
    return [[
        WaveRow(id=i, title=f"t-{i}", surface="x", writes=[f"pkg/{i}.py"], reads=[], depends_on=[])
        for i in ("C1", "C2", "C3")
    ]]


def _script(**kw):
    return compose_script(_rows(), name="wf", description="d", **REVIEW_KW, **kw)


def _embedded_roster(script: str) -> dict:
    m = re.search(r"ROSTER:\\n(\{.*?\\n\})'", script, re.DOTALL)
    assert m
    return json.loads(m.group(1).replace("\\n", "\n").replace('\\"', '"'))


def test_chatty_off_is_byte_identical_to_default():
    assert _script(chatty=False) == _script()
    assert "roster" not in _script().lower()
    assert "chatty" not in _script()


def test_chatty_emit_has_valid_roster_overseer_and_distinct_nonces():
    script = _script(chatty=True)
    assert "chatty: true" in script
    assert "label: 'chatty-overseer'" in script
    roster = _embedded_roster(script)
    assert chatty.validate_roster(roster) == []
    assert roster["overseer_role"] == chatty.OVERSEER_ROLE
    roles = [m["role"] for m in roster["members"]]
    assert roles == ["overseer", "C1", "C2", "C3"]
    nonces = [m["nonce"] for m in roster["members"]]
    assert len(set(nonces)) == 4
    for n in nonces:
        assert script.count(n) >= 1
    for m in roster["members"][1:]:
        assert f"Nonce: {m['nonce']}" in script
    assert script.count("register") >= 3


def test_chatty_script_has_no_contract_errors():
    errors = [f for f in run_checks(_script(chatty=True)) if f.severity is Severity.ERROR]
    assert errors == []


def test_roster_validation_rejects_duplicate_nonce_and_unknown_overseer():
    roster = chatty.build_roster("r", ["A"])
    roster["members"][1]["nonce"] = roster["members"][0]["nonce"]
    assert "duplicate member nonce" in chatty.validate_roster(roster)
    roster["overseer_role"] = "nobody"
    assert "overseer_role names no member" in chatty.validate_roster(roster)
