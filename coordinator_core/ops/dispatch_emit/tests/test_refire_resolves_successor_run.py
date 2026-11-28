"""A refire of a sizing resolves to the newest run whose `resumed_from` chain reaches the halted run, or refuses naming both."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

pytestmark = pytest.mark.complete

STEM = "2026-11-21-successor-sizing"
REL = f"state/sizings/2026-11/{STEM}.yaml"
A = f"warp-{STEM}-20261121T154227-c40124"
B = f"warp-{STEM}-20261121T174917-33ff04"
C = f"warp-{STEM}-20261121T190000-aaaaaa"
UNRELATED = f"warp-{STEM}-20261121T200000-bbbbbb"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    sizing = tmp_path / REL
    sizing.parent.mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-successor-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-21", "mode": "pm"}},
    }
    sizing.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


def _run(root: Path, run_id: str, *, resumed_from: str = None, status: str = "halted") -> None:
    rl.start(root, run_id, resumed_from=resumed_from)
    rl.bind(root, run_id, REL, ask_text="the ask")
    rd = record_dir(root, run_id)
    hr.write_halt_marker(rd, halted="criterion", sizing_rel=REL)
    if status == "resumed":
        hr.mark_resumed(rd)
    elif status == "landed":
        hr.mark_landed(rd, "a" * 40)
    elif status == "closed":
        hr.mark_closed(rd, "never lands", "2026-11-21T19:00:00Z")
    elif status == "superseded":
        hr.mark_superseded_by_refire(rd, prior_engine=None, engine=None, at="2026-11-21T19:00:00Z")


def test_refire_resolves_to_the_halted_successor_not_its_predecessor(repo) -> None:
    _run(repo, A)
    _run(repo, B, resumed_from=A)
    assert hr.halted_run_to_resume(repo, REL) == B


def test_named_predecessor_resolves_to_its_successor(repo) -> None:
    _run(repo, A)
    _run(repo, B, resumed_from=A)
    assert hr.halted_run_to_resume(repo, REL, run_id=A) == B


def test_newest_successor_in_the_chain_wins(repo) -> None:
    _run(repo, A)
    _run(repo, B, resumed_from=A, status="resumed")
    _run(repo, C, resumed_from=B)
    assert hr.halted_run_to_resume(repo, REL, run_id=A) == C


@pytest.mark.parametrize("status", ["resumed", "landed", "closed", "superseded"])
def test_a_successor_that_is_not_halted_refuses_naming_both(repo, status) -> None:
    _run(repo, A)
    _run(repo, B, resumed_from=A, status=status)
    with pytest.raises(hr.AmbiguousHaltedRuns) as exc:
        hr.halted_run_to_resume(repo, REL, run_id=A)
    assert exc.value.candidates == (A, B)
    assert A in str(exc.value) and B in str(exc.value)


def test_a_newer_unrelated_run_does_not_displace_the_halted_run(repo) -> None:
    _run(repo, A)
    rl.start(repo, UNRELATED)
    rl.bind(repo, UNRELATED, REL, ask_text="the ask")
    assert hr._successor_or_self(repo, REL, A) == A


ASK_B = "ask-20261121T174917-33ff04"


def test_a_halted_ask_run_successor_is_resolved(repo) -> None:
    _run(repo, A)
    _run(repo, ASK_B, resumed_from=A)
    assert hr.halted_run_to_resume(repo, REL, run_id=A) == ASK_B


def test_a_resumed_ask_run_successor_refuses_naming_both(repo) -> None:
    _run(repo, A)
    _run(repo, ASK_B, resumed_from=A, status="resumed")
    with pytest.raises(hr.SupersededHaltedRun) as exc:
        hr.halted_run_to_resume(repo, REL, run_id=A)
    assert exc.value.candidates == (A, ASK_B)
