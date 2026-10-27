"""Executor-economy contract: constants and pure helpers shared by emit, hooks and workflow_watch.

No I/O and no coordinator_core.ops imports, so every dependant can import it cheaply.
"""

from __future__ import annotations

CONTEXT_CEILING_TOKENS = 150_000
CHECKPOINT_STATUS = "CHECKPOINT"
MAX_CONTINUATIONS = 2

EXCERPT_HEADING = "## Row excerpts"
EXCERPT_MAX_BYTES = 1200
EXCERPT_MAX_SYMBOLS_PER_FILE = 40

BATCHING_CLAUSE = (
    "Batch independent reads and searches into one turn. Read a file once, by the spans you need. "
    "Run the row's tests once per edit batch."
)

CEILING_MESSAGE = (
    "Context is {tokens} tokens, past the {ceiling}-token ceiling. Write the checkpoint file your "
    "brief names, then reply `CHECKPOINT: <path>`."
)

RUN_COST_FILE = "run-cost.json"

# Execute share is reported in phase_split but never ratcheted: cutting Plan/Review spend raises it.
RATCHET_METRICS = (
    "executor_peak_context",
    "executor_tool_calls",
    "executor_max_same_file_reads",
)

# Measured on run wf_e0561313-728.
MEASURED_BASELINE = {
    "executor_peak_context": 366000,
    "executor_tool_calls": 254,
    "executor_max_same_file_reads": 32,
}

PENDING_REVISIONS_OP = "workflow.pending_revisions"
CLAIM_ROWS_OP = "plan.claim_register_rows"
ABSORB_ROUNDS = 1


def checkpoint_path(sidecar_dir: str, row_id: str, n: int) -> str:
    """Posix path of the n-th checkpoint file for a row, whatever separator the dir arrives with."""
    base = str(sidecar_dir).replace("\\", "/").rstrip("/")
    return f"{base}/{row_id}.checkpoint-{n}.md"


def ceiling_message(tokens: int) -> str:
    """CEILING_MESSAGE rendered for an observed context size."""
    return CEILING_MESSAGE.format(tokens=tokens, ceiling=CONTEXT_CEILING_TOKENS)
