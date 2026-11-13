"""coordinator_core.post_run -- deterministic, no-LLM post-run ops for the distill and lessons pipelines.

The package contract lives in `scope`; each step takes a `PostRunScope` and returns a `StepResult`.
"""

from coordinator_core.post_run.scope import (
    PIPELINES,
    PostRunReport,
    PostRunScope,
    Step,
    StepResult,
)

__all__ = ["PIPELINES", "PostRunReport", "PostRunScope", "Step", "StepResult"]
