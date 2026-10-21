"""The git-backed `chain` fixture: a tmp_path repo seeded with the conftest's plan, sizing and
baton, Phase-1 gates stubbed. Spawns git, so only cadence-tiered tests import it."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.plan_chain import driver, phase1_checks, phase1_gates
from coordinator_core.ops.plan_chain.contract import ChainManifest
from coordinator_core.ops.plan_chain.tests.conftest import (
    _GATE_STUBS,
    _PLAN,
    _SCRIPT_SOURCE,
    _SIZING,
    BATON_REL,
    PLAN_REL,
    SIZING_REL,
    FakeRunner,
    _write,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")



@pytest.fixture
def chain(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, PLAN_REL, _PLAN)
    _write(root, SIZING_REL, _SIZING)
    _write(root, BATON_REL, "---\ntitle: demo baton\n---\n")
    _write(root, "workflows/plan-blitz.mjs", _SCRIPT_SOURCE)
    _write(root, ".gitignore", "trail/\n.coordinator-local/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    # dispatch.emit opens a relative plan_path against the process cwd, not repo_root: the
    # driver runs with cwd at the repo root in production.
    monkeypatch.chdir(root)

    manifest = ChainManifest(
        sizing_object=SIZING_REL,
        baton=BATON_REL,
        deliverable_id="dlv-demo-abcdef",
        interaction_mode="pm",
        repo_root=str(root),
        trail_dir="trail",
        wave_args={},
        script_source=str(root / "workflows" / "plan-blitz.mjs"),
    )
    for name, stub in _GATE_STUBS.items():
        monkeypatch.setattr(phase1_gates, name, stub)
    monkeypatch.setattr(phase1_gates.exec_auth_stamp, "main", lambda argv: 0)
    monkeypatch.setattr(phase1_checks, "_write_baseline", lambda repo_root, plan: None)
    monkeypatch.setattr(phase1_checks, "_check_plan", lambda plan: {"verdict": "VALID"})
    monkeypatch.setattr(phase1_checks, "_falsifier_defect", lambda plan, repo_root: None)
    monkeypatch.setattr(phase1_checks, "_plan_targets", lambda plan, repo_root: [])

    ready = {"kind": "plan", "outcome": "ready", "next_action": {"op": "plan_chain.run", "params": {"plan_path": PLAN_REL}}}
    complete = {
        "outcome": "complete",
        "review": {"status": "integrated", "slices": 1, "fixes_applied": 0},
        "criterion": {"status": "met", "observation": "demo exists", "sidecar": None},
        "next_action": {
            "op": "dispatch.terminal_commit",
            "params": {
                "incomplete_chunks": [],
                "inline_review": {"integration_stem": "demo-review", "slices": 1, "fixes": 0},
            },
        },
    }

    def make(*, plan_digest=None, execute_digest=None):
        runner = FakeRunner(root, execute_digest if execute_digest is not None else complete, plan_digest or ready)
        return runner

    return SimpleNamespace(root=root, manifest=manifest, make_runner=make, driver=driver, ready=ready, complete=complete)
