"""
coordinator_core.ops.dispatch_emit.grind_profile — queue-grind profile
loader, stage-graph validator and appetite resolution.

Purpose: turns a profile YAML file (DoE data — the engine never authors one)
into a validated ``Profile`` the rest of the queue-grind engine reads.
``load_profile`` checks only what the engine owns: vocabulary membership
against ``coordinator_core.contract.grind_vocab``, the field types this
module reads, and refusal of unknown top-level keys. DoE's own
``queue-grind-profile.schema.json`` (which imports the same vocabulary
module) is the schema gate; this module does not re-read it, so it stays
cloud-safe and decoupled from DoE's directory layout (§ Design § Profile,
EM note 2, docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md Tasks § C3).

``validate_graph`` enforces every stage-graph rule named in § Design §
Profile:

  - totality: a ``triage`` node's edges must cover every verdict the
    profile itself declares, exactly (no extra, no missing);
  - membership (EM-authored, DoE request): every OTHER node's edges are
    checked against its own stage kind's outcome set from
    ``vocab.STAGE_OUTCOMES`` — a stray outcome outside that set is refused,
    naming the node and the outcome;
  - the graph is acyclic apart from ``on_fail``, a single optional back-edge
    per node the engine bounds to one traversal per path; an ``on_fail`` (like
    any edge) may instead name a hand-back type, ending the path there;
  - edge targets are graph nodes, universal hand-back types, or the
    profile's own ``hand_back_types`` (DR-404 § 3), which must be disjoint
    from both;
  - the floor: no path reaches a commit without having passed through
    ``refute-close`` or a ``fix``, and no ``fix``-carrying path reaches
    commit without a ``verify`` pass downstream of the LAST fix on that
    path (a verify followed by another fix does not count — the tracked
    flag resets on every fix node);
  - ``refute-close`` is agent-only: a ``refute-close`` node may never carry
    a ``verify`` block;
  - a ``verify`` node's ``verify`` map has a ``default`` entry, and every
    ``op``-mode entry names a member of ``vocab.VERIFY_OPS``;
  - the profile's ``closure.closed_values`` map covers every closing branch
    (``fix``, ``refute-close``) the graph can actually reach (EM note 3).

Each refusal is its own exception class naming the offending node (and, for
path-shaped rules, the path that reached it) — never a single generic
``ValueError`` a caller has to string-match.

Negative spec: this module owns no stage composition, no dispatch, no run
state. It reads a profile and validates its shape; ``grind_stages.py`` and
``grind_compose.py`` are the modules that turn a validated ``Profile`` into
an emitted script.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.contract import grind_vocab as vocab

# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

_TOP_LEVEL_KEYS = frozenset(
    {
        "row_id_key",
        "batch_key",
        "priority",
        "verdicts",
        "graph",
        "closure",
        "absent_sentinels",
        "triage_policy",
        "appetite",
        "archive_path",
        "schema",
        "source",
        "hand_back_types",
    }
)

_REQUIRED_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS - {"absent_sentinels", "source", "hand_back_types"}

_HANDBACK_TYPE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_NODE_KEYS = frozenset({"kind", "edges", "on_fail", "verify"})

_REQUIRED_KNOBS = frozenset(
    {"concurrency", "extra_verification", "batch_size", "triage_depth", "window", "max_agent_calls"}
)


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    edges: dict[str, str] = field(default_factory=dict)
    on_fail: str | None = None
    verify: dict | None = None


@dataclass(frozen=True)
class Closure:
    status_field: str
    closed_values: dict[str, str]
    stamp_fields: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    name: str
    row_id_key: str
    batch_key: tuple[str, ...]
    priority: dict
    verdicts: tuple[str, ...]
    graph: dict[str, GraphNode]
    closure: Closure
    absent_sentinels: dict[str, tuple[str, ...]]
    triage_policy: str
    triage_policy_sha256: str
    appetite: dict[str, dict]
    archive_path: str
    schema: str
    source: Optional[dict]
    source_path: Path
    hand_back_types: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Exceptions — one class, naming the offending node/path via `rule`/`node`,
# never a bare ValueError a caller has to string-match.
# ---------------------------------------------------------------------------


class ProfileError(ValueError):
    """The one refusal class every check in this module raises.

    ``rule`` names which check failed (a short, stable snake_case tag — see
    the call sites below for the full set: ``invalid_profile_name``,
    ``unknown_profile_key``, ``profile_field_type``, ``unknown_node_kind``,
    ``refute_close_not_agent_only``, ``verify_default_missing``,
    ``verify_op_unknown``, ``unknown_edge_target``, ``stray_outcome``,
    ``triage_verdict_totality``, ``graph_cycle``, ``graph_on_fail_traversal``,
    ``verify_floor``, ``closing_floor``, ``closure_block``,
    ``closure_branch_missing``, ``unknown_appetite_preset``,
    ``unoverridable_knob``, ``hand_back_type_collision``).
    ``node`` is the offending node/knob id when the rule is node-shaped,
    ``None`` for a profile-wide or path-shaped rule
    (the path itself is folded into ``detail``). ``detail`` is the
    free-text description. One class rather than a per-rule subclass: no
    caller in this repo catches by a per-rule attribute, only by ``rule``
    text or the bare class."""

    def __init__(self, rule: str, node: str | None = None, detail: str = "") -> None:
        where = f" ({node})" if node else ""
        message = f"{rule}{where}: {detail}" if detail else f"{rule}{where}"
        super().__init__(message)
        self.rule = rule
        self.node = node
        self.detail = detail


# ---------------------------------------------------------------------------
# load_profile
# ---------------------------------------------------------------------------


def load_profile(name: str, profile_dir: str | Path) -> Profile:
    """Load and validate ``<profile_dir>/<name>.yaml``.

    ``name`` must match ``^[a-z][a-z0-9-]*$`` — no path separators, refused
    before any join onto ``profile_dir`` happens. Only the field types this
    module reads are checked; DoE's own JSON-schema gate is the fuller
    check (EM note 2)."""
    if "/" in name or "\\" in name or not _NAME_RE.match(name):
        raise ProfileError("invalid_profile_name", detail=f"invalid profile name: {name!r}")

    path = Path(profile_dir) / f"{name}.yaml"
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ProfileError("profile_field_type", detail="profile document must be a mapping")

    unknown = set(doc) - _TOP_LEVEL_KEYS
    if unknown:
        raise ProfileError("unknown_profile_key", detail=f"unknown key(s): {sorted(unknown)}")
    missing = _REQUIRED_TOP_LEVEL_KEYS - set(doc)
    if missing:
        raise ProfileError("profile_field_type", detail=f"missing required top-level key(s): {sorted(missing)}")

    row_id_key = doc["row_id_key"]
    if not isinstance(row_id_key, str) or not row_id_key:
        raise ProfileError("profile_field_type", detail="row_id_key must be a non-empty string")

    batch_key = doc["batch_key"]
    if not isinstance(batch_key, list) or not batch_key or not all(isinstance(k, str) for k in batch_key):
        raise ProfileError("profile_field_type", detail="batch_key must be a non-empty list of strings")

    priority = doc["priority"]
    if (
        not isinstance(priority, dict)
        or not isinstance(priority.get("field"), str)
        or priority.get("order") not in ("asc", "desc")
    ):
        raise ProfileError("profile_field_type", detail="priority must be {field: str, order: 'asc'|'desc'}")

    verdicts = doc["verdicts"]
    if not isinstance(verdicts, list) or not verdicts or not all(isinstance(v, str) for v in verdicts):
        raise ProfileError("profile_field_type", detail="verdicts must be a non-empty list of strings")

    graph = _build_graph(doc["graph"])
    closure = _build_closure(doc["closure"])
    appetite = _build_appetite(doc["appetite"])

    absent_sentinels_raw = doc.get("absent_sentinels", {}) or {}
    if not isinstance(absent_sentinels_raw, dict):
        raise ProfileError("profile_field_type", detail="absent_sentinels must be a mapping")
    absent_sentinels = {}
    for key, values in absent_sentinels_raw.items():
        if not isinstance(values, list):
            raise ProfileError("profile_field_type", detail=f"absent_sentinels[{key!r}] must be a list")
        absent_sentinels[key] = tuple(values)

    triage_policy = doc["triage_policy"]
    if not isinstance(triage_policy, str) or not triage_policy:
        raise ProfileError("profile_field_type", detail="triage_policy must be a non-empty string")
    digest = hashlib.sha256(triage_policy.encode("utf-8")).hexdigest()

    archive_path = doc["archive_path"]
    if not isinstance(archive_path, str) or not archive_path:
        raise ProfileError("profile_field_type", detail="archive_path must be a non-empty string")

    schema_rel_path = doc["schema"]
    if not isinstance(schema_rel_path, str) or not schema_rel_path:
        raise ProfileError("profile_field_type", detail="schema must be a non-empty string")

    source_block = doc.get("source")
    if source_block is not None:
        if not isinstance(source_block, dict) or not isinstance(source_block.get("op"), str):
            raise ProfileError("profile_field_type", detail="source, when present, must be {op: str, args?: dict}")

    hand_back_types = _build_hand_back_types(doc.get("hand_back_types", []), graph)

    return Profile(
        name=name,
        row_id_key=row_id_key,
        batch_key=tuple(batch_key),
        priority=dict(priority),
        verdicts=tuple(verdicts),
        graph=graph,
        closure=closure,
        absent_sentinels=absent_sentinels,
        triage_policy=triage_policy,
        triage_policy_sha256=digest,
        appetite=appetite,
        archive_path=archive_path,
        schema=schema_rel_path,
        source=dict(source_block) if source_block is not None else None,
        source_path=path,
        hand_back_types=hand_back_types,
    )


def _build_hand_back_types(raw: object, graph: dict[str, "GraphNode"]) -> tuple[str, ...]:
    """A profile's own hand-back types: kebab-case, unique, and disjoint from
    both the universal set and the graph's node ids — an edge target must
    name exactly one thing."""
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise ProfileError("profile_field_type", detail="hand_back_types must be a list of strings")
    bad = [t for t in raw if not _HANDBACK_TYPE_RE.match(t)]
    if bad:
        raise ProfileError("profile_field_type", detail=f"hand_back_types must be kebab-case: {bad}")
    if len(set(raw)) != len(raw):
        raise ProfileError("profile_field_type", detail="hand_back_types has duplicate entries")
    universal = sorted(set(raw) & vocab.UNIVERSAL_HANDBACK_TYPES)
    if universal:
        raise ProfileError("hand_back_type_collision", detail=f"already universal hand-back types: {universal}")
    nodes = sorted(set(raw) & set(graph))
    if nodes:
        raise ProfileError("hand_back_type_collision", detail=f"hand_back_types name graph nodes: {nodes}")
    return tuple(raw)


def _build_graph(raw: object) -> dict[str, GraphNode]:
    if not isinstance(raw, dict) or not raw:
        raise ProfileError("profile_field_type", detail="graph must be a non-empty mapping")
    nodes: dict[str, GraphNode] = {}
    for node_id, raw_node in raw.items():
        if not isinstance(raw_node, dict):
            raise ProfileError("profile_field_type", detail=f"graph node {node_id!r} must be a mapping")
        unknown = set(raw_node) - _NODE_KEYS
        if unknown:
            raise ProfileError("unknown_profile_key", node=str(node_id), detail=f"unknown key(s): {sorted(unknown)}")

        kind = raw_node.get("kind")
        if kind not in vocab.STAGE_KINDS:
            raise ProfileError("unknown_node_kind", node=str(node_id), detail=f"unknown stage kind {kind!r}")

        edges_raw = raw_node.get("edges", {}) or {}
        if not isinstance(edges_raw, dict):
            raise ProfileError("profile_field_type", detail=f"{node_id}: edges must be a mapping")
        edges = {str(k): str(v) for k, v in edges_raw.items()}

        on_fail = raw_node.get("on_fail")
        if on_fail is not None and not isinstance(on_fail, str):
            raise ProfileError("profile_field_type", detail=f"{node_id}: on_fail must be a string")

        verify = raw_node.get("verify")
        if verify is not None:
            if not isinstance(verify, dict):
                raise ProfileError("profile_field_type", detail=f"{node_id}: verify must be a mapping")
            if kind == "refute-close":
                raise ProfileError(
                    "refute_close_not_agent_only",
                    node=str(node_id),
                    detail="refute-close is agent-only, may not carry a verify block",
                )
            if kind != "verify":
                raise ProfileError("profile_field_type", detail=f"{node_id}: verify block only valid on verify nodes")

        nodes[str(node_id)] = GraphNode(id=str(node_id), kind=kind, edges=edges, on_fail=on_fail, verify=verify)
    return nodes


def _build_closure(raw: object) -> Closure:
    if not isinstance(raw, dict):
        raise ProfileError("closure_block", detail="closure must be a mapping")
    missing = vocab.CLOSURE_BLOCK_REQUIRED_FIELDS - set(raw)
    if missing:
        raise ProfileError("closure_block", detail=f"closure block missing field(s): {sorted(missing)}")

    closed_values = raw["closed_values"]
    if not isinstance(closed_values, dict) or not closed_values:
        raise ProfileError("closure_block", detail="closure.closed_values must be a non-empty mapping")
    stray = set(closed_values) - vocab.CLOSURE_CLOSING_BRANCHES
    if stray:
        raise ProfileError(
            "closure_block", detail=f"closure.closed_values names unknown branch(es): {sorted(stray)}"
        )

    stamp_fields = raw["stamp_fields"]
    if not isinstance(stamp_fields, list) or not all(isinstance(f, str) for f in stamp_fields):
        raise ProfileError("closure_block", detail="closure.stamp_fields must be a list of strings")

    status_field = raw["status_field"]
    if not isinstance(status_field, str) or not status_field:
        raise ProfileError("closure_block", detail="closure.status_field must be a non-empty string")

    return Closure(
        status_field=status_field,
        closed_values={str(k): str(v) for k, v in closed_values.items()},
        stamp_fields=tuple(stamp_fields),
    )


def _build_appetite(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict) or not raw:
        raise ProfileError("profile_field_type", detail="appetite must be a non-empty mapping")
    stray = set(raw) - vocab.APPETITE_PRESETS
    if stray:
        raise ProfileError("unknown_profile_key", node="appetite", detail=f"unknown key(s): {sorted(stray)}")

    presets: dict[str, dict] = {}
    for preset_name, preset in raw.items():
        if not isinstance(preset, dict):
            raise ProfileError("profile_field_type", detail=f"appetite.{preset_name} must be a mapping")
        stray_knobs = set(preset) - vocab.KNOB_NAMES
        if stray_knobs:
            raise ProfileError(
                "unknown_profile_key", node=f"appetite.{preset_name}", detail=f"unknown key(s): {sorted(stray_knobs)}"
            )
        missing_knobs = _REQUIRED_KNOBS - set(preset)
        if missing_knobs:
            raise ProfileError("profile_field_type", detail=f"appetite.{preset_name} missing knob(s): {sorted(missing_knobs)}")
        presets[preset_name] = dict(preset)
    return presets


# ---------------------------------------------------------------------------
# validate_graph
# ---------------------------------------------------------------------------


def validate_graph(profile: Profile) -> None:
    """Refuse ``profile`` if any § Design § Profile graph rule (plus the
    EM-authored STAGE_OUTCOMES totality check) fails. Returns ``None`` on a
    clean profile; every failure is a specific exception naming the node
    and, for path-shaped rules, the path."""
    _check_outcome_membership(profile)
    _check_verify_nodes(profile)
    _check_edge_targets(profile)
    _check_acyclic(profile)
    _check_paths(profile)


#: Node kinds the composer actually routes by consulting a node's `edges`
#: map at run time (`route_after_triage`/`follow_edge`) — `commit`/`undo`
#: are terminal, dispatched by their own outcome without an edge lookup, so
#: totality/stray-outcome checking over their `edges` would check a map the
#: engine never reads.
_EDGE_ROUTED_KINDS = frozenset({"triage", "refute-close", "fix", "verify"})


def _check_outcome_membership(profile: Profile) -> None:
    for node_id, node in profile.graph.items():
        if node.kind not in _EDGE_ROUTED_KINDS:
            continue
        outcome_set = set(profile.verdicts) if node.kind == "triage" else set(vocab.STAGE_OUTCOMES[node.kind])
        stray = set(node.edges) - outcome_set
        if stray:
            raise ProfileError(
                "stray_outcome", node=node_id, detail=f"outcome(s) outside its stage kind's set: {sorted(stray)}"
            )
        missing = outcome_set - set(node.edges)
        # A missing outcome is tolerated when `on_fail` is set: `follow_edge`
        # falls back to it at run time for any outcome the `edges` map does
        # not name, so an `on_fail`-carrying node is total by construction
        # even with a sparse `edges` map. Only a node with NO `on_fail` must
        # name every one of its own outcomes explicitly.
        if missing and node.on_fail is None:
            raise ProfileError(
                "triage_verdict_totality", node=node_id, detail=f"verdict map missing edge(s) for: {sorted(missing)}"
            )


def _check_verify_nodes(profile: Profile) -> None:
    """Note (does not refuse): a verify node's ``on_fail`` is unreachable
    dead weight -- the composer's ``_verifyStage`` intercepts the ``fail``
    outcome itself (one retry to fix, then undo, then
    ``rejected-after-retry``, per DR-404 § Run authority) before
    ``followEdge`` is ever consulted, so ``on_fail`` on a verify node is
    never traversed at run time. Left un-enforced rather than refused: a
    profile author who sets it (e.g. to a hand-back type, for
    documentation/intent) is not doing anything harmful, only redundant,
    and several already-legal fixtures/tests set it deliberately."""
    for node_id, node in profile.graph.items():
        if node.kind != "verify":
            continue
        verify_map = node.verify or {}
        if "default" not in verify_map:
            raise ProfileError("verify_default_missing", node=node_id, detail="verify map has no 'default' entry")
        for key, mode in verify_map.items():
            if isinstance(mode, dict):
                if mode.get("mode") != "op" or mode.get("op") not in vocab.VERIFY_OPS:
                    raise ProfileError(
                        "verify_op_unknown", node=node_id, detail=f"[{key!r}]: invalid verify mode {mode!r}"
                    )
            elif mode not in vocab.VERIFY_MODES:
                raise ProfileError(
                    "verify_op_unknown", node=node_id, detail=f"[{key!r}]: invalid verify mode {mode!r}"
                )


def _check_edge_targets(profile: Profile) -> None:
    all_hand_back_types = vocab.UNIVERSAL_HANDBACK_TYPES | frozenset(profile.hand_back_types)
    for node_id, node in profile.graph.items():
        for outcome, target in node.edges.items():
            if target not in profile.graph and target not in all_hand_back_types:
                raise ProfileError(
                    "unknown_edge_target", node=node_id, detail=f"[{outcome!r}]: unknown edge target {target!r}"
                )
            if target in profile.graph and profile.graph[target].kind == "triage":
                raise ProfileError(
                    "edge_targets_triage_node",
                    node=node_id,
                    detail=f"[{outcome!r}]: edge targets triage node {target!r} -- the composer never "
                    "routes an edge to a triage node, it makes no progress and loops forever",
                )
        if (
            node.on_fail is not None
            and node.on_fail not in profile.graph
            and node.on_fail not in all_hand_back_types
        ):
            raise ProfileError(
                "unknown_edge_target",
                node=node_id,
                detail=f"['on_fail']: unknown edge target {node.on_fail!r}",
            )
        if node.on_fail is not None and node.on_fail in profile.graph and profile.graph[node.on_fail].kind == "triage":
            raise ProfileError(
                "edge_targets_triage_node",
                node=node_id,
                detail=f"['on_fail']: on_fail targets triage node {node.on_fail!r} -- the composer never "
                "routes on_fail to a triage node, it makes no progress and loops forever",
            )


def _check_acyclic(profile: Profile) -> None:
    """Cycle-check ``edges`` only — ``on_fail`` is the one permitted
    back-edge and is deliberately excluded from this subgraph."""
    white, gray, black = 0, 1, 2
    color = {node_id: white for node_id in profile.graph}

    def visit(node_id: str, path: list[str]) -> None:
        color[node_id] = gray
        for target in profile.graph[node_id].edges.values():
            if target not in profile.graph:
                continue  # a hand-back type target, terminal
            if color[target] == gray:
                raise ProfileError(
                    "graph_cycle", node=node_id, detail=f"non-on_fail cycle, path {path + [target]!r}"
                )
            if color[target] == white:
                visit(target, path + [target])
        color[node_id] = black

    for node_id in profile.graph:
        if color[node_id] == white:
            visit(node_id, [node_id])


def _check_paths(profile: Profile) -> None:
    starts = [node_id for node_id, node in profile.graph.items() if node.kind == "triage"]
    if not starts:
        raise ProfileError("profile_field_type", detail="graph has no triage entry node")
    for start in starts:
        _walk(
            profile,
            start,
            on_fail_used=False,
            since_fix_verified=True,
            saw_fix=False,
            saw_refute_close=False,
            path=[start],
        )


def _walk(
    profile: Profile,
    node_id: str,
    *,
    on_fail_used: bool,
    since_fix_verified: bool,
    saw_fix: bool,
    saw_refute_close: bool,
    path: list[str],
) -> None:
    node = profile.graph[node_id]

    if node.kind == "commit":
        if saw_fix and not since_fix_verified:
            raise ProfileError(
                "verify_floor", detail=f"path {path!r} reaches commit without a verify downstream of its last fix"
            )
        if not saw_fix and not saw_refute_close:
            raise ProfileError(
                "closing_floor", detail=f"path {path!r} reaches commit without refute-close or fix"
            )
        if saw_refute_close and "refute-close" not in profile.closure.closed_values:
            raise ProfileError(
                "closure_branch_missing",
                detail=f"closure.closed_values has no entry for 'refute-close' (path {path!r})",
            )
        if saw_fix and "fix" not in profile.closure.closed_values:
            raise ProfileError(
                "closure_branch_missing", detail=f"closure.closed_values has no entry for 'fix' (path {path!r})"
            )
        return

    next_saw_fix = saw_fix or node.kind == "fix"
    next_since_fix_verified = False if node.kind == "fix" else since_fix_verified
    next_saw_refute_close = saw_refute_close or node.kind == "refute-close"

    for outcome, target in node.edges.items():
        if target not in profile.graph:
            continue  # ends in a hand-back type; the path legally terminates here
        step_since_verified = next_since_fix_verified
        if node.kind == "verify" and outcome == "pass":
            step_since_verified = True
        _walk(
            profile,
            target,
            on_fail_used=on_fail_used,
            since_fix_verified=step_since_verified,
            saw_fix=next_saw_fix,
            saw_refute_close=next_saw_refute_close,
            path=path + [target],
        )

    if node.on_fail is not None and node.on_fail in profile.graph:
        if on_fail_used:
            raise ProfileError(
                "graph_on_fail_traversal", node=node_id, detail=f"second on_fail traversal on path {path!r}"
            )
        _walk(
            profile,
            node.on_fail,
            on_fail_used=True,
            since_fix_verified=next_since_fix_verified,
            saw_fix=next_saw_fix,
            saw_refute_close=next_saw_refute_close,
            path=path + [node.on_fail],
        )


# ---------------------------------------------------------------------------
# resolve_appetite
# ---------------------------------------------------------------------------


def resolve_appetite(profile: Profile, appetite: str, overrides: dict | None = None) -> dict:
    """Turn ``hunt | standard | sweep`` into concrete knobs from
    ``profile``'s preset values. Only ``vocab.OVERRIDABLE_KNOBS`` may be
    overridden. Concurrency is resolved as
    ``min(profile, vocab.ENGINE_CONCURRENCY_CEILING)`` and the resolved
    value is written into the returned knobs — no ``cpu_count`` term: the
    runtime applies its own host cap independently, the emit host is not
    necessarily the run host, and a cpu_count-dependent clamp would make
    the emitted bytes vary with the emitting host."""
    if appetite not in vocab.APPETITE_PRESETS:
        raise ProfileError("unknown_appetite_preset", detail=f"unknown appetite: {appetite!r}")
    if appetite not in profile.appetite:
        raise ProfileError(
            "unknown_appetite_preset", detail=f"profile {profile.name!r} has no {appetite!r} preset"
        )

    overrides = overrides or {}
    for knob in overrides:
        if knob not in vocab.OVERRIDABLE_KNOBS:
            raise ProfileError(
                "unoverridable_knob",
                detail=f"knob {knob!r} is not overridable (only {sorted(vocab.OVERRIDABLE_KNOBS)})",
            )

    resolved = copy.deepcopy(profile.appetite[appetite])
    resolved.update(overrides)
    if "concurrency" in resolved:
        concurrency = resolved["concurrency"]
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ProfileError("profile_field_type", detail=
                f"appetite.{appetite}.concurrency must be an int >= 1, got {concurrency!r}"
            )
        resolved["concurrency"] = min(concurrency, vocab.ENGINE_CONCURRENCY_CEILING)
    batch_size = resolved.get("batch_size")
    if isinstance(batch_size, dict) and "default" in batch_size and "@unkeyed" not in batch_size:
        # The selector's reserved fallback key is `@unkeyed` (never `default`,
        # which `_group_into_batches`/`select_rows` do not read) -- map the
        # profile author's `default` onto it so an `@unkeyed`-batched row
        # resolves the SAME batch size a named key would.
        batch_size = dict(batch_size)
        batch_size["@unkeyed"] = batch_size["default"]
        resolved["batch_size"] = batch_size
    return resolved
