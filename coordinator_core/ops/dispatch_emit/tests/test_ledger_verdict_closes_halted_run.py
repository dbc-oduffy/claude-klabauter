"""workflow.revise `ledger_verdict` on a halted run: all rows met by a landed commit closes it landed."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.win_portability import no_console_creationflags

REL = "state/sizings/2026-10-01-verdict.yaml"
RUN = "ask-20261001T000000-cccccc"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "name": "verdict", "intent": "exercise verdict",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-verdict-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="the ask")
    hr.write_halt_marker(rl.run_dir(tmp_path, RUN), halted="criterion_not_met", sizing_rel=REL)
    _git(tmp_path, "add", "--", REL)
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "landed")
    return tmp_path


def _verdict(repo, status="met", sha=None):
    ledger = rl.read_ledger(repo, rl.run_dir(repo, RUN))
    out = {"items": [{"id": i.id, "status": status, "wired": True} for i in ledger.items]}
    if sha:
        out["landed_sha"] = sha
    return out


def _marker(repo):
    return hr._read(rl.run_dir(repo, RUN))


def test_all_met_by_landed_commit_closes_the_run_landed(repo):
    head = _git(repo, "rev-parse", "HEAD")
    reply = rl._handler({"run_id": RUN, "ledger_verdict": _verdict(repo, sha=head)}, repo)
    assert reply.get("ok") is True, reply
    assert "ledger_verdict" in reply["applied"]
    assert reply["landed"] == head
    marker = _marker(repo)
    assert marker["status"] == hr.STATUS_LANDED and marker["sha"] == head


def test_a_not_met_row_leaves_the_run_halted(repo):
    head = _git(repo, "rev-parse", "HEAD")
    reply = rl._handler({"run_id": RUN, "ledger_verdict": _verdict(repo, status="not_met", sha=head)}, repo)
    assert "ledger_verdict" not in reply["applied"]
    assert any(r["input"] == "ledger_verdict" for r in reply["refused"])
    assert _marker(repo)["status"] == hr.STATUS_HALTED


def test_verdict_without_a_landed_sha_leaves_the_run_halted(repo):
    reply = rl._handler({"run_id": RUN, "ledger_verdict": _verdict(repo)}, repo)
    assert any(r["input"] == "ledger_verdict" for r in reply["refused"])
    assert _marker(repo)["status"] == hr.STATUS_HALTED


def test_a_sha_that_is_not_a_commit_leaves_the_run_halted(repo):
    reply = rl._handler({"run_id": RUN, "ledger_verdict": _verdict(repo, sha="0" * 40)}, repo)
    assert any(r["input"] == "ledger_verdict" for r in reply["refused"])
    assert _marker(repo)["status"] == hr.STATUS_HALTED


@pytest.mark.spawns_process
def test_closing_on_a_full_sha_spawns_exactly_the_one_ancestry_check(repo, monkeypatch):
    """The verdict leg verifies through `halted_run_landed._verify`: one batched `run_git` ancestry read,
    the figure the budget manifest pins for that shared verifier."""
    head = _git(repo, "rev-parse", "HEAD")
    budget = load_manifest()["overrides"]["dispatch.record_halted_run_landed"]["spawn_count_budget"]
    with _count_spawns_attributed(monkeypatch) as spawns:
        reply = rl._handler({"run_id": RUN, "ledger_verdict": _verdict(repo, sha=head)}, repo)
    assert reply.get("landed") == head, reply
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns), [s.argv for s in spawns]
    assert len(spawns) == budget["ancestry_check"], [s.argv for s in spawns]
