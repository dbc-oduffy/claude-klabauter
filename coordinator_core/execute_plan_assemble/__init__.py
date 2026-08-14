"""
coordinator_core.execute_plan_assemble — mutating assemblers for the
`/execute-plan` skill's write surfaces.

First (and, at this chunk, only) member: `close_out_and_stamp`, the op that
collapses `/execute-plan` Phase 4's ordinal-narrated close-out sequence
(stage every changed path, stamp the plan `status:` to `implemented` when
every chunk shipped, land one scoped commit) into a single named op the
skill invokes instead of hand-sequencing git.

Spec backlink: DoE-claude coordinator/skills/execute-plan/SKILL.md § Phase 4
"""

from __future__ import annotations
