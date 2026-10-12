"""Pins for the in-run resolver (decide.py) and the workday-start rule table (rules.py).

Git-backed fixtures spawn real processes, so the module runs at the cadence tier.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workday_start import decide, rules
from coordinator_core.workday_start.contract import (
    DECISIONS_FILE,
    HANDBACK_DISPOSITIONS,
    RULE_IDS,
    Decision,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ORIENT_BRIEF = Path(__file__).resolve().parents[2] / "orient_brief"
DECIDE_SOURCE = Path(decide.__file__)


def _point(pid, dispositions, *, recommendation=None, reportable=None, evidence="ev"):
    return build_judgment_point(
        recommendation,
        id=pid,
        question=f"question {pid}",
        dispositions=[build_disposition(v) for v in dispositions],
        evidence=evidence,
        reason="r" if recommendation is None else "",
        reportable=reportable,
    )


def _resolve(points, directives=(), context=None):
    return decide.resolve(points, list(directives), rules=rules.POINT_RULES, context=context or {})


def _ids(items):
    return [i["id"] if isinstance(i, dict) else i.point_id for i in items]


def test_every_point_lands_in_exactly_one_list():
    points = [
        _point("j-rec", ["pin", "leave"], recommendation={"disposition": "pin", "rationale": "because"}),
        _point("j-handback-rec", ["surface_to_pm", "leave"],
               recommendation={"disposition": "surface_to_pm", "rationale": "x"}),
        _point("j-open", ["close_stubs", "leave_as_is"]),
        _point("j-only-person", ["surface_to_pm", "pm_reviews_manually"]),
        _point("j-memo-1", ["accept", "decline", "surface_to_pm"]),
        _point("j-note", ["defer"], recommendation={"disposition": "defer", "rationale": "n"}, reportable=True),
    ]
    out = _resolve(points)
    placed = _ids(out.decisions) + _ids(out.observations) + _ids(out.judgment_entries)
    assert sorted(placed) == sorted(p["id"] for p in points)
    assert len(placed) == len(set(placed))


def test_recommended_point_is_decided_by_recommendation():
    out = _resolve([_point("j-rec", ["pin", "leave"],
                           recommendation={"disposition": "pin", "rationale": "because"})])
    (decision,) = out.decisions
    assert (decision.decision, decision.rule, decision.decided_by) == ("pin", "recommendation", "rule")
    assert decision.rationale == "because"
    assert not out.judgment_entries


def test_point_recommending_a_handback_value_is_an_entry_not_a_decision():
    out = _resolve([_point("j-hb", ["surface_to_pm", "leave"],
                           recommendation={"disposition": "surface_to_pm", "rationale": "x"})])
    assert not out.decisions
    (entry,) = out.judgment_entries
    assert [d["value"] for d in entry["dispositions"]] == ["leave"]


def test_no_entry_offers_a_handback_disposition():
    out = _resolve([
        _point("a", ["surface_to_pm", "accept"]),
        _point("b", ["pm_reviews_manually", "leave_for_now", "pin"]),
    ])
    for entry in out.judgment_entries:
        assert not {d["value"] for d in entry["dispositions"]} & HANDBACK_DISPOSITIONS
    assert out.judgment_entries[0]["agent"] == decide.DEFAULT_AGENT


def test_routing_table_picks_the_agent():
    table = decide.RuleTable(point_rules=(), exhausted_rule="no-live-disposition",
                             routing={"j-special": "reviewer:x"})
    out = decide.resolve([_point("j-special-1", ["pin"]), _point("j-other", ["pin"])], [],
                         rules=table, context={})
    assert [e["agent"] for e in out.judgment_entries] == ["reviewer:x", decide.DEFAULT_AGENT]


def test_point_with_only_handback_dispositions_is_no_change():
    out = _resolve([_point("j-hb", ["surface_to_pm", "pm_reviews_manually"], evidence="producer X")])
    (decision,) = out.decisions
    assert (decision.decision, decision.rule) == ("no-change", "no-live-disposition")
    assert "producer X" in decision.evidence
    assert not out.judgment_entries


def test_reportable_gate_nothing_point_is_an_observation():
    out = _resolve([_point("j-note", ["leave"],
                           recommendation={"disposition": "leave", "rationale": "n"}, reportable=True)])
    assert _ids(out.observations) == ["j-note"]
    assert not out.decisions and not out.judgment_entries


@pytest.mark.parametrize("state", ["escalate", "inventory"])
def test_memo_point_is_routed_to_inbox_agents_whatever_the_assembler_state(state):
    context = {"inbox": {"state": state, "trigger": {"fires": state == "escalate", "open_count": 4},
                         "dispatches": [{"id": "triage-all"}]}}
    out = _resolve([_point("j-memo-1", ["accept", "decline", "surface_to_pm"]),
                    _point("j-overflow-memo", ["accept", "decline", "surface_to_pm"])], context=context)
    assert [d.decision for d in out.decisions] == ["routed-to-inbox-agents"] * 2
    assert all(d.rule == "memo-inbox" for d in out.decisions)
    assert '"open_count": 4' in out.decisions[0].evidence and "dispatches=1" in out.decisions[0].evidence
    assert not any(d.decision == "left-in-inbox" for d in out.decisions)


def _entry(pid="j-1", dispositions=("pin", "leave"), agent="coordinator:apm"):
    return {"id": pid, "question": "q", "evidence": "e", "agent": agent,
            "dispositions": [{"value": v, "resolves": []} for v in dispositions]}


def test_agent_result_with_undeclared_disposition_is_undecided(tmp_path):
    results = {"groups": {"judgment": [
        {"key": "j-1", "outcome": "ok", "decision": {"disposition": "switch", "rationale": "r"}},
        {"key": "j-2", "outcome": "ok", "decision": {"disposition": "pin", "rationale": "r"}},
        {"key": "j-3", "outcome": "ok", "decision": None},
    ]}}
    entries = [_entry("j-1"), _entry("j-2", agent="coordinator:the Staff Engineer"), _entry("j-3"), _entry("j-4")]
    decisions, undecided = decide.record_agent_decisions(tmp_path, entries, results)
    assert [(d.point_id, d.decided_by, d.decision) for d in decisions] == [
        ("j-2", "agent:coordinator:the Staff Engineer", "pin")]
    assert {u["id"]: u["reason"] for u in undecided} == {
        "j-1": "undeclared-disposition:switch",
        "j-3": "result-carries-no-decision",
        "j-4": "no-agent-result",
    }
    rows = [json.loads(ln) for ln in (tmp_path / DECISIONS_FILE).read_text().splitlines()]
    assert [r["point_id"] for r in rows] == ["j-2"]


def test_record_appends_one_object_per_line(tmp_path):
    d = Decision("p", "q", "v", "rule", "recommendation", "r", "e")
    decide.record(tmp_path / "run", [d])
    decide.record(tmp_path / "run", [d])
    lines = (tmp_path / "run" / DECISIONS_FILE).read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["point_id"] == "p"


def test_decisions_map_has_the_apply_base_shape():
    d = Decision("p", "q", "pin", "rule", "recommendation", "r", "e")
    assert decide.decisions_map([d]) == {"p": {"disposition": "pin"}}


def test_rules_cover_every_rule_id_but_no_live_disposition():
    assert set(rules.RULES) == set(RULE_IDS) - {"no-live-disposition"}
    assert all(callable(r) for r in rules.RULES.values())


def test_every_orient_brief_disposition_is_classed_exactly_once():
    values = set()
    for path in ORIENT_BRIEF.glob("*.py"):
        values |= set(re.findall(r'build_disposition\(\s*"([^"]+)"', path.read_text(encoding="utf-8")))
    assert values
    classes = [HANDBACK_DISPOSITIONS, rules.NO_ACTION_DISPOSITIONS, rules.MEMO_DISPOSITIONS,
               rules.ACTING_DISPOSITIONS]
    for value in values:
        assert sum(value in c for c in classes) == 1, value


def test_decide_holds_no_ceremony_name():
    text = DECIDE_SOURCE.read_text(encoding="utf-8")
    assert "workday-start" not in text and "workday_start" not in text


# ---- Step 0 rules (fixture repo) -------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True,
                          **no_console_creationflags()).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@t", "COORDINATOR_OVERRIDE_BRANCH": "1"}.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _runs(tmp_path):
    base = tmp_path / "runs"
    base.mkdir(exist_ok=True)
    return base


def _stale(branch="work/x/2026-10-01"):
    return SimpleNamespace(stdout=f"STALE-NEEDS-ABC branch={branch} age_days=5\n", stderr="")


def test_stale_branch_writes_one_line_and_commits_only_that_path(repo, tmp_path):
    (repo / "other.txt").write_text("dirty")
    run = _runs(tmp_path) / "20261012T000000Z-a"
    before = _git(repo, "rev-list", "--count", "HEAD").strip()
    assert rules.step0_stale_branch(None, _stale(), repo_root=repo, run_dir=run) is True
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == str(int(before) + 1)
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [rules.DEFERRED_BRANCHES_PATH]
    assert "?? other.txt" in _git(repo, "status", "--porcelain")
    (line,) = (repo / rules.DEFERRED_BRANCHES_PATH).read_text().splitlines()
    parts = [p.strip() for p in line.split("|")]
    assert parts[0] == "work/x/2026-10-01" and parts[2].startswith("re-check:") and parts[3] == run.name
    row = json.loads((run / DECISIONS_FILE).read_text())
    assert row["rule"] == "step0-stale-branch" and "without reconciling" in row["rationale"]


def test_stale_branch_second_run_replaces_the_line(repo, tmp_path):
    rules.step0_stale_branch(None, _stale(), repo_root=repo, run_dir=_runs(tmp_path) / "20261012T000000Z-a")
    rules.step0_stale_branch(None, _stale(), repo_root=repo, run_dir=_runs(tmp_path) / "20261013T000000Z-b")
    lines = (repo / rules.DEFERRED_BRANCHES_PATH).read_text().splitlines()
    assert len(lines) == 1 and lines[0].endswith("20261013T000000Z-b")


def test_stale_branch_without_a_recognised_line_is_false(repo, tmp_path):
    result = SimpleNamespace(stdout="CRASH\n", stderr="")
    assert rules.step0_stale_branch(None, result, repo_root=repo, run_dir=_runs(tmp_path) / "r") is False


def _reconcile(outcome="RECONCILE-CONFLICT", branch="work/x/2026-10-12", paths=("README.md",),
               marker="CONFLICT-PATHS"):
    return SimpleNamespace(
        stdout=f"FRESH-CUT branch={branch}\n{outcome} branch={branch}\n",
        stderr="note\n" + marker + "\n" + "\n".join(paths) + "\n",
    )


def test_first_conflict_defers_once_and_leaves_the_tree_alone(repo, tmp_path):
    runs = _runs(tmp_path)
    head, status = _git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")
    assert rules.step0_reconcile_outcome(None, _reconcile(), repo_root=repo, run_dir=runs / "20261012T000000Z-a") is True
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")) == (head, status)
    row = json.loads((runs / "20261012T000000Z-a" / DECISIONS_FILE).read_text())
    assert row["decision"] == "deferred-once" and row["rule"] == "step0-reconcile-outcome"
    assert "paths=README.md" in row["evidence"] and "outcome=RECONCILE-CONFLICT" in row["evidence"]


def test_staged_paths_come_from_the_staged_marker(repo, tmp_path):
    runs = _runs(tmp_path)
    result = _reconcile("RECONCILE-MERGE-NOT-STARTED", paths=("x.py", "y.py"), marker="STAGED-PATHS")
    assert rules.step0_reconcile_outcome(None, result, repo_root=repo, run_dir=runs / "20261012T000000Z-a") is True
    row = json.loads((runs / "20261012T000000Z-a" / DECISIONS_FILE).read_text())
    assert "paths=x.py,y.py" in row["evidence"]


def _prior(runs, name, *, decision, branch="work/x/2026-10-12", outcome="RECONCILE-CONFLICT"):
    (runs / name).mkdir()
    row = Decision("step0-reconcile-outcome", "q", decision, "rule", "step0-reconcile-outcome", "r",
                   f"branch={branch} outcome={outcome} paths=").to_row()
    (runs / name / DECISIONS_FILE).write_text(json.dumps(row) + "\n")


@pytest.mark.parametrize("prior_decision", ["deferred-once", "stop-and-file"])
def test_second_sight_stops_and_files(repo, tmp_path, prior_decision):
    runs = _runs(tmp_path)
    _prior(runs, "20261011T000000Z-a", decision=prior_decision)
    out = rules.step0_reconcile_outcome(None, _reconcile(), repo_root=repo, run_dir=runs / "20261012T000000Z-b")
    assert out == "reconcile-unresolved:RECONCILE-CONFLICT:work/x/2026-10-12"
    row = json.loads((runs / "20261012T000000Z-b" / DECISIONS_FILE).read_text())
    assert row["decision"] == "stop-and-file"


def test_a_previous_deferral_of_another_branch_does_not_count(repo, tmp_path):
    runs = _runs(tmp_path)
    _prior(runs, "20261011T000000Z-a", decision="deferred-once", branch="work/y/2026-10-11")
    assert rules.step0_reconcile_outcome(None, _reconcile(), repo_root=repo,
                                         run_dir=runs / "20261012T000000Z-b") is True


def test_only_the_run_just_before_this_one_is_read(repo, tmp_path):
    runs = _runs(tmp_path)
    _prior(runs, "20261010T000000Z-a", decision="deferred-once")
    (runs / "20261011T000000Z-b").mkdir()
    assert rules.step0_reconcile_outcome(None, _reconcile(), repo_root=repo,
                                         run_dir=runs / "20261012T000000Z-c") is True


def test_commit_refused_is_false_and_writes_nothing(repo, tmp_path):
    runs = _runs(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    result = _reconcile("RECONCILE-MERGE-COMMIT-REFUSED", paths=())
    assert rules.step0_reconcile_outcome(None, result, repo_root=repo, run_dir=runs / "20261012T000000Z-a") is False
    assert not (runs / "20261012T000000Z-a").exists()
    assert _git(repo, "rev-parse", "HEAD") == head
    unknown = SimpleNamespace(stdout="SOMETHING-ELSE branch=b\n", stderr="")
    assert rules.step0_reconcile_outcome(None, unknown, repo_root=repo, run_dir=runs / "r") is False
