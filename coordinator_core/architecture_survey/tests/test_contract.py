"""Pins the survey contract against its schema file."""
from __future__ import annotations

import copy

import pytest

from coordinator_core.architecture_survey import contract as c

RUN_ID = "20261012T093000Z-ab12"


def _digest() -> dict:
    return c.build_digest(
        run_id=RUN_ID,
        repo_root="/repo",
        state="ran",
        mode="refresh",
        system=None,
        scratch_dir="/scratch",
        finish_argv=["finish", "--run-id", RUN_ID],
        decisions=[c.Decision("mode", "refresh", "atlas-exists", "3 pages")],
        existing_atlas={"systems": [{"name": "a", "page": "docs/architecture/a.md", "last_mapped": None}]},
        source="rag",
        predicate={"ok": True},
        structure_path="/scratch/structure.json",
        refresh_set=[{"system": "a", "page": "docs/architecture/a.md", "reason": "changed", "slice_path": "/scratch/a.json"}],
        consume_gate=None,
    )


def _need(covered: bool = True) -> dict:
    return {"covered": covered, "door": "d" if covered else None, "calls": 1 if covered else 0,
            "reason": None if covered else "no door", "payload": {"x": 1} if covered else None}


def _structure() -> dict:
    return {
        "run_id": RUN_ID,
        "index": {"commit": "abc", "commits_behind": 0, "state": "current", "dropped_paths": 0, "unindexed": []},
        "needs": {n: _need() for n in c.NEEDS},
        "systems": [{"name": "a", "file_count": 1, "line_count": 10, "fingerprint": "f"}],
    }


def _results() -> dict:
    return {
        "pages_written": ["docs/architecture/a.md"],
        "decisions": [{"id": "clarity", "chosen": "pass", "rule": "agent", "evidence": "e"}],
        "clarity": [{"page": "docs/architecture/a.md", "verdict": "pass", "findings": []}],
        "dead_paths_repaired": [{"page": "p", "path": "x", "action": "removed"}],
        "manifest": {},
    }


def test_digest_validates_and_rejects_bad():
    assert c.validate_digest(_digest()) == []
    bad = _digest()
    bad["state"] = "waiting"
    assert c.validate_digest(bad)
    bad = _digest()
    bad["surprise"] = 1
    assert c.validate_digest(bad)


def test_build_digest_raises_on_invalid():
    with pytest.raises(ValueError):
        c.build_digest(**{"run_id": "nope", "repo_root": "/r", "state": "ran",
                          "mode": "refresh", "system": None, "scratch_dir": "s", "finish_argv": [],
                          "decisions": [], "existing_atlas": {"systems": []}, "source": "rag",
                          "predicate": {}, "structure_path": None, "refresh_set": [], "consume_gate": None})


def test_structure_validates_and_requires_every_need():
    assert c.validate_structure(_structure()) == []
    bad = _structure()
    del bad["needs"]["last-touch"]
    assert c.validate_structure(bad)
    bad = _structure()
    bad["index"]["state"] = "stale"
    assert c.validate_structure(bad)


def test_agent_results_validate_and_scope_only_on_cartography():
    assert c.validate_agent_results(_results(), "rag") == []
    assert c.validate_agent_results(_results(), "cartography")
    obj = copy.deepcopy(_results())
    obj["scope"] = {"systems": []}
    assert c.validate_agent_results(obj, "cartography") == []
    obj["decisions"][0]["id"] = "bogus"
    assert c.validate_agent_results(obj)


def test_agents_tally_optional():
    obj = _results()
    obj["agents"] = {"count": 2, "tokens": 100}
    assert c.validate_agent_results(obj) == []
    obj["agents"] = {"count": 2}
    assert c.validate_agent_results(obj)


def test_stage_tuples_disjoint_and_unique():
    assert not set(c.LAUNCH_STAGES) & set(c.FINISH_STAGES)
    assert len(set(c.LAUNCH_STAGES)) == len(c.LAUNCH_STAGES)
    assert len(set(c.FINISH_STAGES)) == len(c.FINISH_STAGES)


def test_owed_decisions_per_mode_and_source():
    assert c.owed_decisions("refresh", "rag") == c.DECISION_IDS
    assert "refresh-set" not in c.owed_decisions("bootstrap", "rag")
    assert "ledger-stamp" not in c.owed_decisions("targeted", "rag")
    assert "index-currency" not in c.owed_decisions("refresh", "cartography")
    assert set(c.DECISION_IDS) - set(c.owed_decisions("bootstrap", "cartography")) == {"refresh-set", "index-currency"}
    assert set(c.DECISION_IDS) - set(c.owed_decisions("targeted", "cartography")) == {"ledger-stamp", "index-currency"}
    with pytest.raises(ValueError):
        c.owed_decisions("x", "rag")


def test_exit_codes_and_names():
    assert (c.EXIT_OK, c.EXIT_USAGE, c.EXIT_IN_FLIGHT, c.EXIT_TEMPLATE_MISSING, c.EXIT_RUN_FAILED) == (0, 2, 3, 4, 5)
    assert set(c.REQUIRED_NEEDS) <= set(c.NEEDS)
