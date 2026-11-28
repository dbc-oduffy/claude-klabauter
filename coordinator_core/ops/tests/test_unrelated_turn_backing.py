"""A PM turn listed under `unrelated_turns` needs a quoted reason and a ruling or rule-out backing."""
from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr

PM_TEXT = "please keep the old exporter around until the migration lands"
QUOTE = '"keep the old exporter"'
OTHER = "and also rename the module to something shorter"

DOC = {
    "pm_verbatims": [
        {"turn": 1, "text": PM_TEXT},
        {"turn": 2, "text": OTHER},
        {"turn": 3, "text": "<agent-message>relayed note</agent-message>"},
    ]
}


def _errors(reason, turn=1, *, root=None, prior=None, doc=DOC):
    reg = {rr.UNRELATED_TURNS_KEY: [{"turn": turn, "reason": reason}]}
    return rr.unrelated_turn_errors(doc, reg, root=root, prior=prior)


def _ruling_root(tmp_path, rid="r-1", status="accepted"):
    d = tmp_path / "state" / "pm-rulings"
    d.mkdir(parents=True)
    body = {"id": rid, "pm_turn": "x R1", "scope": ["repo"], "text": "t", "status": status}
    if status == "superseded":
        body["superseded_by"] = "r-2"
        (d / "r-2.yaml").write_text(
            yaml.safe_dump({"id": "r-2", "pm_turn": "x R2", "scope": ["repo"], "text": "t"}), encoding="utf-8"
        )
    (d / f"{rid}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return tmp_path


def test_a_reason_with_no_quote_is_refused():
    assert _errors("not relevant ruling:r-1")


def test_a_quote_absent_from_the_turn_is_refused():
    assert _errors('"something the PM never said" rule-out:T2')


def test_a_quote_under_the_minimum_is_refused():
    assert _errors('"keep" rule-out:T2')


def test_a_quote_without_backing_is_refused():
    errs = _errors(f"stale {QUOTE}")
    assert errs and "backing token" in errs[0]


def test_a_token_inside_the_quote_is_not_a_token():
    doc = {"pm_verbatims": [{"turn": 1, "text": "see ruling:r-1 for the ruling:r-1 wording"}, {"turn": 2, "text": OTHER}]}
    assert _errors('"ruling:r-1 for the ruling:r-1"', doc=doc)


def test_an_unknown_ruling_is_refused(tmp_path):
    root = _ruling_root(tmp_path)
    errs = _errors(f"{QUOTE} ruling:nope", root=root)
    assert errs and "not in the ruling register" in errs[0]


def test_a_superseded_ruling_is_refused(tmp_path):
    root = _ruling_root(tmp_path, status="superseded")
    errs = _errors(f"{QUOTE} ruling:r-1", root=root)
    assert errs and "superseded" in errs[0]


def test_a_ruling_with_no_root_is_refused_naming_the_root():
    errs = _errors(f"{QUOTE} ruling:r-1")
    assert errs and "no root" in errs[0]


def test_an_accepted_ruling_backs_the_entry(tmp_path):
    root = _ruling_root(tmp_path)
    assert _errors(f"superseded by policy: {QUOTE} ruling:r-1.", root=root) == []


def test_a_rule_out_turn_naming_itself_is_refused():
    errs = _errors(f"{QUOTE} rule-out:T1")
    assert errs and "itself" in errs[0]


def test_a_rule_out_turn_not_embedded_is_refused():
    errs = _errors(f"{QUOTE} rule-out:T9")
    assert errs and "not an embedded PM turn" in errs[0]


def test_a_rule_out_turn_with_no_pm_words_is_refused():
    assert _errors(f"{QUOTE} rule-out:T3")


def test_a_valid_rule_out_turn_backs_the_entry():
    assert _errors(f"{QUOTE} rule-out:T2") == []


def test_both_tokens_are_refused(tmp_path):
    root = _ruling_root(tmp_path)
    assert _errors(f"{QUOTE} ruling:r-1 rule-out:T2", root=root)


def test_an_unchanged_prior_entry_is_grandfathered_and_a_changed_one_is_not():
    reason = "chat"
    assert _errors(reason)
    prior = {rr.UNRELATED_TURNS_KEY: [{"turn": 1, "reason": reason}]}
    assert _errors(reason, prior=prior) == []
    assert _errors("chat, reworded", prior=prior)


def test_an_agent_message_only_turn_stays_exempt():
    assert _errors("relayed agent message only, no PM words", turn=3) == []


def test_an_empty_reason_and_an_unembedded_turn_are_still_refused():
    assert _errors("  ")
    assert _errors("x", turn=9)
