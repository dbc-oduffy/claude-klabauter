"""Shared fixture for the bfdd34 shape: a premise row C7 with a one-file footprint records `widened`.

C0-C6 are recorded `coded` rows, C8 waits on C7's epistemic-premise gate. Every helper drives the
production entry (`plan_tasks_mutate._handler`, `ask_stage._handler`, `land_run_halt.land`) at default
arguments, so a test built on it observes what a refire observes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.ops.dispatch_emit import land_run_halt, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import _handler as stage_handler
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.plan_tasks_mutate import _handler as mutate_handler

PLAN_REL = "docs/plans/2026-11/bfdd34-shape.md"
SIZING_REL = "state/sizings/2026-11/bfdd34-shape.yaml"
SYNTHESIS_REL = "docs/research/bfdd34/synthesis.md"
RUN_ID = "ask-20261126T000000-bfdd34"
DONE_ROWS = ("C0", "C1", "C2", "C3", "C4", "C5", "C6")
PREMISE_ROW = "C7"
DEPENDENT_ROW = "C8"
PREMISE_FOOTPRINT = "docs/x/c7.md"
#: A repo file the C7 evidence says must change, owned by no row's footprint.
OUTSIDE_PATH = "coordinator_core/outside_c7_footprint.py"
ADDED_SCOPE = f"the fix lies in {OUTSIDE_PATH}, outside C7's one-file footprint"

_DONE_ROW = """\
- id: {row}
  title: finished {row}
  change_kind: doc-edit
  surface: docs/x/{low}.md
  writes:
    - docs/x/{low}.md
  disposition: coded
  disposition_ref: abc{n}234
"""

_TAIL_ROWS = f"""\
- id: {PREMISE_ROW}
  title: probe where the fix lands
  change_kind: doc-edit
  surface: {PREMISE_FOOTPRINT}
  writes:
    - {PREMISE_FOOTPRINT}
  disposition: coded
  disposition_ref: abc7234
- id: {DEPENDENT_ROW}
  title: apply the fix
  change_kind: doc-edit
  surface: docs/x/c8.md
  writes:
    - docs/x/c8.md
  depends_on:
    - chunk: {PREMISE_ROW}
      gate_kind: epistemic-premise
"""

_SYNTHESIS = f"""# Synthesis

## Premise verdict for the downstream rows

- {DEPENDENT_ROW}: confirmed, but {ADDED_SCOPE}.
"""


@dataclass(frozen=True)
class WidenedHaltRepo:
    """A scratch git repo holding the plan, its synthesis and the sizing a halted run points at."""

    repo: Path
    plan: Path


def plan_text() -> str:
    """The plan body: seven `coded` rows, the one-file premise row, its epistemic-premise dependent."""
    done = "".join(_DONE_ROW.format(row=r, low=r.lower(), n=i) for i, r in enumerate(DONE_ROWS))
    return (
        "---\ntitle: bfdd34 shape\ncreated: 2026-11-26\n---\n\n# bfdd34 shape\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{done}{_TAIL_ROWS}```\n"
    )


def build_repo(tmp_path: Path) -> WidenedHaltRepo:
    """Seed `tmp_path / "repo"` with the plan, the synthesis and the sizing, committed."""
    repo = seed_repo(tmp_path / "repo")
    for rel, text in (
        (PLAN_REL, plan_text()),
        (SYNTHESIS_REL, _SYNTHESIS),
        (SIZING_REL, f"plan: {PLAN_REL}\n"),
    ):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "bfdd34 shape"], repo)
    return WidenedHaltRepo(repo=repo, plan=repo / PLAN_REL)


def record_premise(fx: WidenedHaltRepo, verdict: str, **extra) -> dict:
    """`premise-record` for C7 through the mutate op handler, as an executor's invoke call lands it."""
    params = {
        "verb": "premise-record", "plan_path": str(fx.plan), "id": PREMISE_ROW, "verdict": verdict,
        "ref": SYNTHESIS_REL, **extra,
    }
    return asyncio.run(mutate_handler(params, repo_root=fx.repo / ".git"))


def record_widened(fx: WidenedHaltRepo) -> dict:
    """C7's `widened` verdict naming the scope the plan lacks; the reply must be exit 0."""
    result = record_premise(fx, "widened", added_scope=ADDED_SCOPE)
    assert result["exit_code"] == 0, result
    return result


def stage(fx: WidenedHaltRepo) -> dict:
    """The refire's stage call at default arguments: a refusal reply as-is, else the manifest read back."""
    write_run_scripts(fx.repo, "r1")
    reply = stage_handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=fx.repo)
    if "error" in reply:
        return reply
    return reply_sidecar.read_reply(fx.repo, reply["manifest_handle"])


def halt_run(fx: WidenedHaltRepo) -> str:
    """Halt RUN_ID over the plan through the default `land_run_halt.land` call; its landed summary."""
    run_json = fx.repo / RECORD_DIR_ROOT / RUN_ID / "run.json"
    run_json.parent.mkdir(parents=True, exist_ok=True)
    run_json.write_text(json.dumps({"sizing_rel": SIZING_REL}), encoding="utf-8", newline="\n")
    halted = {"halted": "ledger-check", "plan": PLAN_REL, "reason": "PREMISE-WIDENED"}
    return land_run_halt.land(fx.repo, fx.repo, {"run_id": RUN_ID, HALTED_RECORD_PARAM: halted})
