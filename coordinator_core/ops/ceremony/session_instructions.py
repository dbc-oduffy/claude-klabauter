"""
coordinator_core.ops.ceremony.session_instructions — EM-facing instruction-set render op.

Purpose: Render branch_resolution's already-computed node classification into a pruned,
ordered, EM-facing instruction set — collapsing the branch-reasoning the EM currently
hand-walks in the /workstream-complete SKILL.  Read-mostly: no mutation, no tail, no
commit, no durable write of its own (renders off an already-resolved context; the
retired ``ceremony.wsc_resolve`` op used to emit a receipt off this same engine —
this op never did and still doesn't).

Genericity (op-spec §5, hard requirement): this op keys ONLY on node TYPE (D/J/F/B,
from node_handlers._STEP_NODE_TYPES) and generic top-level PipelineContext fields
(``ceremony``, ``scope_mode``, ``disposition``, ``applicable_node_ids``).  It contains
NO wsc step-ID literals, NO wsc-phase-label string-matches, and NO ceremony-specific
conditionals of its own — membership comes ONLY from ``ctx.applicable_node_ids``
(the declared-membership signal ``branch_resolution._resolve_branches`` populates,
op-spec §3 Option B).  This is what makes the op reusable by a second ceremony/
consumer (named consumer #2 is `/pickup`, future wiring) without a rebuild.

Three-tier input ladder (op-spec §6): every J-node entry carries a ``dropdown``
sidecar — ``{question, options, harvested_default}`` — pre-filled from Tier-1/Tier-2
harvest primitives already computed by branch_resolution (session shape, commit log,
nature classification) when a harvest rule applies to that node's evidence; ``null``
when silent/ambiguous (never a guessed value).  DR-502 guard: ``harvested_default``
PRE-FILLS but never PRE-SELECTS — the dropdown's presence does not resolve the node;
only an EM-authored ``answer`` does.

Spec backlink:
  docs/plans/2026-07-08-session-specific-instruction-set-emitter.op-spec.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.branch_resolution import (
    _read_session_shape,
    _resolve_branches,
    _session_commit_log,
)
from coordinator_core.ops.fleet._common import main_worktree_root

# ---------------------------------------------------------------------------
# Tier-2 harvest — conventional-commit bug-vs-not marker (op-spec §6 Tier 2)
# ---------------------------------------------------------------------------
# Pure string check over commit subjects already fetched by branch_resolution's own
# git-log primitive (_session_commit_log) — no new git call, no ceremony literal.

_BUG_MARKERS: tuple[str, ...] = ("fix:", "fix(", "bug")


def _harvest_bug_signal(commit_msgs: list[str]) -> Optional[bool]:
    """Return True/False when commit subjects legibly signal bug-vs-not, else None.

    Tier-2 harvest rule (op-spec §6): conventional-commit `fix:`/`fix(...)`/`bug`
    markers in the session's own commit subjects.  Fallback is None (never a guess)
    when the commit set is empty — the caller drops to Tier 3 (dropdown, no default).
    """
    if not commit_msgs:
        return None
    return any(
        marker in msg.lower() for msg in commit_msgs for marker in _BUG_MARKERS
    )


# ---------------------------------------------------------------------------
# Render — keyed on node type ONLY (op-spec §4/§5)
# ---------------------------------------------------------------------------


def _render_node(node: dict[str, Any], harvested_bug_signal: Optional[bool]) -> dict[str, Any]:
    """Render one pruned node into its EM-facing instruction-set entry.

    Keys on ``node["type"]`` ONLY — D/J/F/B (receipt_schema._NODE_TYPE_REQUIRED is
    the render vocabulary).  No per-node conditional, no step-ID literal.
    """
    node_type = node.get("type")
    entry: dict[str, Any] = {"id": node.get("id"), "type": node_type}

    if node_type == "D":
        entry["instruction"] = "auto — no EM action"
        entry["resolving_op"] = node.get("resolving_op", "")
    elif node_type == "J":
        entry["question"] = node.get("question", "")
        entry["dropdown"] = {
            "question": node.get("question", ""),
            "options": ["yes", "no"],
            "harvested_default": harvested_bug_signal,
        }
    elif node_type == "F":
        entry["prose_slot"] = node.get("slot", "")
    elif node_type == "B":
        entry["review_bracket"] = node.get("pre_resolved_evidence", {})
    else:
        entry["instruction"] = "unrecognized node type — render vocabulary gap"

    return entry


# ---------------------------------------------------------------------------
# Registered op handler
# ---------------------------------------------------------------------------


@register_op("ceremony.session_instructions")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'ceremony.session_instructions' handler — instruction-set render op.

    Reads disk (via the same `_resolve_branches` call the retired ``ceremony.wsc_resolve``
    op used to make) and renders the pruned, ordered, EM-facing instruction set for
    this session's disposition.

    This is a READ-MOSTLY op — it does not mutate work-state artifacts, does not
    commit, and does not run a tail.  Its only output is the instruction-set payload.

    Parameters (params dict):
        sid         (str, required) — the WSC session ID to render instructions for.
        scope_mode  (str, optional) — typed override; mirrors the retired
                                      ``ceremony.wsc_resolve`` op's scope_mode
                                      param handling.  Disk-read is primary; this
                                      is the secondary/testing path.

    repo_root:
        Git common dir (from _OP_KEY_SCOPE = "common_dir").  Worktree root is
        derived via main_worktree_root(repo_root).

    Returns:
        {
          "exit_code":           0 | 1,
          "disposition":         "single-session" | "chain-terminal",
          "scope_mode":          str,
          "applicable_node_ids": list[str],
          "instructions":        list[dict],  # ordered, pruned, type-keyed entries
        }

    On error (exit_code=1):
        {"exit_code": 1, "error": str}
    """
    sid = params.get("sid", "")
    if not sid:
        return {"exit_code": 1, "error": "ceremony.session_instructions: 'sid' param is required"}

    if repo_root is None:
        return {
            "exit_code": 1,
            "error": (
                "ceremony.session_instructions: repo_root arg is None — "
                "common_dir not supplied by engine (check _OP_KEY_SCOPE = 'common_dir')"
            ),
        }

    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    session_shape, shape_source = _read_session_shape(common_dir, sid)

    scope_override = str(params.get("scope_mode") or "")
    if scope_override and session_shape:
        session_shape.setdefault("plan", {})
        session_shape["plan"]["scope_mode"] = scope_override
    elif scope_override:
        session_shape = {"plan": {"scope_mode": scope_override}}
        shape_source = "param_override"

    # --- Reuse the SAME _resolve_branches call wsc_resolve makes — no reclassification.
    ctx = _resolve_branches(
        worktree_root=worktree_root,
        common_dir=common_dir,
        sid=sid,
        session_shape=session_shape,
        shape_source=shape_source,
    )

    # --- Tier-2 harvest inputs (op-spec §6) — reuse wsc_resolve's own primitives.
    commit_msgs = _session_commit_log(worktree_root, sid)
    harvested_bug_signal = _harvest_bug_signal(commit_msgs)

    # --- Prune: emit exactly the nodes whose id is in the declared membership list.
    applicable_ids = set(ctx.applicable_node_ids)
    instructions = [
        _render_node(node, harvested_bug_signal)
        for node in ctx.nodes
        if node.get("id") in applicable_ids
    ]

    return {
        "exit_code": 0,
        "disposition": ctx.disposition,
        "scope_mode": ctx.scope_mode,
        "applicable_node_ids": ctx.applicable_node_ids,
        "instructions": instructions,
    }
