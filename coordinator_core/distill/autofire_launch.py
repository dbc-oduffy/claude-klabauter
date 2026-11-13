"""
coordinator_core.distill.autofire_launch -- the headless distill launcher.

Implements `autofire_contract.Launcher`: one detached `claude -p` child running
`/distill` for a repo, started through `fire.fire_workflow(wait=False)`.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.ops.workflow_fire import fire

# Gitignored via `.coordinator-local/*`; `state/` is committed substrate and must not hold it.
FIRE_SCRIPT_REL = Path(".coordinator-local") / "distill-autofire" / "fire.mjs"

# The prompt replaces the default Workflow driver prompt, so the script body is never executed:
# it exists because fire_workflow requires a script file under the repo root, and its path is
# the stable key the fire registry records (`script_path`) for recognising an autofired run.
_SCRIPT_BODY = "// distill autofire marker: the run is driven by the /distill prompt, not this file.\n"

_PROMPT = (
    "Run /distill headless over this repository's unharvested artifacts for run_id {run_id}. "
    "No PM gate and no operator is attached: follow the command's \"No PM gate\" rule and "
    "its out-of-scope actions list, never wait for an answer, and finish with a one-line summary."
)


def fire_script_path(repo_root: Path) -> Path:
    """Absolute, resolved path of the autofire marker script for `repo_root`."""
    return (Path(repo_root) / FIRE_SCRIPT_REL).resolve()


def mint_run_id(now: Optional[datetime.datetime] = None) -> str:
    """A `distill.scope` run id: YYYY-MM-DD-HHhMM."""
    return (now or datetime.datetime.now()).strftime("%Y-%m-%d-%Hh%M")


def launch(repo_root: Path) -> dict:
    """Fire one detached headless distill run for `repo_root`; return the fire record.

    Never waits on the child. Raises whatever `fire.fire_workflow` raises; the caller
    (`autofire.on_idle_tick`) records the failure and releases its claim.

    `expected` is deliberately not passed: `fire_workflow` reads it as the script's sha256
    (a mismatch raises FiredDriftError before spawn) and never stores it on the record.
    """
    root = Path(repo_root).resolve()
    script = fire_script_path(root)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_SCRIPT_BODY, encoding="utf-8", newline="\n")
    run_id = mint_run_id()
    return fire.fire_workflow(
        str(script),
        cwd=str(root),
        prompt=_PROMPT.format(run_id=run_id),
        run_id=run_id,
        wait=False,
    )
