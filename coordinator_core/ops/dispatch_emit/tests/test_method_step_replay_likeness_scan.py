"""Likeness-scan replay: a PM method stated as lettered steps, judged through the ledger-verify call.

Pinned falsifier: the three refusal cases (name-only mechanism, a different mechanism with no
deviation, a mechanism absent from the tree while the metric moved) fail `requirements_ledger.judge`
only through its method-step branch. `test_refusals_vanish_when_the_judge_branch_is_reverted`
reverts that branch and shows all three pass the ledger, which is the baseline defect.
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import method_step
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-11-02-likeness-scan.yaml"
RUN = "ask-20261102T000000-aaaaaa"
BRIEF = (
    "The likeness method has three steps: (a) capture scans. "
    "(b) map into MetaHuman expected-value space. (c) apply."
)
PROBE_FILE = "likeness/mapper.py"
PROBE = "def map_to_expected_value_space("


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "likeness").mkdir()
    (tmp_path / PROBE_FILE).write_text(f"{PROBE}landmarks):\n    return landmarks\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "likeness-scan", "intent": "likeness",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-likeness-scan-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-02", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "match the scan likeness"}],
        "em_analysis": {"em_brief": BRIEF},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="the ask")
    return tmp_path


def _ledger(repo):
    return rl.read_ledger(repo, rl.run_dir(repo, RUN))


def _step_ids(ledger):
    steps = {}
    for item in ledger.items:
        step = method_step.split_anchor(item.anchor)[1]
        if step:
            steps[step.rpartition("/")[2]] = item.id
    return steps


def _fence(row, mechanism, contains=PROBE, deviation=""):
    return (
        f"```yaml method-trace\nrow: {row}\nmechanism: {mechanism}\n"
        f"at: {{path: {PROBE_FILE}, contains: '{contains}'}}\n{deviation}```\n"
    )


def _plan(ids, b_fence):
    return (
        _fence(ids["a"], "capture scans from the scanner device")
        + b_fence
        + _fence(ids["c"], "apply the mapped values to the face rig")
    )


def _verdict(ledger, **per_row):
    return {"items": [{"id": i.id, "status": "met", **per_row.get(i.id, {})} for i in ledger.items]}


def _reasons(out, rid):
    return [f["reason"] for f in out["failures"] if f["id"] == rid]


GOOD_B = "map scan landmarks into MetaHuman expected-value space"
LIKENESS_SCAN_SUBSTITUTION = "conform scan values onto the MetaHuman mesh"


def test_register_mints_one_marked_row_per_step(repo):
    ids = _step_ids(_ledger(repo))
    assert sorted(ids) == ["a", "b", "c"]


def test_traced_present_mechanism_passes(repo):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    out = rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=_plan(ids, _fence(ids["b"], GOOD_B)))
    assert out["pass"], out


def test_name_only_mechanism_is_refused(repo):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    plan = _plan(ids, _fence(ids["b"], "the likeness method step b"))
    out = rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=plan)
    assert not out["pass"]
    assert any("names the step" in r for r in _reasons(out, ids["b"])), out
    assert not _reasons(out, ids["a"]) and not _reasons(out, ids["c"])


def test_likeness_scan_substitution_without_deviation_is_refused_though_it_shares_a_noun(repo):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    shared = method_step._words(LIKENESS_SCAN_SUBSTITUTION) & method_step._words("map into MetaHuman expected-value space")
    assert shared == {"metahuman"}
    plan = _plan(ids, _fence(ids["b"], LIKENESS_SCAN_SUBSTITUTION))
    out = rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=plan)
    assert not out["pass"]
    assert any("under half" in r for r in _reasons(out, ids["b"])), out


def test_substitution_with_a_ratified_deviation_passes(repo):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    dev = "deviation: {delivers_by: mesh conform, reason: no mapper yet, ratified_by: PM ruling}\n"
    plan = _plan(ids, _fence(ids["b"], LIKENESS_SCAN_SUBSTITUTION, deviation=dev))
    assert rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=plan)["pass"]


def test_substitution_with_an_unratified_deviation_is_refused(repo):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    dev = "deviation: {delivers_by: mesh conform, reason: no mapper yet}\n"
    plan = _plan(ids, _fence(ids["b"], LIKENESS_SCAN_SUBSTITUTION, deviation=dev))
    out = rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=plan)
    assert any("ratified_by" in r for r in _reasons(out, ids["b"])), out


def test_mechanism_absent_from_tree_fails_while_the_metric_moved(repo):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    plan = _plan(ids, _fence(ids["b"], GOOD_B, contains="def never_written("))
    verdict = _verdict(ledger, **{ids["b"]: {"note": "likeness metric moved 0.61 -> 0.88"}})
    out = rl.judge(ledger, verdict, repo_root=repo, plan_text=plan)
    assert not out["pass"]
    assert any("absent" in r for r in _reasons(out, ids["b"])), out
    assert all(o["id"] != ids["b"] for o in out["open"])


def test_refusals_vanish_when_the_judge_branch_is_reverted(repo, monkeypatch):
    ledger = _ledger(repo)
    ids = _step_ids(ledger)
    cases = [
        _fence(ids["b"], "the likeness method step b"),
        _fence(ids["b"], LIKENESS_SCAN_SUBSTITUTION),
        _fence(ids["b"], GOOD_B, contains="def never_written("),
    ]
    for b in cases:
        assert not rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=_plan(ids, b))["pass"]
    monkeypatch.setattr(method_step, "is_method_step", lambda row: False)
    for b in cases:
        assert rl.judge(ledger, _verdict(ledger), repo_root=repo, plan_text=_plan(ids, b))["pass"]
