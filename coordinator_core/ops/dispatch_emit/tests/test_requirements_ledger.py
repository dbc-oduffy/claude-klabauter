"""requirements_ledger: the run's PM requirements as requirement_register rows -- bind, revise,
the view stages read, the one pass rule, and the write-back after the commit."""

from __future__ import annotations

import subprocess
import time

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261013T000000-abc123"
REL = "state/sizings/2026-10-13-ledger.yaml"


def _sizing(**extra):
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}, {"turn": 2, "text": "keep it under 500ms"}],
    }
    doc.update(extra)
    return doc


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    return tmp_path


@pytest.fixture
def run(repo):
    rl.start(repo, RUN, all_of_pm_ask=False)
    rl.bind(repo, RUN, REL, ask_text="a raw ask")
    return repo, repo / RECORD_DIR_ROOT / RUN


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _revise(root, **params):
    return rl._handler({"run_id": RUN, **params}, repo_root=root)


def test_bind_seeds_the_register_in_order_and_is_idempotent(run):
    repo, _d = run
    rl.bind(repo, RUN, REL, ask_text="a raw ask")
    rows = _rows(repo)
    assert [(r["id"], r["source_text"], r["anchor"], r["status"]) for r in rows] == [
        ("R1", "build the op", "pm_verbatims", "open"),
        ("R2", "keep it under 500ms", "pm_verbatims", "open"),
        ("R3", "a raw ask", "ask", "open"),
    ]
    ledger = rl.read_ledger(repo, run[1])
    assert ledger.sizing_rel == REL and ledger.all_of_pm_ask is False
    # One list of what the PM asked: no second store beside the register.
    assert not any(p.suffix == ".jsonl" for p in run[1].iterdir())


def test_a_run_emitted_without_a_ledger_is_left_alone(repo):
    assert rl.bind(repo, "other-run", REL) is None
    assert rr.read_register((repo / REL).read_text(encoding="utf-8")) is None
    assert rl.read_ledger(repo, repo / RECORD_DIR_ROOT / "other-run") is None


def test_revise_appends_a_stamped_row_and_rewrites_the_view(run):
    repo, d = run
    out = _revise(repo, verbatim="also cover Windows paths")
    assert out["ok"] is True and out["item"] == "R4" and out["items"] == 4 and out["sizing"] == REL
    assert out["ledger"] == f"{RECORD_DIR_ROOT}/{RUN}/{rl.LEDGER_VIEW_FILE}"
    row = _rows(repo)[-1]
    assert row["anchor"].startswith("revision@") and row["anchor"].endswith("Z")
    view = (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert view.index("## R1") < view.index("## R4 (revision") and "> also cover Windows paths" in view


def test_a_revision_before_the_sizing_exists_joins_the_register_at_bind(repo):
    rl.start(repo, RUN)
    out = _revise(repo, verbatim="early revision")
    assert out["ok"] is True and out["item"] == "Q1" and out["sizing"] is None
    rl.bind(repo, RUN, REL, ask_text="the ask")
    rows = _rows(repo)
    assert [r["source_text"] for r in rows] == ["build the op", "keep it under 500ms", "the ask", "early revision"]
    assert rows[-1]["anchor"].startswith("revision@")
    assert not (repo / RECORD_DIR_ROOT / RUN / rl.INBOX_FILE).exists()


def test_bind_refuses_a_second_sizing(run):
    repo, _d = run
    other = "state/sizings/other.yaml"
    (repo / other).write_text(yaml.safe_dump(_sizing()), encoding="utf-8")
    with pytest.raises(rl.LedgerRefused, match="is bound to"):
        rl.bind(repo, RUN, other)


def test_revise_sets_all_of_pm_ask_alone(run):
    repo, d = run
    out = _revise(repo, all_of_pm_ask=True)
    assert out["ok"] is True and out["item"] is None and out["all_of_pm_ask"] is True
    assert "(all_of_pm_ask): yes" in (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "params, needle",
    [
        ({}, "needs a non-empty verbatim"),
        ({"verbatim": "   "}, "needs a non-empty verbatim"),
        ({"verbatim": 3}, "must be a string"),
        ({"run_id": "../x", "verbatim": "v"}, "safe path segment"),
        ({"run_id": "never-emitted", "verbatim": "v"}, "emit it first"),
    ],
)
def test_revise_refusals_name_the_cause(run, params, needle):
    repo, _d = run
    out = rl._handler({"run_id": RUN, **params}, repo_root=repo)
    assert needle in out["error"]


def test_run_live_reports_the_unredeemed_run_token(run):
    repo, _d = run
    assert _revise(repo, verbatim="a")["run_live"] is False
    token = run_token.mint(repo, RUN, "s.mjs")
    assert _revise(repo, verbatim="b")["run_live"] is True
    assert run_token.redeem(repo, RUN, token, "s.mjs")
    assert _revise(repo, verbatim="c")["run_live"] is False


def test_a_non_live_run_replies_the_refire_next_and_a_live_one_does_not(run):
    from coordinator_core.warp_fire import fire_command

    repo, _d = run
    out = _revise(repo, verbatim="a")
    assert out["next"] == {"kind": "refire", "call": fire_command(REL)}
    assert "refire" in out["note"]
    run_token.mint(repo, RUN, "s.mjs")
    live = _revise(repo, verbatim="b")
    assert "next" not in live and "note" not in live


def test_a_non_live_run_with_no_sizing_names_the_raw_ask_form(repo):
    rl.start(repo, RUN)
    out = _revise(repo, verbatim="early")
    assert out["sizing"] is None and '"ask"' in out["next"]["call"] and '"sizing"' not in out["next"]["call"]


def test_revise_spawns_nothing_and_holds_the_bar(run, monkeypatch):
    repo, _d = run

    def _boom(*_a, **_k):
        raise AssertionError("workflow.revise spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    started = time.process_time()
    for n in range(20):
        assert _revise(repo, verbatim=f"revision {n}")["ok"] is True
    assert (time.process_time() - started) / 20 < 0.5


def _verdict(*rows):
    return {"items": [dict(zip(("id", "status"), r[:2]), **(r[2] if len(r) > 2 else {})) for r in rows]}


def _ledger(run):
    return rl.read_ledger(run[0], run[1])


def test_judge_passes_every_item_met(run):
    assert rl.judge(_ledger(run), _verdict(("R1", "met"), ("R2", "met"), ("R3", "met"))) == {"pass": True, "failures": [], "open": []}


def test_judge_fails_drift_and_an_unjudged_revision(run):
    _revise(run[0], verbatim="late revision")
    out = rl.judge(_ledger(run), _verdict(("R1", "met"), ("R2", "drifted"), ("R3", "met")))
    assert not out["pass"]
    assert {f["id"] for f in out["failures"]} == {"R2", "R4"}


def test_superseded_needs_a_later_revision(run):
    _revise(run[0], verbatim="drop the 500ms bar")
    ledger = _ledger(run)
    ok = _verdict(("R1", "met"), ("R2", "superseded", {"superseded_by": "R4"}), ("R3", "met"), ("R4", "met"))
    assert rl.judge(ledger, ok)["pass"]
    out = rl.judge(ledger, _verdict(("R1", "superseded", {"superseded_by": "R3"}), ("R2", "met"), ("R3", "met"), ("R4", "met")))
    assert [f["id"] for f in out["failures"]] == ["R1"]


def test_all_of_pm_ask_makes_an_inessential_item_mandatory(run):
    verdict = _verdict(("R1", "met"), ("R2", "not_met", {"essential": False}), ("R3", "met"))
    assert rl.judge(_ledger(run), verdict)["pass"] is True
    _revise(run[0], all_of_pm_ask=True)
    out = rl.judge(_ledger(run), verdict)
    assert not out["pass"] and "all_of_pm_ask" in out["failures"][0]["reason"]


def test_not_met_defaults_to_essential(run):
    assert not rl.judge(_ledger(run), _verdict(("R1", "met"), ("R2", "not_met"), ("R3", "met")))["pass"]


@pytest.mark.parametrize("all_of", [False, True])
def test_a_not_met_row_withheld_by_a_gated_chunk_passes_and_is_open(run, all_of):
    if all_of:
        _revise(run[0], all_of_pm_ask=True)
    verdict = _verdict(("R1", "met"), ("R2", "not_met", {"withheld_by": "C9"}), ("R3", "met"))
    out = rl.judge(_ledger(run), verdict, gated={"C9"})
    assert out == {"pass": True, "failures": [], "open": [{"id": "R2", "reason": "withheld_by C9"}]}


@pytest.mark.parametrize("gated", [None, [], {"C1"}])
def test_withheld_by_naming_no_gated_chunk_fails(run, gated):
    verdict = _verdict(("R1", "met"), ("R2", "not_met", {"withheld_by": "C7"}), ("R3", "met"))
    out = rl.judge(_ledger(run), verdict, gated=gated)
    assert not out["pass"] and out["open"] == []
    assert out["failures"] == [
        {"id": "R2", "reason": "withheld_by C7 names no gated chunk of the run's manifest"}
    ]


def test_an_essential_not_met_row_still_fails_beside_a_withheld_one(run):
    verdict = _verdict(("R1", "not_met"), ("R2", "not_met", {"withheld_by": "C9"}), ("R3", "met"))
    out = rl.judge(_ledger(run), verdict, gated={"C9"})
    assert [f["id"] for f in out["failures"]] == ["R1"]
    assert out["open"] == [{"id": "R2", "reason": "withheld_by C9"}]


def test_open_lists_withheld_and_non_essential_rows_in_ledger_order(run):
    verdict = _verdict(
        ("R3", "not_met", {"essential": False}), ("R2", "not_met", {"withheld_by": "C9"}), ("R1", "met")
    )
    out = rl.judge(_ledger(run), verdict, gated=("C9",))
    assert out["pass"] is True
    assert out["open"] == [
        {"id": "R2", "reason": "withheld_by C9"}, {"id": "R3", "reason": "non-essential"},
    ]


def test_judge_refuses_unknown_and_duplicate_ids_and_a_shapeless_verdict(run):
    ledger = _ledger(run)
    out = rl.judge(ledger, _verdict(("R1", "met"), ("R1", "met"), ("R2", "met"), ("R3", "met"), ("R9", "met")))
    assert {(f["id"], f["reason"]) for f in out["failures"]} == {("R1", "judged twice"), ("R9", "names no requirement row")}
    assert not rl.judge(ledger, None)["pass"]


def test_apply_verdict_writes_met_wired_and_superseded_rows(run):
    repo, _d = run
    _revise(repo, verbatim="drop the 500ms bar")
    verdict = _verdict(
        ("R1", "met", {"wired": True}), ("R2", "superseded", {"superseded_by": "R4"}),
        ("R3", "met", {"wired": False}), ("R4", "met", {"wired": True}),
    )
    rl.apply_verdict(repo, REL, verdict, run_id=RUN, sha="abc1234")
    rows = {r["id"]: r for r in _rows(repo)}
    assert rows["R1"]["status"] == "met" and rows["R1"]["met_by"] == [f"{RUN}@abc1234"]
    assert rows["R2"]["status"] == "waived" and rows["R2"]["ruling"]["quote"] == "drop the 500ms bar"
    assert rows["R3"]["status"] == "partial" and rows["R3"]["wired"] is False
    # A waived row is ruled: it leaves the set the next check must judge.
    assert [i.id for i in _ledger(run).items] == ["R1", "R3", "R4"]


def test_judge_passes_a_post_commit_item_and_the_landing_writes_it_back(run):
    repo, _d = run
    assert "post_commit" in rl.STATUSES and rl.POST_COMMIT_ACTS == ("commit", "sha_reply", "sha_memo")
    verdict = _verdict(
        ("R1", "post_commit", {"post_commit_acts": ["commit", "sha_reply"], "wired": True}),
        ("R2", "met", {"wired": True}), ("R3", "met", {"wired": True}),
    )
    assert rl.judge(_ledger(run), verdict) == {"pass": True, "failures": [], "open": []}
    rl.apply_verdict(repo, REL, verdict, run_id=RUN, sha="abc1234")
    rows = {r["id"]: r for r in _rows(repo)}
    assert rows["R1"]["status"] == "met" and rows["R1"]["met_by"] == [f"{RUN}@abc1234"]


@pytest.mark.parametrize("extra,needle", [
    ({}, "no post-commit act"),
    ({"post_commit_acts": []}, "no post-commit act"),
    ({"post_commit_acts": ["push"]}, "'push'"),
])
def test_judge_refuses_a_post_commit_item_without_a_valid_act(run, extra, needle):
    out = rl.judge(_ledger(run), _verdict(("R1", "post_commit", extra), ("R2", "met"), ("R3", "met")))
    assert not out["pass"] and needle in out["failures"][0]["reason"]


def test_a_sha_memo_item_is_written_partial_and_owed(run):
    repo, _d = run
    verdict = _verdict(
        ("R1", "post_commit", {"post_commit_acts": ["commit", "sha_memo"], "wired": True}),
        ("R2", "met", {"wired": True}), ("R3", "met", {"wired": True}),
    )
    assert rl.judge(_ledger(run), verdict)["pass"]
    rl.apply_verdict(repo, REL, verdict, run_id=RUN, sha="abc1234")
    rows = {r["id"]: r for r in _rows(repo)}
    assert rows["R1"]["status"] == "partial" and not rows["R1"].get("met_by")
    assert rl.post_commit_owed(verdict, "abc1234") == [
        {"id": "R1", "acts": ["commit", "sha_memo"], "sha": "abc1234"}
    ]


def test_not_met_with_only_a_commit_remaining_passes_and_lands_met(run):
    repo, _d = run
    verdict = _verdict(
        ("R1", "not_met", {"essential": True, "remaining": ["commit"], "wired": True}),
        ("R2", "met", {"wired": True}), ("R3", "met", {"wired": True}),
    )
    assert rl.judge(_ledger(run), verdict) == {"pass": True, "failures": [], "open": []}
    assert verdict["items"][0]["status"] == "not_met"
    rl.apply_verdict(repo, REL, verdict, run_id=RUN, sha="abc1234")
    rows = {r["id"]: r for r in _rows(repo)}
    assert rows["R1"]["status"] == "met" and rows["R1"]["met_by"] == [f"{RUN}@abc1234"]


def test_not_met_with_a_sha_memo_remaining_lands_partial_and_owed(run):
    repo, _d = run
    verdict = _verdict(
        ("R1", "not_met", {"remaining": ["commit", "sha_memo"], "wired": True}),
        ("R2", "met", {"wired": True}), ("R3", "met", {"wired": True}),
    )
    assert rl.judge(_ledger(run), verdict)["pass"]
    rl.apply_verdict(repo, REL, verdict, run_id=RUN, sha="abc1234")
    assert {r["id"]: r for r in _rows(repo)}["R1"]["status"] == "partial"
    assert rl.post_commit_owed(verdict, "abc1234") == [
        {"id": "R1", "acts": ["commit", "sha_memo"], "sha": "abc1234"}
    ]


@pytest.mark.parametrize("extra", [
    {"remaining": ["commit", "the test is red"]},
    {"remaining": []},
    {},
])
def test_not_met_with_a_pre_commit_gap_or_no_remaining_stays_not_met(run, extra):
    verdict = _verdict(("R1", "not_met", extra), ("R2", "met"), ("R3", "met"))
    assert rl.normalize_verdict(verdict)["items"][0]["status"] == "not_met"
    assert [f["id"] for f in rl.judge(_ledger(run), verdict)["failures"]] == ["R1"]


def test_pm_words_prefers_verbatims_and_never_takes_an_em_paraphrase():
    assert rl.pm_words({"pm_verbatims": [{"text": " a "}, {"text": ""}], "intent": "x"}) == ["a"]
    assert rl.pm_words({"intent": "do x", "intent_source": "pm-verbatim"}) == ["do x"]
    assert rl.pm_words({"intent": "do x", "intent_source": "em-elaborated"}) == []


def test_a_relayed_row_is_a_requirement_and_never_supersedes_a_pm_verbatim(repo):
    rl.start(repo, RUN, all_of_pm_ask=False)
    rl.bind(repo, RUN, REL, relayed_text="relayed words")
    rows = _rows(repo)
    assert [(r["source_text"], r["anchor"]) for r in rows] == [("relayed words", "relayed")]
    ledger = rl.read_ledger(repo, repo / RECORD_DIR_ROOT / RUN)
    assert ledger.items[-1].origin == "relayed"
    assert "relayed" in (repo / RECORD_DIR_ROOT / RUN / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


def test_a_relayed_bind_seeds_no_pm_verbatim_rows(repo):
    rl.start(repo, RUN, all_of_pm_ask=False)
    rl.bind(repo, RUN, REL, relayed_text="relayed words")
    assert not [r for r in _rows(repo) if r["anchor"] == "pm_verbatims"]
    assert len(rl.read_ledger(repo, repo / RECORD_DIR_ROOT / RUN).items) == 1


def test_a_relayed_row_never_supersedes_a_pm_verbatim_row(run):
    repo, d = run
    ledger = rl.read_ledger(repo, d)
    out = rl.judge(ledger, {"items": [
        {"id": "R1", "status": "superseded", "superseded_by": "R3"},
        {"id": "R2", "status": "met"}, {"id": "R3", "status": "met"},
    ]})
    assert not out["pass"] and out["failures"][0]["id"] == "R1"


def test_routed_item_must_name_the_engine_root_it_read(run, tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir()
    ledger = _ledger(run)
    routed = {"R1": str(engine)}
    base = [("R2", "met"), ("R3", "met")]
    ok = rl.judge(ledger, _verdict(("R1", "met", {"root_read": [str(engine), "cwd"]}), *base), routed)
    assert ok["pass"]
    assert rl.judge(ledger, _verdict(("R1", "met", {"root_read": str(engine)}), *base), routed)["pass"]
    for extra in ({"root_read": str(run[0])}, {}):
        out = rl.judge(ledger, _verdict(("R1", "met", extra), *base), routed)
        assert [f["id"] for f in out["failures"]] == ["R1"]
        assert "owed the engine root" in out["failures"][0]["reason"]


def test_unrouted_judgment_ignores_root_read(run):
    verdict = _verdict(("R1", "met", {"root_read": "anywhere"}), ("R2", "met"), ("R3", "met"))
    assert rl.judge(_ledger(run), verdict) == rl.judge(_ledger(run), verdict, {}) == {"pass": True, "failures": [], "open": []}
