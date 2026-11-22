"""A resumed run starts with its predecessor's `all_of_pm_ask`; a run whose sizing register holds a
PM verbatim starts true; a fresh `pm`-sourced ask mints true."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

PRIOR = "ask-20261121T120000-c40124"
REFIRE = "ask-20261121T130000-33ff04"
REL_PM = "state/sizings/2026-11/2026-11-21-resume-pm.yaml"
REL_PLAIN = "state/sizings/2026-11/2026-11-21-resume-plain.yaml"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _sizing(repo: Path, rel: str, **extra) -> None:
    doc = {
        "schema": "sizing-object", "intent": "carry the flag", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"}, **extra,
    }
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _flag(repo: Path, run_id: str) -> bool:
    return bool(rl.read_run(repo / RECORD_DIR_ROOT / run_id)["all_of_pm_ask"])


def _resume(repo: Path, **ctx) -> None:
    op._start_requirements({"root": repo, "run_id": REFIRE, "sizing_rel": None, "ask_source": "relayed", **ctx}, all_of_pm_ask=False)


def test_a_refire_starts_with_the_predecessors_true(repo: Path) -> None:
    rl.start(repo, PRIOR)
    rl.revise(repo, PRIOR, all_of_pm_ask=True)
    _resume(repo, resumes_ask=PRIOR)
    assert _flag(repo, REFIRE) is True
    assert rl.read_ledger(repo, repo / RECORD_DIR_ROOT / REFIRE).all_of_pm_ask is True


def test_a_rebound_resume_carries_through_resumed_from(repo: Path) -> None:
    rl.start(repo, PRIOR)
    rl.revise(repo, PRIOR, all_of_pm_ask=True)
    _resume(repo, resumed_from=PRIOR)
    assert _flag(repo, REFIRE) is True


def test_a_predecessor_false_stays_false_on_the_refire(repo: Path) -> None:
    rl.start(repo, PRIOR)
    rl.revise(repo, PRIOR, all_of_pm_ask=False)
    _resume(repo, resumes_ask=PRIOR)
    assert _flag(repo, REFIRE) is False


def test_an_explicit_false_on_the_refire_is_honoured_without_pm_words(repo: Path) -> None:
    rl.start(repo, PRIOR, all_of_pm_ask=True)
    rl.start(repo, REFIRE, resumed_from=PRIOR)
    rl.revise(repo, REFIRE, all_of_pm_ask=False)
    assert _flag(repo, REFIRE) is False
    assert rl.read_ledger(repo, repo / RECORD_DIR_ROOT / REFIRE).all_of_pm_ask is False


def test_a_sizing_with_a_pm_verbatim_row_starts_true_with_no_revise(repo: Path) -> None:
    _sizing(repo, REL_PM, pm_verbatims=[{"turn": 119, "text": "ship the whole of it"}])
    rl.start(repo, PRIOR)
    rl.bind(repo, PRIOR, REL_PM)
    assert _flag(repo, PRIOR) is True


def test_a_refire_bound_to_a_pm_sizing_starts_true_though_the_predecessor_read_false(repo: Path) -> None:
    _sizing(repo, REL_PM, pm_verbatims=[{"turn": 119, "text": "ship the whole of it"}])
    rl.start(repo, PRIOR)
    rl.revise(repo, PRIOR, all_of_pm_ask=False)
    _resume(repo, resumes_ask=PRIOR, sizing_rel=REL_PM)
    assert _flag(repo, REFIRE) is True


def test_a_sizing_with_no_pm_verbatim_stays_false(repo: Path) -> None:
    _sizing(repo, REL_PLAIN)
    rl.start(repo, PRIOR)
    rl.bind(repo, PRIOR, REL_PLAIN)
    assert _flag(repo, PRIOR) is False


def test_a_fresh_pm_sourced_ask_with_a_pm_turn_mints_true_and_a_relayed_one_false(repo: Path) -> None:
    op._start_requirements({"root": repo, "run_id": PRIOR, "sizing_rel": None, "ask_source": "pm"}, all_of_pm_ask=False)
    op._start_requirements({"root": repo, "run_id": REFIRE, "sizing_rel": None, "ask_source": "relayed"}, all_of_pm_ask=False)
    assert _flag(repo, PRIOR) is True
    assert _flag(repo, REFIRE) is False
