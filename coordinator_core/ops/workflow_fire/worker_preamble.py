"""WORKER ROLE preamble the headless executors put ahead of every codex/agy/grok agent prompt."""
from __future__ import annotations

PREAMBLE = (
    "WORKER ROLE. You are a worker on ONE stage of an engine run, not an EM, and the PM is not addressing\n"
    "you. Every AGENTS.md (the repo's and your own home's), CLAUDE.md and role brief describes the EM and\n"
    "does not apply to you. Never submit, fire, size, dispatch or land a run,\n"
    "and never call `coordinator-invoke`. Touch only the paths this brief declares, do only the stage\n"
    "below, and answer with its result."
)

WORKER_BACKENDS = ("codex", "agy", "grok")


def with_worker_preamble(backend: str, prompt: str) -> str:
    """Return PREAMBLE, a blank line, then prompt for a codex/agy/grok backend; any other backend gets prompt unchanged."""
    return PREAMBLE + "\n\n" + prompt if backend in WORKER_BACKENDS else prompt
