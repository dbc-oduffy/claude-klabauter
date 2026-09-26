
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from coordinator_core.ops.ceremony.receipt_schema import (  # noqa: F401 — shapes re-exported
    VALID_NODE_TYPES,
    VALID_SCOPING_METHODS,
    make_b_node,
    make_d_node,
    make_f_node,
    make_j_node,
    make_x_node,
)
from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, VALID, canonicalize


BRANCH_ID_WSC_DISPOSITION = "WSC_DISPOSITION"
BRANCH_ID_GOVERNING_PLAN = "governing_plan_exists"
BRANCH_ID_CHAIN_SLUG = "chain_slug_4way"
BRANCH_ID_IDEMPOTENCY_GUARD = "idempotency_guard"
BRANCH_ID_NATURE_CLASSIFICATION = "nature_classification"
BRANCH_ID_LESSON_QUALIFIES = "lesson_qualifies"
BRANCH_ID_LESSON_UNIVERSAL = "lesson_universal"
BRANCH_ID_REVIEW_WAVE_SCALE = "review_wave_scale"
BRANCH_ID_OPEN_MEMOS = "open_memos_exist"
BRANCH_ID_MEMOS_RESOLVED = "which_memos_resolved"
BRANCH_ID_SESSION_AUTHORED_FILES = "session_authored_transient_files"
BRANCH_ID_COMPLETENESS_CHECKLIST = "completeness_checklist_present"
BRANCH_ID_CHECKLIST_ITEMS = "checklist_items_verified"
BRANCH_ID_DOC_FRAGILE_DOMAIN = "doc_fragile_domain_active"
BRANCH_ID_BIG_DIFF_BRIGHTLINE = "big_diff_brightline"
BRANCH_ID_PLAN_CLAIM_GUARD = "plan_claim_guard"
BRANCH_ID_LOE_PATH = "loe_path"


@dataclass
class BranchResolution:
    """Resolved outcome for a single branch signal in the ceremony.

    Negative-spec: this is per-branch evidence, NOT a node — it is the signal-read
    result that feeds a node entry in the ledger.  The node itself is stored in
    PipelineContext.nodes using receipt_schema shapes; BranchResolution carries the
    human-facing «what signal was read and what value did it have» provenance that
    the phase-1 receipt exposes to the EM.

    Fields:
      branch_id   — canonical identifier (see BRANCH_ID_* constants above)
      legible     — True when the signal was readable from disk (D-resolvable);
                    False for J (judgment needed), F (free prose), B (EM-turn bracket),
                    or X (illegible-state gap).  Callers should cross-check against
                    node_type.
      node_type   — one of D / J / F / B / X (matches VALID_NODE_TYPES)
      signal_read — the raw value the resolver observed (string, list, bool, or None
                    when absent/unreadable).  For X-nodes this is None.
      evidence    — arbitrary dict with read provenance: {method, path, value, ...}.
                    For J-nodes, includes the discriminating question text.
                    For F-nodes, includes the slot description.
                    For B-nodes, includes pre_resolved_evidence blob.
                    For X-nodes, includes missing_signal description.
    """

    branch_id: str
    legible: bool
    node_type: str
    signal_read: Any = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.node_type not in VALID_NODE_TYPES:
            raise ValueError(
                f"BranchResolution.node_type must be one of {sorted(VALID_NODE_TYPES)}; "
                f"got {self.node_type!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "legible": self.legible,
            "node_type": self.node_type,
            "signal_read": self.signal_read,
            "evidence": copy.deepcopy(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BranchResolution":
        return cls(
            branch_id=data["branch_id"],
            legible=data["legible"],
            node_type=data["node_type"],
            signal_read=data.get("signal_read"),
            evidence=copy.deepcopy(data.get("evidence", {})),
        )


@dataclass
class PipelineContext:
    """Resolved state for a single ceremony pipeline run.

    Populated by branch_resolution.py's _resolve_branches (C2.2, the surviving
    engine of the retired ceremony.wsc_resolve op); consumed by receipt_emit (C2.4).

    The node ledger (``nodes``) stores node dicts in the receipt_schema shapes
    (make_d_node / make_j_node / make_f_node / make_b_node / make_x_node).  The
    receipt emitter reads ``nodes`` directly to build the receipt.

    ``resolved_branches`` is the per-branch evidence layer — it parallels the node
    ledger but carries the «signal-read + provenance» view the EM audits in the
    phase-1 receipt.  The ledger is the source of truth for op_tail derivation;
    resolved_branches is the source of truth for per-branch evidence presentation.

    ``applicable_node_ids`` is the declared-membership signal (op-spec §3, Option B):
    the ordered subset of ``nodes`` ids that actually apply to this session's
    disposition.  Populated once by branch_resolution.py off already-resolved
    branch state; a receipt-reading consumer (e.g. the session_instructions render
    op) reads this list rather than scavenging per-node evidence keys for
    applicability.

    Round-trip contract: to_dict() → from_dict() produces a PipelineContext with
    identical data.  This is used by the receipt emitter and by resumption after a
    phase-1 halt.

    Disposition values:
      "single-session"    — this session is not a chain-terminal (no predecessor handoff)
      "chain-terminal"    — this session consumed a handoff (activates Steps 2.7/2.75/2.9c)
      "memo-predecessor"  — memo-attributed predecessor consume; consumed_handoffs /
                            predecessors stay empty on this leg (the memo path rides the
                            .detection record only, not a chain)
      ""                  — not yet resolved (pre-init state; should not persist to receipt)

    ``consumed_handoffs`` / ``predecessors`` — populated ONLY when disposition ==
    "chain-terminal".  ``consumed_handoffs`` is the ordered list of repo-relative
    paths to EVERY predecessor handoff this session consumed (may live under
    state/handoffs/ or archive/handoffs/ once swept — this module performs no
    disk I/O and does not itself enforce that convention; the source of truth
    is branch_resolution.py's ``_find_all_consumed_handoffs``, which scans both
    directories, and the coordinator-handoff-archive sweep script that moves
    swept handoffs from state/handoffs/ to archive/handoffs/); ``predecessors``
    is the parallel list of each handoff's frontmatter ``predecessor`` field
    (the session-id of the session that authored the handoff), when present.
    Both default to an empty list for single-session dispositions and
    pre-init state.

    ``consumed_handoff`` / ``predecessor`` — DERIVED scalar fallback, kept for
    backward compat with callers/receipts predating the N-handoff pickup model.
    Always equal to ``consumed_handoffs[0]`` / ``predecessors[0]`` when the
    corresponding list is non-empty, else "".  validate() enforces this
    derived-scalar contract; it is not merely a from_dict()-time convention.

    ``sid`` — the WSC session id this ceremony run resolves branches for;
    threaded from the handler so ``resolved_state`` carries it — closes the
    ``resolved_state.sid = null`` defect (PipelineContext had no field to
    carry the session id, so to_dict()'s output — which becomes the op's
    resolved_state — structurally could not include it).  Defaults to "" for
    pre-init state.

    ``scoping_method`` / ``foreign_commit_count`` — C3 wiring of the C1
    analyze_session_scoping() verdict into the B-wave scoping site
    (branch_resolution._resolve_branches, Branch 8).  scoping_method mirrors
    receipt_schema.VALID_SCOPING_METHODS ("session_id_trailer" |
    "started_at_contiguous_range" | "ambiguous-x-node"); "" means not yet
    resolved (pre-init state — never persisted to the receipt, same
    graceful-absent posture as applicable_node_ids).  foreign_commit_count is
    the ScopingVerdict.foreign_count observed in the scoping window; 0 by
    default (both a genuine zero-foreign-commits verdict and pre-init state
    serialize as 0 — receipt_schema's OPTIONAL/additive posture means the
    caller only sets these once a verdict exists, per emit_receipt's
    truthy/None-gated key-presence convention on the same additive fields).

    Spec backlink:
      docs/plans/2026-07-08-wsc-commit-op-defects.md § Bug-1(i)
      docs/plans/2026-07-10-wsc-resolve-foreign-repo-bleed-and-sid-null.md
      docs/plans/…-wsc-concurrent-tree-race-fix.md § C3 (scoping verdict wiring)
    """

    ceremony: str
    scope_mode: str
    disposition: str = ""
    resolved_branches: list[BranchResolution] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    applicable_node_ids: list[str] = field(default_factory=list)
    consumed_handoffs: list[str] = field(default_factory=list)
    predecessors: list[str] = field(default_factory=list)
    consumed_handoff: str = ""
    predecessor: str = ""
    sid: str = ""
    scoping_method: str = ""
    foreign_commit_count: int = 0


    def add_branch(self, resolution: BranchResolution) -> None:
        self.resolved_branches.append(resolution)

    def add_node(self, node: dict[str, Any]) -> None:
        self.nodes.append(node)

    def get_branch(self, branch_id: str) -> BranchResolution | None:
        for br in self.resolved_branches:
            if br.branch_id == branch_id:
                return br
        return None

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        for n in self.nodes:
            if n.get("id") == node_id:
                return n
        return None


    def to_dict(self) -> dict[str, Any]:
        # Emit a CONSISTENT plural
        consumed_handoffs_out = list(self.consumed_handoffs) or (
            [self.consumed_handoff] if self.consumed_handoff else []
        )
        predecessors_out = list(self.predecessors) or (
            [self.predecessor] if self.predecessor else []
        )
        return {
            "ceremony": self.ceremony,
            "scope_mode": self.scope_mode,
            "disposition": self.disposition,
            "resolved_branches": [br.to_dict() for br in self.resolved_branches],
            "nodes": copy.deepcopy(self.nodes),
            "applicable_node_ids": list(self.applicable_node_ids),
            "consumed_handoffs": consumed_handoffs_out,
            "predecessors": predecessors_out,
            "consumed_handoff": self.consumed_handoff,
            "predecessor": self.predecessor,
            "sid": self.sid,
            "scoping_method": self.scoping_method,
            "foreign_commit_count": self.foreign_commit_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineContext":
        """Reconstruct a PipelineContext from a to_dict() output.

        from_dict(ctx.to_dict()) must produce a context with identical data.

        ``consumed_handoffs`` / ``predecessors`` read the plural key BY
        PRESENCE (not truthiness) — Review: code-reviewer (Slice C1, Finding 1).
        An explicit ``[]`` means "zero handoffs" and drops any stale legacy
        scalar rather than resurrecting it; the scalar is lifted into a
        1-element list only when the plural key is truly absent (a genuinely
        legacy, scalar-only dict). The scalar fields are then re-derived as
        list[0] (or "") so the two representations agree even when
        reconstructing from a legacy, scalar-only dict. to_dict() emits a
        consistent plural list for scalar-only-constructed contexts (deriving
        the plural from the scalar when the list is empty), so scalar-only
        round-trips are unaffected by the presence-based switch here.
        """
        if "consumed_handoffs" in data:
            consumed_handoffs = list(data["consumed_handoffs"])
        else:
            legacy_consumed_handoff = data.get("consumed_handoff", "")
            consumed_handoffs = [legacy_consumed_handoff] if legacy_consumed_handoff else []

        if "predecessors" in data:
            predecessors = list(data["predecessors"])
        else:
            legacy_predecessor = data.get("predecessor", "")
            predecessors = [legacy_predecessor] if legacy_predecessor else []

        return cls(
            ceremony=data["ceremony"],
            scope_mode=data["scope_mode"],
            disposition=data.get("disposition", ""),
            resolved_branches=[
                BranchResolution.from_dict(br)
                for br in data.get("resolved_branches", [])
            ],
            nodes=copy.deepcopy(data.get("nodes", [])),
            applicable_node_ids=list(data.get("applicable_node_ids", [])),
            consumed_handoffs=consumed_handoffs,
            predecessors=predecessors,
            consumed_handoff=consumed_handoffs[0] if consumed_handoffs else "",
            predecessor=predecessors[0] if predecessors else "",
            sid=data.get("sid", ""),
            scoping_method=data.get("scoping_method", ""),
            foreign_commit_count=data.get("foreign_commit_count", 0),
        )


    def validate(self) -> list[str]:
        errors: list[str] = []

        if not self.ceremony:
            errors.append("ceremony must be a non-empty string")

        if not isinstance(self.scope_mode, str):
            errors.append(
                f"scope_mode must be a string; got {type(self.scope_mode).__name__}"
            )

        if not isinstance(self.disposition, str):
            errors.append(
                f"disposition must be a string; got {type(self.disposition).__name__}"
            )

        if not isinstance(self.consumed_handoff, str):
            errors.append(
                f"consumed_handoff must be a string; got {type(self.consumed_handoff).__name__}"
            )
        if not isinstance(self.predecessor, str):
            errors.append(
                f"predecessor must be a string; got {type(self.predecessor).__name__}"
            )
        if not isinstance(self.sid, str):
            errors.append(
                f"sid must be a string; got {type(self.sid).__name__}"
            )

        if not isinstance(self.scoping_method, str):
            errors.append(
                f"scoping_method must be a string; got {type(self.scoping_method).__name__}"
            )
        elif self.scoping_method and self.scoping_method not in VALID_SCOPING_METHODS:
            errors.append(
                f"scoping_method {self.scoping_method!r} not in "
                f"{sorted(VALID_SCOPING_METHODS)} when non-empty"
            )
        if not isinstance(self.foreign_commit_count, int) or isinstance(
            self.foreign_commit_count, bool
        ):
            errors.append(
                "foreign_commit_count must be an int; got "
                f"{type(self.foreign_commit_count).__name__}"
            )

        for i, ch in enumerate(self.consumed_handoffs):
            if not isinstance(ch, str):
                errors.append(
                    f"consumed_handoffs[{i}] must be a string; got {type(ch).__name__}"
                )
        for i, pred in enumerate(self.predecessors):
            if not isinstance(pred, str):
                errors.append(
                    f"predecessors[{i}] must be a string; got {type(pred).__name__}"
                )
        disposition_for_check = (
            canonicalize(self.disposition) if self.disposition in VALID else self.disposition
        )
        if disposition_for_check != PREDECESSOR_CONSUMED and (self.consumed_handoffs or self.predecessors):
            errors.append(
                "consumed_handoffs/predecessors must be empty unless "
                f"disposition == 'chain-terminal'; got disposition={self.disposition!r}, "
                f"consumed_handoffs={self.consumed_handoffs!r}, predecessors={self.predecessors!r}"
            )

        expected_consumed_handoff = self.consumed_handoffs[0] if self.consumed_handoffs else ""
        if isinstance(self.consumed_handoff, str) and self.consumed_handoff != expected_consumed_handoff:
            errors.append(
                "consumed_handoff must equal consumed_handoffs[0] (or '' when empty); "
                f"got consumed_handoff={self.consumed_handoff!r}, "
                f"consumed_handoffs={self.consumed_handoffs!r}"
            )
        expected_predecessor = self.predecessors[0] if self.predecessors else ""
        if isinstance(self.predecessor, str) and self.predecessor != expected_predecessor:
            errors.append(
                "predecessor must equal predecessors[0] (or '' when empty); "
                f"got predecessor={self.predecessor!r}, predecessors={self.predecessors!r}"
            )

        if self.disposition and not self.sid:
            errors.append(
                "sid must be non-empty when disposition is set "
                "(resolved_state.sid must never be null once resolution has run)"
            )

        for i, node in enumerate(self.nodes):
            if not isinstance(node, dict):
                errors.append(f"nodes[{i}] must be a dict; got {type(node).__name__}")
                continue
            ntype = node.get("type")
            if ntype not in VALID_NODE_TYPES:
                errors.append(
                    f"nodes[{i}] has invalid type {ntype!r}; "
                    f"must be one of {sorted(VALID_NODE_TYPES)}"
                )

        for i, br in enumerate(self.resolved_branches):
            if not isinstance(br, BranchResolution):
                errors.append(
                    f"resolved_branches[{i}] must be a BranchResolution; "
                    f"got {type(br).__name__}"
                )

        for i, nid in enumerate(self.applicable_node_ids):
            if not isinstance(nid, str):
                errors.append(
                    f"applicable_node_ids[{i}] must be a string; got {type(nid).__name__}"
                )

        return errors
