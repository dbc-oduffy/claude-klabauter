"""Shared fixture for the plan_chain end-to-end tests: a tmp_path git repo, a one-row plan, a fake runner."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.plan_chain import driver, phase1_checks, phase1_gates
from coordinator_core.ops.plan_chain.contract import ChainManifest, WorkflowResult
from coordinator_core.win_portability import no_console_creationflags

PLAN_REL = "docs/plans/2026-10-08-demo.md"
SIZING_REL = "state/sizings/2026-10-08-demo.yaml"
BATON_REL = "state/handoffs/2026-10-08-demo.md"
CHUNK_FILE = "src/demo.py"

_PLAN = f"""---
title: demo
status: executing
plan_id: pln-demo-abcdef
deliverable_id: dlv-demo-abcdef
sizing_object: {SIZING_REL}
---

# demo

## Tasks

```yaml plan-tasks
- id: C1
  title: write demo
  change_kind: code-edit
  surface: {CHUNK_FILE}
  writes:
    - {CHUNK_FILE}
  disposition: open
  deferred: false
  body: |
    Write the demo file.
```
"""

_SIZING = """exit_criterion:
  accepted: true
interaction_mode: pm
estimate:
  tshirt: M
route: plan
"""

_SCRIPT_SOURCE = "export const meta = {\n  name: 'plan-blitz',\n  phases: [],\n}\nreturn args\n"

# Plugin-root-dependent callees (check_plan, plan-completeness, authorize-invocation, claim_plan)
# need a live coordinator plugin install; the fixture repo has none, so they are stubbed at the
# import sites the Phase-1 modules call them through.
_GATE_STUBS = {
    "stamp_check": lambda path, repo_root=None: (0, {"verdict": "match"}),
    "assemble_plan_gate": lambda root, subject=None: {"batons": []},
    "claim_plan": lambda slug, cwd=None, *, for_execution=False, plan_path=None: True,
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class Spy:
    """Calls through to ``target`` and records each call's args and the full caller stack."""

    def __init__(self, target):
        self.target = target
        self.calls: list[tuple] = []
        self.stacks: list[list[str]] = []

    def __call__(self, *args, **kwargs):
        frame, names = sys._getframe(1), []
        while frame is not None:
            names.append(f"{frame.f_globals.get('__name__')}.{frame.f_code.co_name}")
            frame = frame.f_back
        self.stacks.append(names)
        self.calls.append((args, kwargs))
        return self.target(*args, **kwargs)


class FakeRunner:
    """Plays each Workflow's disk effects; ``execute`` may be a dict overriding the execute digest."""

    def __init__(self, root: Path, execute_digest: dict | None, plan_digest: dict):
        self.root = root
        self.execute_digest = execute_digest
        self.plan_digest = plan_digest
        self.scripts: list[str] = []

    def __call__(self, script_path: Path, *, session_id: str) -> WorkflowResult:
        name = Path(script_path).name
        self.scripts.append(name)
        if name.endswith(".plan.mjs"):
            return WorkflowResult(self.plan_digest, "", session_id)
        _write(self.root, CHUNK_FILE, "VALUE = 1\n")
        return WorkflowResult(self.execute_digest, "", session_id)


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
