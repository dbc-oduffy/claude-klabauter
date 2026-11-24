"""agent_pipe.agy_model -- discrete-job argv for Antigravity on a chosen Gemini model.

Wraps the C3 `AgyAdapter.spawn_argv` and adds agy's `--model` flag. The model id is
`COORDINATOR_AGY_MODEL` when set, else `model_for_role("agy", "executor")`, which raises
ModelSlotBlocked if agy's sonnet slot is ever unverified; the env var takes an id `agy models` prints.

`AgyModelAdapter` is the production entry: set COORDINATOR_AGENT_PIPE_ADAPTER to
`coordinator_core.agent_pipe.agy_model:AgyModelAdapter` and the `agent_pipe.start` worker spawns
`job_argv` (model flag and reply instruction) instead of the plain adapter argv.
"""
from __future__ import annotations

import os
from typing import Optional

from coordinator_core.agent_pipe.agy import AgyAdapter
from coordinator_core.agent_pipe.model_tiers import model_for_role

MODEL_ENV = "COORDINATOR_AGY_MODEL"
DEFAULT_MODEL = "gemini-4-carbon"

REPLY_INSTRUCTION = (
    "\n\nReply protocol: every message you write in your response is relayed to the session "
    "that asked. Send a short progress or question message whenever you need input, and a final "
    "message with the result when the job is done."
)


def resolve_model(model: Optional[str] = None) -> str:
    """Explicit `model`, else the env override, else the agy executor slot; raises ModelSlotBlocked if that slot is unverified."""
    return model or os.environ.get(MODEL_ENV) or model_for_role("agy", "executor")


def job_argv(prompt: str, model: Optional[str] = None) -> list[str]:
    """Spawn argv for a discrete job: the adapter argv, `--model <id>`, the prompt plus the reply instruction."""
    argv = AgyAdapter().spawn_argv(prompt + REPLY_INSTRUCTION)
    return [*argv, "--model", resolve_model(model)]


class AgyModelAdapter(AgyAdapter):
    """AgyAdapter whose fresh spawns are `job_argv` discrete jobs; resumes pass through unchanged.

    Reached by the relay worker via COORDINATOR_AGENT_PIPE_ADAPTER=coordinator_core.agent_pipe.agy_model:AgyModelAdapter.
    """

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        if conversation:
            return self.resume_argv(conversation, prompt)
        return job_argv(prompt)
