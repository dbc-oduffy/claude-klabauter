"""Shared plan_chain test substrate: the one-row plan, sizing, gate stubs, Spy and FakeRunner.

Holds no spawn site (a conftest spawn is untierable): the git-backed `chain` fixture lives in
`_chain_repo.py`, imported by the cadence-tiered tests that use it."""
from __future__ import annotations

import sys
from pathlib import Path

from coordinator_core.ops.plan_chain.contract import WorkflowResult

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
