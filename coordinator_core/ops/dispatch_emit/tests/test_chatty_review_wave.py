"""Review-wave template: roster validity, per-baton nonce isolation, brief contract."""

from __future__ import annotations

import copy
import json

import pytest

from coordinator_core.ops.dispatch_emit import chatty


def _fill(wave: dict, baton: str) -> dict:
    return json.loads(json.dumps(wave).replace(chatty.BATON_SLOT, baton))


def _texts(wave: dict) -> list[str]:
    return [
        wave["overseerPrompt"],
        wave["surveyPrompt"],
        wave["summaryPrompt"],
        *wave["memberBriefs"].values(),
        *wave["continuationBriefs"].values(),
    ]


def test_filled_roster_validates_with_overseer_and_three_reviewers():
    wave = _fill(chatty.review_wave_args("r1"), "B1")
    roster = wave["roster"]
    assert chatty.validate_roster(roster) == []
    assert [m["role"] for m in roster["members"]] == ["overseer", "review-1", "review-2", "review-3"]
    assert len({m["nonce"] for m in roster["members"]}) == 4
    assert roster["run_id"] == "r1"


def test_review_slot_bounds():
    assert chatty.review_slot(0) == "review-1"
    with pytest.raises(ValueError):
        chatty.review_slot(chatty.REVIEW_WAVE_SLOTS)
    with pytest.raises(ValueError):
        chatty.review_slot(-1)


def test_member_briefs_carry_the_wave_contract():
    wave = chatty.review_wave_args("r1")
    for slot, brief in wave["memberBriefs"].items():
        nonce = chatty.member_nonce(wave["roster"], slot)
        assert f"MY-NONCE={nonce}" in brief
        for literal in (
            "CHATTY RUN.",
            "exact-text Edit",
            "CLAIM <region>",
            "RELEASE <region>",
            "the lower review-N holds",
            "A returned slot holds nothing",
            "{PEERS}",
        ):
            assert literal in brief, literal


def test_every_text_scopes_to_the_baton_wave_directory():
    wave = chatty.review_wave_args("r1")
    for text in _texts(wave):
        assert "<run directory>/{BATON}/" in text
        assert "<run-dir>/agent-<agent_id>.jsonl" in text
        assert "the EM mailbox <run-dir>/mail/_em.jsonl" in text
    for nonce in (m["nonce"] for m in wave["roster"]["members"]):
        assert nonce.startswith("{BATON}-")
    assert wave["batonSlot"] == "{BATON}"


def test_paths_move_into_baton_dir_but_em_mailbox_and_transcript_stay():
    wave = chatty.review_wave_args("r1")
    brief = wave["memberBriefs"]["review-1"]
    assert "<run-dir>/{BATON}/mail/review-1.jsonl" in brief
    assert "<run-dir>/mail/_em.jsonl" in brief
    assert "<run-dir>/{BATON}/mail/_em.jsonl" not in brief
    cont = wave["continuationBriefs"]["review-2"]
    assert "<run-dir>/{BATON}/roster.json" in cont
    assert "<run-dir>/agent-<its agent_id>.jsonl" in cont
    assert "<run directory>/{BATON}/roster.json" in wave["overseerPrompt"]


def test_two_batons_get_disjoint_nonces_even_when_one_id_prefixes_the_other():
    wave = chatty.review_wave_args("r1")
    a, b = _fill(wave, "A"), _fill(wave, "AB")
    na = {m["nonce"] for m in a["roster"]["members"]}
    nb = {m["nonce"] for m in b["roster"]["members"]}
    assert na.isdisjoint(nb)
    for x in na:
        for y in nb:
            assert f"MY-NONCE={x}" not in f"MY-NONCE={y}"
    assert chatty.validate_roster(a["roster"]) == chatty.validate_roster(b["roster"]) == []


def test_json_round_trip_and_fresh_nonces_per_call():
    wave = chatty.review_wave_args("r1")
    assert json.loads(json.dumps(wave)) == wave
    other = chatty.review_wave_args("r1")
    assert copy.deepcopy(wave["roster"]) != other["roster"]
