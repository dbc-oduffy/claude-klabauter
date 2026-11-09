"""coordinator_core.consolidate_assemble.ops — registers `consolidate_assemble.brief`
(read-only) as a warm-servable op.

Importing this module is the registration side-effect, mirroring
`coordinator_core.baton_assemble.ops`. The handler is a thin
`(params, repo_root) -> dict` adapter over `coordinator_core.consolidate_assemble.brief`;
no decision logic lives here.

Negative-spec:
    - Do NOT re-derive `brief()`'s decision logic here.
    - Do NOT resolve `repo_root` from `Path.cwd()`/`Path(__file__)`: the per-request
      resolved worktree root that arrives as the handler's `repo_root` is the only source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.consolidate_assemble import brief as _brief
from coordinator_core.ipc import register_op


@register_op("consolidate_assemble.brief")
async def _consolidate_assemble_brief(params: dict[str, Any], repo_root: Optional[Path]) -> dict[str, Any]:
    """Return the `/consolidate-git` decision object (COMPUTE_ONLY; spawns read-only git, writes nothing).

    params:
        superseded: optional dict mapping branch name to the superseding sha, the
                    CLI's `--superseded <branch>=<sha>` map.
    """
    superseded = params.get("superseded")
    kwargs: dict[str, Any] = {"repo_root": repo_root}
    if superseded:
        kwargs["superseded"] = superseded
    return _brief(**kwargs)
