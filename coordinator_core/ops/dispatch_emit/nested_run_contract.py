"""The launcher <-> run-script seam for a multi-ask fan-out.

A launcher script starts each emitted run as a nested workflow child. A nested child receives no
args (typeof args is 'undefined', run wf_35910dfa-28a) and nesting is one level deep, so a child
never calls ``workflow()`` and cannot be handed a manifest. The seam is the child's
``stage_pending`` return plus a staged twin: a copy of the run script with ``STAGED_INLINE_SLOT``
replaced by the sha-verified manifest text, at ``staged_twin_rel(run_id, sha256)``. Pure
constants and one pure string function: no I/O, no imports.
"""

# dispatch.emit param the launcher's emit leg sets: renders the script for a launcher to start (no workflow() reader).
LAUNCHER_CHILD_EMIT_PARAM = "launcher_child"

# A launcher child's return when it reaches a stage fold: {handle, ...}.
STAGE_PENDING_KEY = "stage_pending"

# A launcher child's return when its sizing resolved M+: the dispatch.emit params of the {sizing} script the launcher emits and starts once.
CHAIN_HANDOFF_KEY = "chain"

# The one line the twin writer replaces with the verified manifest text.
STAGED_INLINE_SLOT = "const _STAGED_INLINE = null;"

# The inline payload's resume block tells a twin it is past stage and carries what the launcher's own legs settled.
STAGED_RESUME_KEY = "resume"
STAGED_RESUME_FIELDS = ("arm", "sizing_rel", "plan_rel", "tshirt")


def staged_twin_rel(run_id: str, sha256: str) -> str:
    """Repo-relative path of the staged twin; callers validate run_id and sha256 (64 lowercase hex)."""
    return f"scratch/warp/{run_id}.staged-{sha256}.workflow.mjs"
