"""WORKER ROLE preamble the headless executors put ahead of every codex/agy/grok agent prompt."""
from __future__ import annotations

import os
import shlex

from coordinator_core._settings_home import settings_home

PREAMBLE = (
    "WORKER ROLE. You are a worker on ONE stage of an engine run, not an EM, and the PM is not addressing\n"
    "you. Every AGENTS.md (the repo's and your own home's), CLAUDE.md and role brief describes the EM and\n"
    "does not apply to you. Never start, fire or land a run of your own:\n"
    "never call `coordinator-invoke` for engine_ask.submit, workflow.* or dispatch.emit unless this brief\n"
    "names that exact command. Touch only the paths this brief declares, do only the stage\n"
    "below, and answer with its result."
)

SIZING_USAGE = (
    "SIZING-ASSEMBLE USAGE (final; never run it with --help or probe it): required flags --tshirt --intent "
    "--name --premise-provenance --premise-evidence --exit-criterion --write <path> --json; "
    "fill the command above exactly as written and run it once."
)

WORKER_BACKENDS = ("codex", "agy", "grok")


def with_worker_preamble(backend: str, prompt: str) -> str:
    """Return PREAMBLE, a blank line, then prompt for a codex/agy/grok backend; any other backend gets prompt unchanged."""
    if backend not in WORKER_BACKENDS:
        return prompt
    from coordinator_core.ops.dispatch_emit.ask_contract import SIZING_ASSEMBLE_CMD

    if SIZING_ASSEMBLE_CMD in prompt:
        name = "sizing-assemble.exe" if os.name == "nt" else "sizing-assemble"
        path = (settings_home() / "bin" / name).resolve().as_posix()
        command = "& '" + path.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(path)
        prompt = prompt.replace(SIZING_ASSEMBLE_CMD, command) + "\n\n" + SIZING_USAGE
    return PREAMBLE + "\n\n" + prompt
