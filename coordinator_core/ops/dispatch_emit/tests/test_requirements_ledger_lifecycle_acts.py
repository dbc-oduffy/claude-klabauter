"""requirements_ledger lifecycle acts: baton_archive and plan_implemented as post-commit acts."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import design_conformance as dc
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261022T000000-abc123"
REL = "state/sizings/2026-10-22-lifecycle.yaml"
LIFE = ["baton_archive", "plan_implemented"]
BOARD = "https://claude.ai/design/p/abc123"
HIT = {"id": "settings", "board_url": BOARD, "artboards": ["A1"], "status": "designed", "paths": ["app/settings/page.tsx"]}


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN


def _ledger(run):
    return rl.read_ledger(run[0], run[1])


def _item(rid="R1", remaining=None, **extra):
    return {"id": rid, "status": "not_met", "remaining": remaining if remaining is not None else LIFE, **extra}


def _verdict(run, **replaced):
    return {"items": [replaced.get(i.id) or {"id": i.id, "status": "met", "wired": True} for i in _ledger(run).items]}


def test_lifecycle_vocabulary_is_pinned():
    assert rl.LIFECYCLE_ACTS == ("baton_archive", "plan_implemented")
    assert rl.POST_COMMIT_ACTS[:3] == ("commit", "sha_reply", "sha_memo")
    assert rl.CLOSE_DISCHARGED_ACTS == ("commit", "baton_archive", "plan_implemented")


def test_normalize_turns_lifecycle_remaining_into_post_commit():
    out = rl.normalize_verdict({"items": [_item()]})["items"][0]
    assert out["status"] == "post_commit" and out["post_commit_acts"] == LIFE


def test_judge_passes_a_lifecycle_only_item(run):
    verdict = _verdict(run, R1={**_item("R1"), "wired": True})
    assert rl.judge(_ledger(run), verdict) == {"pass": True, "failures": [], "open": []}


def test_judge_fails_a_lifecycle_act_mixed_with_a_plain_words_gap(run):
    out = rl.judge(_ledger(run), _verdict(run, R1=_item("R1", ["plan_implemented", "typecheck not run"])))
    assert not out["pass"] and [f["id"] for f in out["failures"]] == ["R1"]


@pytest.mark.parametrize("acts,expected", [
    (["commit", "plan_implemented"], True),
    (LIFE, True),
    (["sha_reply"], False),
    (["plan_implemented", "sha_memo"], False),
    ([], False),
])
def test_close_dischargeable(acts, expected):
    assert rl.close_dischargeable(_item(remaining=acts)) is expected
    assert rl.close_dischargeable({"id": "R1", "status": "post_commit", "post_commit_acts": acts}) is expected


def test_close_dischargeable_refuses_other_statuses():
    assert rl.close_dischargeable({"id": "R1", "status": "met"}) is False
    assert rl.close_dischargeable({"id": "R1", "status": "not_met", "remaining": ["typecheck"]}) is False


def test_post_commit_owed_lists_an_undischarged_lifecycle_act():
    verdict = {"items": [_item("R1", ["plan_implemented"])]}
    assert rl.post_commit_owed(verdict, "abc1234") == [{"id": "R1", "acts": ["plan_implemented"], "sha": "abc1234"}]
    assert rl.post_commit_owed(verdict, "abc1234", discharged=["plan_implemented"]) == []
    both = {"items": [_item("R1", LIFE)]}
    assert rl.post_commit_owed(both, "abc1234", discharged={"baton_archive"}) == [
        {"id": "R1", "acts": ["plan_implemented"], "sha": "abc1234"}
    ]


def test_post_commit_owed_keeps_the_sha_memo_entry_unchanged():
    verdict = {"items": [_item("R1", ["commit", "sha_memo"]), _item("R2", ["commit", "sha_reply"])]}
    assert rl.post_commit_owed(verdict, "abc1234") == [{"id": "R1", "acts": ["commit", "sha_memo"], "sha": "abc1234"}]


def test_judge_still_fails_post_commit_on_a_design_registry_row(run):
    root, _ = run
    dc.mint_rows(root, REL, [HIT])
    ledger = _ledger(run)
    design = next(i for i in ledger.items if i.anchor == "design-registry:settings")
    items = [{"id": i.id, "status": "met"} for i in ledger.items if i.id != design.id]
    items.append(_item(design.id, LIFE))
    out = rl.judge(ledger, {"items": items}, repo_root=root)
    assert not out["pass"] and design.id in [f["id"] for f in out["failures"]]
