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
        assert f"MY-NONCE={m['nonce']}" in script
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
    roster = chatty.build_roster("r", ["A"])
    for text in (brief, chatty.overseer_prompt(roster), chatty.survey_prompt("n"),
                 chatty.summary_prompt("n"), chatty.continuation_brief("A")):
        assert "SendMessage" not in text


def test_continuation_stage_after_rows_and_no_sendmessage():
    assert "chatty-survey" not in _script()
    script = _script(chatty=True)
    assert "SendMessage" not in script
    rows = script.index("await Promise.all(Object.values(_rows));")
    prov = script.index("label: 'chatty-overseer'")
    survey = script.index("label: 'chatty-survey'")
    cont = script.index("_continue['C1'] =")
    summary = script.index("label: 'chatty-summary'")
    assert prov < script.index("_rows['C1'] =") < rows < survey < cont < summary


def test_continuation_briefs_carry_continued_by_predecessor_and_own_nonce():
    script = _script(chatty=True)
    assert script.count("_continue['C") == 3
    assert "continued_by" in script and "Read its transcript" in script
    assert "'CHATTY CONTINUATION" in script.replace("\\n", "") or "CHATTY CONTINUATION" in script
    assert "MY-NONCE=' + _n + '" in script
    brief = chatty.continuation_brief("C1")
    assert brief.count("MY-NONCE=" + chatty.NONCE_SLOT) == 1
    assert "continued_by" in brief and "transcript" in brief
    assert "first user record" in brief


def test_chatty_flag_reaches_the_emitter(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import cli

    seen = []
    monkeypatch.setattr(cli, "_dispatch_emit", lambda params, repo_root=None: seen.append(params) or {"ok": False})
    for extra in ([], ["--chatty"]):
        cli.main(["--plan", "p.md", "--out", str(tmp_path / "o.workflow.mjs"), *extra])
    assert "chatty" not in seen[0]
    assert seen[1]["chatty"] is True


def _all_briefs(roster):
    out = {chatty.OVERSEER_ROLE: chatty.overseer_prompt(roster)}
    for m in roster["members"]:
        if m["role"] != chatty.OVERSEER_ROLE:
            out[m["role"]] = chatty.member_brief(m["role"], m["nonce"])
    out["survey"] = chatty.survey_prompt("0123456789abcdef")
    out["summary"] = chatty.summary_prompt("fedcba9876543210")
    return out


def test_each_brief_carries_exactly_its_own_my_nonce():
    roster = chatty.build_roster("r", ["a", "b"])
    briefs = _all_briefs(roster)
    for role, text in briefs.items():
        if role in ("survey", "summary"):
            assert len(set(re.findall(r"MY-NONCE=([0-9a-f]{16})", text))) == 1
            continue
        own = chatty.member_nonce(roster, role)
        assert text.count("MY-NONCE=" + own) == 1
        assert set(re.findall(r"MY-NONCE=([0-9a-f]{16})", text)) == {own}
    assert len({m["nonce"] for m in roster["members"]}) == 3


def test_roster_text_never_contains_my_nonce_form():
    roster = chatty.build_roster("r", ["a", "b"])
    text = chatty.overseer_prompt(roster)
    embedded = text[text.index("ROSTER:"):]
    assert "MY-NONCE=" not in embedded
    assert "MY-NONCE=" not in json.dumps(roster)
    for m in roster["members"]:
        assert m["nonce"] in embedded


def test_self_id_checks_line_2_and_fails_closed():
    roster = chatty.build_roster("r", ["a"])
    for text in _all_briefs(roster).values():
        assert "sed -n" not in text
        assert '"type\\"==\\"user' in text.replace(" ", "") or "first user record" in text
        assert "grep -rl" not in text
        assert "Zero or more than one: stop and report" in text
