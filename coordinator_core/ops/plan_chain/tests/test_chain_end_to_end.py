"""plan_chain.run end to end on a tmp_path git repo: completed chain, pulled plan, blocked chunk."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from coordinator_core.ops.plan_chain import digest as digest_mod, driver
from coordinator_core.ops.plan_chain.contract import ChainManifest
from coordinator_core.ops.plan_chain.tests.conftest import CHUNK_FILE, Spy, _git, head

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _written_digest(root: Path) -> dict:
    files = list((root / "trail").glob("*.final-digest.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    digest_mod.validate_final_digest(written)
    return written


def _spy_terminal_commit(monkeypatch, root: Path) -> Spy:
    real = driver._engine_terminal_commit(str(root))
    spy = Spy(real)
    monkeypatch.setattr(driver, "_engine_terminal_commit", lambda repo_root: spy)
    return spy


def _spy_emit(monkeypatch) -> Spy:
    spy = Spy(driver.emit_leg.run)
    monkeypatch.setattr(driver.emit_leg, "run", spy)
    return spy


def test_completed_chain_commits_from_the_driver_loop_with_zero_em_turns(chain, monkeypatch):
    commit_spy = _spy_terminal_commit(monkeypatch, chain.root)
    before = head(chain.root)
    runner = chain.make_runner()

    digest = driver.run(chain.manifest, runner=runner)

    assert digest["chain"]["halted_at"] is None, digest["chain"]
    assert digest == _written_digest(chain.root)
    assert len(commit_spy.calls) == 1
    assert commit_spy.stacks[0][:3] == [
        "coordinator_core.ops.plan_chain.driver._drive",
        "coordinator_core.ops.plan_chain.driver.run",
        "coordinator_core.ops.plan_chain.tests.test_chain_end_to_end.test_completed_chain_commits_from_the_driver_loop_with_zero_em_turns",
    ]
    sha = digest["chain"]["commit"]["sha"]
    assert head(chain.root) != before
    # One product commit on top of the seed; the plan-only "mark rows coded" commit may follow it.
    assert _git(chain.root, "rev-parse", f"{sha}^") == before
    assert CHUNK_FILE in _git(chain.root, "ls-tree", "-r", "--name-only", sha).split()
    assert digest["chain"]["commit"]["receipt_path"]

    assert len(runner.scripts) == 2
    assert runner.scripts[0].endswith(".plan.mjs") and runner.scripts[1].endswith(".execute.mjs")
    params = inspect.signature(driver.run).parameters
    assert set(params) == {"manifest", "runner", "invoke_terminal_commit"}
    assert not any("em" in f.lower().split("_") or "callback" in f for f in ChainManifest.__dataclass_fields__)


def test_pulled_plan_halts_at_ready_gate_without_emitting(chain, monkeypatch):
    emit_spy = _spy_emit(monkeypatch)
    commit_spy = _spy_terminal_commit(monkeypatch, chain.root)
    before = head(chain.root)
    pulled = {"kind": "plan", "outcome": "pulled", "decision_required": "scope question"}

    digest = driver.run(chain.manifest, runner=chain.make_runner(plan_digest=pulled))

    assert digest["chain"]["halted_at"] == "ready-gate"
    assert digest["kind"] == "plan"
    assert emit_spy.calls == [] and commit_spy.calls == []
    assert head(chain.root) == before
    assert digest == _written_digest(chain.root)


def test_blocked_chunk_halts_at_execute_without_a_commit(chain, monkeypatch):
    commit_spy = _spy_terminal_commit(monkeypatch, chain.root)
    before = head(chain.root)
    blocked = {"outcome": "complete", "deviations": [{"chunk": "C1", "kind": "blocked", "anchor": "no tool"}]}

    digest = driver.run(chain.manifest, runner=chain.make_runner(execute_digest=blocked))

    assert digest["chain"]["halted_at"] == "execute"
    assert digest["chain"]["commit"] is None
    assert commit_spy.calls == []
    assert head(chain.root) == before
    assert digest == _written_digest(chain.root)
