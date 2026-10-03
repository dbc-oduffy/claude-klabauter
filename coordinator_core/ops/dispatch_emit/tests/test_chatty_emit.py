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


def test_briefs_carry_mailbox_rule_and_no_peer_sendmessage_instruction():
    brief = chatty.member_brief("C1", "abc")
    assert "mail/<role>.jsonl" in brief
    assert "state=returned" in brief
    assert "Never SendMessage a peer" in brief
    assert "Message the overseer or a peer" not in brief
    assert "addressable by their roster agent_id" not in brief
    assert "SendMessage" not in chatty.overseer_prompt(chatty.build_roster("r", ["A"]))


def test_wake_stage_only_when_chatty_and_after_rows():
    assert "chatty-wake" not in _script()
    script = _script(chatty=True)
    wake = script.index("label: 'chatty-wake'")
    rows = script.index("await Promise.all(Object.values(_rows));")
    prov = script.index("label: 'chatty-overseer'")
    assert prov < script.index("_rows['C1'] =") < rows < wake
    assert "const _overseer" not in script and "  await agent('You are the overseer of a chatty run. " in script
    assert script.count("SendMessage its roster agent_id") == 1
    assert "`returned`" in script


def test_chatty_flag_reaches_the_emitter(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import cli

    seen = []
    monkeypatch.setattr(cli, "_dispatch_emit", lambda params, repo_root=None: seen.append(params) or {"ok": False})
    for extra in ([], ["--chatty"]):
        cli.main(["--plan", "p.md", "--out", str(tmp_path / "o.workflow.mjs"), *extra])
    assert "chatty" not in seen[0]
    assert seen[1]["chatty"] is True
