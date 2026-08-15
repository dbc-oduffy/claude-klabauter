"""
coordinator_core.ops.ceremony.receipt_schema — Ceremony evidence receipt schema.

Purpose: Defines the shape, constants, factory helpers, and validation for
state/ceremony/<ceremony>-receipt.json.  This is a PURE DATA/SCHEMA module — it
owns structure and validation only.  The file is written by the emitter (C2.4,
receipt_emit.py); this module is NEVER responsible for disk I/O.

General-from-day-one design: the schema is shared by all ceremonies (#3 corpus).
Adding a second ceremony requires a new ceremony name and phase label — no schema
change.  The A8 naming-generality gate (docs/plans/…-invert-workstream.md § A8)
verified this before freezing the schema.

Artifact path:  state/ceremony/<ceremony>-receipt.json
Schema version: 1 (integer — self-consistent with session-shape.json convention;
                NOT a live cockpit gate; cockpit gates via semver on cockpit-emission.json).

Receipt top-level shape:
  schema_version    int         — always 1; INTEGER not semver
  ceremony          str         — ceremony name (e.g. "wsc")
  phase             str         — "phase-1" (pre-resolution) | "phase-2" (post-commit)
  emitted_at        str         — ISO 8601 UTC timestamp (write-time)
  scope_mode        str         — session scope_mode from session-shape.json (write-time fact);
                                  NEVER a derived light|full ceremony weight (pcore-06 / C7)
  nodes             list[dict]  — node ledger (single source of truth for op_tail derivation)
  op_tail           dict        — {phase, acted[], skipped[], failed[]}; capability-generic
                                  derived view over D-node tail entries; computed at emit time,
                                  NOT stored independently of the ledger
  coverage_pointer  str         — repo-relative path to state/coverage/gate-result.json;
                                  a reference only — receipt does NOT gate on coverage success
  applicable_node_ids  list[str]  — OPTIONAL, additive.  Ordered step-IDs that apply to
                                  this session's disposition, declared once by the
                                  producer (wsc_resolve) off already-resolved state —
                                  same graceful-absent posture as failed_critical (see
                                  _REQUIRED_OP_TAIL_FIELDS below).  Absent on
                                  pre-existing / hand-crafted receipts; a receipt WITH
                                  and WITHOUT this field both validate.
  scoping_method    str         — OPTIONAL, additive.  How wsc_resolve scoped the
                                  ceremony's commits: "session_id_trailer" |
                                  "started_at_contiguous_range" | "ambiguous-x-node".
                                  Same graceful-absent posture as applicable_node_ids;
                                  enum-checked only when present.
  foreign_commit_count  int     — OPTIONAL, additive.  Count of commits observed in
                                  the scoping window that were NOT attributable to
                                  this session (concurrent-tree race visibility).
                                  Same graceful-absent posture as applicable_node_ids;
                                  type-checked only when present.

Node ledger entry shapes (discriminated by "type" field):
  D — {id, type:"D", resolving_op:str, evidence:dict, tail_step:bool}
      tail_step=True marks the 8 fleet-ops in the deterministic tail (used to
      derive op_tail; pre-resolution D-nodes have tail_step=False)
  J — {id, type:"J", question:str, answer:str}
      answer="" when unanswered (phase-1); filled during EM turn
  F — {id, type:"F", slot:str, filled:str}
      filled="" when empty (phase-1); authored during EM turn
  B — {id, type:"B", pre_resolved_evidence:dict, em_adjudication:dict}
      GENERIC two-slot shape — wsc fills pre_resolved_evidence with review-wave keys
      (slice count, partition boundaries, docs-checker inclusion) and em_adjudication
      with aggregate WARN/BLOCKED verdict + integration disposition.  A different
      ceremony uses different blob content WITHOUT renaming these sub-keys (the
      tell we built the schema wrong is a second ceremony needing a rename).
  X — {id, type:"X", missing_signal:str}
      illegible-state gap; NOT auto-resolvable; present in receipt as documentation

op_tail shape:
  phase   str        — ceremony-specific label ("archival" for wsc; "consume" for pickup;
                       etc.) — no schema change when adding a ceremony, only the value changes
  acted   list[str]  — items acted on by the tail ops (aggregated across all tail D-nodes)
  skipped list[str]  — items skipped
  failed  list[str]  — items that failed; legible in receipt for best-effort-with-report

Graceful-absent rules (HARD):
  - Absent file = "pipeline not yet run" — not an error; callers MUST treat absence gracefully
  - Every required field present; empty arrays NEVER key-absent
  - op_tail ALWAYS present even when tail did not run: {phase:"<label>", acted:[], skipped:[], failed:[]}
    — absence of the op_tail object reintroduces the "absent vs. not-run" ambiguity

Spec backlink:
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § Design → receipt
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
"""Schema version integer — not semver; consistent with session-shape.json convention."""

COVERAGE_GATE_RELPATH: str = "state/coverage/gate-result.json"
"""Repo-relative path to the coverage gate artifact; used as coverage_pointer value."""

CEREMONY_RECEIPT_DIR: str = "state/ceremony"
"""Repo-relative directory where ceremony receipts are written."""

VALID_PHASES_TOP: frozenset[str] = frozenset({"phase-1", "phase-2"})
"""Valid values for the receipt top-level ``phase`` field."""

VALID_NODE_TYPES: frozenset[str] = frozenset({"D", "J", "F", "B", "X"})
"""Valid node type discriminants for the ledger."""

VALID_SCOPING_METHODS: frozenset[str] = frozenset(
    {"session_id_trailer", "started_at_contiguous_range", "ambiguous-x-node"}
)
"""Valid values for the receipt top-level ``scoping_method`` field (C2, additive)."""

# ---------------------------------------------------------------------------
# Node factory helpers — produce schema-valid node dicts with all keys present
# ---------------------------------------------------------------------------


def make_d_node(
    node_id: str,
    resolving_op: str = "",
    evidence: dict[str, Any] | None = None,
    *,
    tail_step: bool = False,
) -> dict[str, Any]:
    """Return a schema-valid D-node dict.

    tail_step=True marks this D-node as belonging to the deterministic tail;
    these entries feed the op_tail derived view via compute_op_tail().
    evidence for tail D-nodes should carry the fleet-op result shape:
    {acted:[], skipped:[], failed:[], ...} so compute_op_tail can aggregate.
    """
    return {
        "id": node_id,
        "type": "D",
        "resolving_op": resolving_op,
        "evidence": evidence if evidence is not None else {},
        "tail_step": tail_step,
    }


def make_j_node(
    node_id: str,
    question: str = "",
    answer: str = "",
) -> dict[str, Any]:
    """Return a schema-valid J-node dict.

    answer="" indicates the question has not yet been answered (phase-1).
    """
    return {
        "id": node_id,
        "type": "J",
        "question": question,
        "answer": answer,
    }


def make_f_node(
    node_id: str,
    slot: str = "",
    filled: str = "",
) -> dict[str, Any]:
    """Return a schema-valid F-node dict.

    filled="" indicates the slot has not yet been authored (phase-1).
    """
    return {
        "id": node_id,
        "type": "F",
        "slot": slot,
        "filled": filled,
    }


def make_b_node(
    node_id: str,
    pre_resolved_evidence: dict[str, Any] | None = None,
    em_adjudication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a schema-valid B-node dict.

    GENERIC two-slot shape: pre_resolved_evidence and em_adjudication are
    ceremony-instance-specific blobs.  For wsc: pre_resolved_evidence carries
    review-wave keys (slice count, partition boundaries, docs-checker inclusion);
    em_adjudication carries aggregate WARN/BLOCKED + integration disposition.
    A different ceremony fills different blob content WITHOUT renaming these keys.

    Negative-spec: do NOT add wsc-specific sub-keys (dispatch_plan, adjudication,
    review_verdict, etc.) to this function — the generality is the invariant.
    """
    return {
        "id": node_id,
        "type": "B",
        "pre_resolved_evidence": pre_resolved_evidence if pre_resolved_evidence is not None else {},
        "em_adjudication": em_adjudication if em_adjudication is not None else {},
    }


def make_x_node(
    node_id: str,
    missing_signal: str = "",
) -> dict[str, Any]:
    """Return a schema-valid X-node dict (illegible-state gap)."""
    return {
        "id": node_id,
        "type": "X",
        "missing_signal": missing_signal,
    }


# ---------------------------------------------------------------------------
# op_tail helpers
# ---------------------------------------------------------------------------


def make_empty_op_tail(phase: str) -> dict[str, Any]:
    """Return an op_tail dict with all keys present and empty arrays.

    Used when the tail did not run; the graceful-absent rule forbids a key-absent
    op_tail object (absence-of-object reintroduces the not-run ambiguity).

    C3 — structured failure-class discriminator:
      failed[]          environmental-class failures (tolerant, recorded-not-fatal per
                        BEST-EFFORT-WITH-REPORT contract at wsc_commit.py:29-32)
      failed_critical[] critical-class failures (hard-exit-1 via C3 exit predicate);
                        populated by tail ops that detect integrity-breach conditions
                        (e.g. C6 review-trail fail-loud when b_adjudication present
                        but review_trail absent/incomplete).  Empty when no critical
                        failures occurred.  Exit predicate reads this field directly —
                        NOT via substring-matching on failed[].
    """
    return {
        "phase": phase,
        "acted": [],
        "skipped": [],
        "failed": [],
        "failed_critical": [],  # C3: structured critical-class failures (hard-exit-1)
    }


def compute_op_tail(nodes: list[dict[str, Any]], phase: str) -> dict[str, Any]:
    """Derive the op_tail view from the node ledger.

    Filters D-nodes with tail_step=True, then aggregates their evidence.acted[],
    evidence.skipped[], evidence.failed[], and evidence.failed_critical[] into
    four partitions.

    C3 — structured failure-class discriminator:
      failed[]          environmental-class (tolerant; aggregated from evidence.failed[]).
                        Critical-class failures do NOT also appear in failed[] — they
                        appear ONLY in failed_critical[].  Callers MUST NOT check
                        failed[] to detect critical failures.
      failed_critical[] critical-class (hard-exit-1; aggregated from
                        evidence.failed_critical[]).  Absent from pre-C3 evidence dicts —
                        graceful-absent via .get("failed_critical", []).

    op_tail is the SINGLE DERIVED VIEW — it is computed here from the ledger and
    stored in the receipt at emit time.  The ledger (nodes) is the source of truth;
    op_tail is not stored independently.

    When no tail D-nodes are present, returns make_empty_op_tail(phase) — all
    empty arrays, fully graceful-absent.
    """
    acted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    failed_critical: list[str] = []  # C3: structured critical-class failures

    for node in nodes:
        if node.get("type") != "D" or not node.get("tail_step", False):
            continue
        evidence = node.get("evidence", {})
        acted.extend(evidence.get("acted", []))
        skipped.extend(evidence.get("skipped", []))
        failed.extend(evidence.get("failed", []))
        failed_critical.extend(evidence.get("failed_critical", []))  # C3: graceful-absent

    return {
        "phase": phase,
        "acted": acted,
        "skipped": skipped,
        "failed": failed,
        "failed_critical": failed_critical,  # C3: empty list when no critical failures
    }


# ---------------------------------------------------------------------------
# Receipt factory
# ---------------------------------------------------------------------------


def make_receipt(
    ceremony: str,
    phase: str,
    emitted_at: str,
    scope_mode: str,
    nodes: list[dict[str, Any]] | None = None,
    op_tail: dict[str, Any] | None = None,
    coverage_pointer: str = COVERAGE_GATE_RELPATH,
    sid: str | None = None,
    applicable_node_ids: list[str] | None = None,
    engine_sha: str | None = None,
    engine_dirty: bool | None = None,
    scoping_method: str | None = None,
    foreign_commit_count: int | None = None,
) -> dict[str, Any]:
    """Return a schema-valid receipt dict with all required fields present.

    op_tail should be computed by the caller via compute_op_tail(nodes, tail_phase)
    before passing in.  When omitted, make_empty_op_tail("<phase>") is used;
    the caller must supply the correct tail phase label for their ceremony.

    sid (review-integrator 2026-07-08 Finding 5, OPTIONAL/additive — same
    posture as applicable_node_ids): the ceremony session id, when the caller
    has one.  Enables a body-level sid verification at read-back time
    (receipt_emit.resolve_latest_receipt_path's shard-filename isolation is
    the primary defense — Finding 1 — this is defense-in-depth on top of it,
    see wsc_commit._build_ctx_from_params).  Omitted (key absent) when the
    caller does not supply sid — a receipt with and without this field both
    validate; absence is graceful, never a hard requirement.

    applicable_node_ids (OPTIONAL/additive, op-spec §3 Option B): the ordered
    declared-membership list from PipelineContext.applicable_node_ids.  Omitted
    (key absent) when the caller passes None — a receipt with and without this
    field both validate.  Callers that HAVE a resolved ctx should always pass
    ctx.applicable_node_ids so the persisted receipt carries the field; the
    default of None exists only for hand-crafted / pre-existing receipt callers.

    engine_sha (OPTIONAL/additive, C2 — coordinator_core.engine_version): the
    resolved git HEAD SHA of the coordinator_core copy that produced this
    receipt, per resolve_engine_sha().  Omitted (key absent) when the caller
    passes None — a receipt with and without this field both validate; same
    graceful-absent posture as sid.  (Review: code-reviewer — trimmed the
    why-None-happens detail out of this pure-data module; resolve_engine_sha()'s
    own docstring in engine_version.py is the single source of truth for its
    failure modes.)

    engine_dirty (OPTIONAL/additive — coordinator_core.engine_version): whether
    the coordinator_core copy that produced this receipt had a dirty engine
    source tree at emit time, per resolve_engine_dirty().  Omitted (key absent)
    when the caller passes None — a receipt with and without this field both
    validate.  Note engine_dirty=False is a meaningful, real finding ("the
    engine source was clean") and is still emitted, so the emission check below
    is `is not None`, not truthiness — same None-vs-value convention
    foreign_commit_count uses (0 is a real finding too).

    scoping_method / foreign_commit_count (OPTIONAL/additive, C3 — wsc
    concurrent-tree race fix): the C1 analyze_session_scoping() verdict,
    threaded through by the caller from PipelineContext.scoping_method /
    .foreign_commit_count.  Omitted (key absent) when the caller passes None
    — a receipt with and without these fields both validate; same
    graceful-absent posture as sid/engine_sha.  foreign_commit_count is
    written even when 0 (a genuine "zero foreign commits observed" verdict
    is a real finding, not an absence) — the None/omitted-vs-0 distinction is
    what makes 0 unambiguous, same convention applicable_node_ids uses for
    None-vs-empty-list.

    Graceful-absent: nodes defaults to [] not None; op_tail defaults to empty with
    an empty phase label (callers should always supply the op_tail explicitly via
    compute_op_tail).
    """
    node_list = nodes if nodes is not None else []
    tail = op_tail if op_tail is not None else make_empty_op_tail("")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ceremony": ceremony,
        "phase": phase,
        "emitted_at": emitted_at,
        "scope_mode": scope_mode,
        "nodes": node_list,
        "op_tail": tail,
        "coverage_pointer": coverage_pointer,
    }
    if sid:
        receipt["sid"] = sid
    if applicable_node_ids is not None:
        receipt["applicable_node_ids"] = list(applicable_node_ids)
    if engine_sha:
        receipt["engine_sha"] = engine_sha
    if engine_dirty is not None:
        receipt["engine_dirty"] = engine_dirty
    if scoping_method:
        receipt["scoping_method"] = scoping_method
    if foreign_commit_count is not None:
        receipt["foreign_commit_count"] = foreign_commit_count
    return receipt


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REQUIRED_TOP_FIELDS: tuple[str, ...] = (
    "schema_version",
    "ceremony",
    "phase",
    "emitted_at",
    "scope_mode",
    "nodes",
    "op_tail",
    "coverage_pointer",
)

_OPTIONAL_TOP_FIELDS: tuple[str, ...] = (
    "applicable_node_ids",
    "sid",
    "engine_sha",
    "engine_dirty",
    "scoping_method",
    "foreign_commit_count",
)
# D1: additive/backward-compat top-level fields — NOT in _REQUIRED_TOP_FIELDS.
# A receipt with and without an optional field both validate; when present, it
# is still type-checked below (mirrors op_tail.failed_critical's graceful-absent-
# but-typed-when-present posture, documented just below).
# `sid` (review-integrator 2026-07-08 Finding 5) is additive for the same reason:
# legacy/pre-Finding-5 receipts never carried it and must still validate clean.
# `engine_sha` (C2, coordinator_core.engine_version) is additive for the same
# reason: pre-C2 receipts never carried it and must still validate clean.
# `engine_dirty` (coordinator_core.engine_version) is additive for the same
# reason: pre-existing receipts never carried it and must still validate
# clean.  It is bool-checked only when present; engine_dirty=False is a real
# finding and still validates (same None-vs-value posture as
# foreign_commit_count's None-vs-0).
# `scoping_method` / `foreign_commit_count` (C2, wsc concurrent-tree race fix)
# are additive for the same reason: pre-C2 receipts never carried them and must
# still validate clean.  scoping_method is enum-checked (VALID_SCOPING_METHODS)
# only when present; foreign_commit_count is int-checked only when present.

_REQUIRED_OP_TAIL_FIELDS: tuple[str, ...] = ("phase", "acted", "skipped", "failed")
# Review: code-reviewer Slice-A F3 — `failed_critical` is intentionally ABSENT from
# _REQUIRED_OP_TAIL_FIELDS for backward-compat with pre-C3 receipts (external tooling,
# hand-crafted dicts, phase-1 receipts produced before C3 landed).  However,
# failed_critical is load-bearing for the C3(C) exit predicate — a receipt that passes
# validate() but omits failed_critical will silently prevent C3(C) from ever firing
# (op_tail.get("failed_critical") returns None → bool(None) = False).
# Constraint: make_empty_op_tail() and compute_op_tail() always include failed_critical;
# external producers must also include it for C3(C) to function correctly.
# See test_phase2_receipt_schema_valid for the positive assertion that the emitted
# receipt always carries failed_critical as a list.

_NODE_TYPE_REQUIRED: dict[str, tuple[str, ...]] = {
    "D": ("id", "type", "resolving_op", "evidence", "tail_step"),
    "J": ("id", "type", "question", "answer"),
    "F": ("id", "type", "slot", "filled"),
    "B": ("id", "type", "pre_resolved_evidence", "em_adjudication"),
    "X": ("id", "type", "missing_signal"),
}


def validate(receipt: dict[str, Any]) -> list[str]:
    """Validate a receipt dict against the schema.

    Returns a list of human-readable error strings.  An empty list means the
    receipt is valid.  Callers should treat a non-empty list as a schema error
    and surface it before attempting to use the receipt.

    This function does NOT perform disk I/O — it validates structure only.
    """
    errors: list[str] = []

    if not isinstance(receipt, dict):
        return [f"receipt must be a dict; got {type(receipt).__name__}"]

    # --- top-level required fields ---
    for field in _REQUIRED_TOP_FIELDS:
        if field not in receipt:
            errors.append(f"required field missing: {field!r}")

    if errors:
        # Cannot proceed with structural checks without the basic fields.
        return errors

    # --- schema_version ---
    sv = receipt["schema_version"]
    if not isinstance(sv, int) or isinstance(sv, bool):
        errors.append(
            f"schema_version must be an integer; got {type(sv).__name__!r} ({sv!r})"
        )
    elif sv < 1:
        errors.append(f"schema_version must be >= 1; got {sv}")

    # --- ceremony ---
    if not isinstance(receipt["ceremony"], str) or not receipt["ceremony"]:
        errors.append("ceremony must be a non-empty string")

    # --- phase (top-level) ---
    top_phase = receipt["phase"]
    if not isinstance(top_phase, str) or not top_phase:
        errors.append("phase must be a non-empty string")
    # Review: code-reviewer F4 — VALID_PHASES_TOP was defined but never enforced;
    # a receipt with phase="garbage" previously passed validation silently.
    elif top_phase not in VALID_PHASES_TOP:
        errors.append(f"phase {top_phase!r} not in {VALID_PHASES_TOP}")

    # --- emitted_at ---
    if not isinstance(receipt["emitted_at"], str) or not receipt["emitted_at"]:
        errors.append("emitted_at must be a non-empty string")

    # --- scope_mode ---
    if not isinstance(receipt["scope_mode"], str):
        errors.append(f"scope_mode must be a string; got {type(receipt['scope_mode']).__name__}")

    # --- coverage_pointer ---
    if not isinstance(receipt["coverage_pointer"], str):
        errors.append(
            f"coverage_pointer must be a string; got {type(receipt['coverage_pointer']).__name__}"
        )

    # --- applicable_node_ids (optional, additive — D1) ---
    if "applicable_node_ids" in receipt:
        anids = receipt["applicable_node_ids"]
        if not isinstance(anids, list) or not all(isinstance(x, str) for x in anids):
            errors.append("applicable_node_ids must be a list[str] when present")

    # --- sid (optional, additive — Finding 5) ---
    if "sid" in receipt:
        if not isinstance(receipt["sid"], str) or not receipt["sid"]:
            errors.append("sid must be a non-empty string when present")

    # --- engine_sha (optional, additive — C2) ---
    if "engine_sha" in receipt:
        if not isinstance(receipt["engine_sha"], str):
            errors.append("engine_sha must be a str when present")

    # --- engine_dirty (optional, additive — coordinator_core.engine_version) ---
    if "engine_dirty" in receipt:
        if not isinstance(receipt["engine_dirty"], bool):
            errors.append("engine_dirty must be a bool when present")

    # --- scoping_method (optional, additive — C2, wsc concurrent-tree race fix) ---
    if "scoping_method" in receipt:
        sm = receipt["scoping_method"]
        if not isinstance(sm, str) or sm not in VALID_SCOPING_METHODS:
            errors.append(
                f"scoping_method {sm!r} not in {sorted(VALID_SCOPING_METHODS)} when present"
            )

    # --- foreign_commit_count (optional, additive — C2, wsc concurrent-tree race fix) ---
    if "foreign_commit_count" in receipt:
        fcc = receipt["foreign_commit_count"]
        if not isinstance(fcc, int) or isinstance(fcc, bool):
            errors.append(
                f"foreign_commit_count must be an int when present; got {type(fcc).__name__}"
            )

    # --- op_tail ---
    op_tail = receipt["op_tail"]
    if not isinstance(op_tail, dict):
        errors.append(f"op_tail must be a dict; got {type(op_tail).__name__}")
    else:
        for tf in _REQUIRED_OP_TAIL_FIELDS:
            if tf not in op_tail:
                errors.append(f"op_tail missing required field: {tf!r}")
        # Review: code-reviewer F5 — op_tail.phase="" previously passed validation;
        # an empty phase label is meaningless (schema docstring requires e.g. "archival").
        if "phase" in op_tail and not (isinstance(op_tail["phase"], str) and op_tail["phase"]):
            errors.append("op_tail.phase must be a non-empty string")
        if "acted" in op_tail and not isinstance(op_tail["acted"], list):
            errors.append(f"op_tail.acted must be a list; got {type(op_tail['acted']).__name__}")
        if "skipped" in op_tail and not isinstance(op_tail["skipped"], list):
            errors.append(f"op_tail.skipped must be a list; got {type(op_tail['skipped']).__name__}")
        if "failed" in op_tail and not isinstance(op_tail["failed"], list):
            errors.append(f"op_tail.failed must be a list; got {type(op_tail['failed']).__name__}")
        # C3: failed_critical is optional in old receipts (graceful-absent); when present, must be a list.
        if "failed_critical" in op_tail and not isinstance(op_tail["failed_critical"], list):
            errors.append(
                f"op_tail.failed_critical must be a list; got {type(op_tail['failed_critical']).__name__}"
            )

    # --- nodes ---
    nodes = receipt["nodes"]
    if not isinstance(nodes, list):
        errors.append(f"nodes must be a list; got {type(nodes).__name__}")
    else:
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                errors.append(f"nodes[{i}] must be a dict; got {type(node).__name__}")
                continue
            node_type = node.get("type")
            if node_type not in VALID_NODE_TYPES:
                errors.append(
                    f"nodes[{i}] has invalid type {node_type!r}; "
                    f"must be one of {sorted(VALID_NODE_TYPES)}"
                )
                continue
            required_keys = _NODE_TYPE_REQUIRED.get(node_type, ("id", "type"))
            for k in required_keys:
                if k not in node:
                    errors.append(f"nodes[{i}] (type={node_type!r}) missing field: {k!r}")
            # Type-specific structural checks
            if node_type == "D":
                if "evidence" in node and not isinstance(node["evidence"], dict):
                    errors.append(
                        f"nodes[{i}] (D) evidence must be a dict; "
                        f"got {type(node['evidence']).__name__}"
                    )
                if "tail_step" in node and not isinstance(node["tail_step"], bool):
                    errors.append(
                        f"nodes[{i}] (D) tail_step must be bool; "
                        f"got {type(node['tail_step']).__name__}"
                    )
            elif node_type == "B":
                if "pre_resolved_evidence" in node and not isinstance(
                    node["pre_resolved_evidence"], dict
                ):
                    errors.append(
                        f"nodes[{i}] (B) pre_resolved_evidence must be a dict; "
                        f"got {type(node['pre_resolved_evidence']).__name__}"
                    )
                if "em_adjudication" in node and not isinstance(node["em_adjudication"], dict):
                    errors.append(
                        f"nodes[{i}] (B) em_adjudication must be a dict; "
                        f"got {type(node['em_adjudication']).__name__}"
                    )

    return errors
