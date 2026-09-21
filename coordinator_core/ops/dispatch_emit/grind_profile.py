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
    per node the engine bounds to one traversal per path;
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
    }
)

_REQUIRED_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS - {"absent_sentinels"}

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
    source_path: Path


# ---------------------------------------------------------------------------
# Exceptions — each names the offending node/path, never a bare ValueError.
# ---------------------------------------------------------------------------


class InvalidProfileNameError(ValueError):
    """Raised when a profile ``name`` fails ``^[a-z][a-z0-9-]*$`` or contains
    a path separator — the name is joined onto ``profile_dir`` verbatim, so
    an unvalidated name is a path-traversal surface."""


class UnknownProfileKeyError(ValueError):
    """Raised when a profile (or one of its nested blocks) declares a
    top-level/knob/node key this module does not recognise."""

    def __init__(self, keys: list[str], *, node: str | None = None) -> None:
        where = f" in {node}" if node else ""
        super().__init__(f"unknown key(s){where}: {keys}")
        self.keys = keys
        self.node = node


class ProfileFieldTypeError(ValueError):
    """Raised when a field this module reads has the wrong shape or is
    missing where required."""


class UnknownNodeKindError(ValueError):
    """Raised when a graph node's ``kind`` is not a member of
    ``vocab.STAGE_KINDS``."""

    def __init__(self, node_id: str, kind: object) -> None:
        super().__init__(f"{node_id}: unknown stage kind {kind!r}")
        self.node_id = node_id
        self.kind = kind


class RefuteCloseNotAgentOnlyError(ValueError):
    """Raised when a ``refute-close`` node declares a ``verify`` block.
    ``refute-close`` is agent-only (§ Design § Profile) — it never routes
    through the op-runner path a ``verify`` block would name."""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"{node_id}: refute-close is agent-only, may not carry a verify block")
        self.node_id = node_id


class VerifyDefaultMissingError(ValueError):
    """Raised when a ``verify``-kind node's ``verify`` map has no ``default``
    entry."""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"{node_id}: verify map has no 'default' entry")
        self.node_id = node_id


class VerifyOpUnknownError(ValueError):
    """Raised when a ``verify``-kind node names an op-mode entry that is not
    a member of ``vocab.VERIFY_OPS``, or a bare mode outside
    ``vocab.VERIFY_MODES``."""

    def __init__(self, node_id: str, key: str, mode: object) -> None:
        super().__init__(f"{node_id}[{key!r}]: invalid verify mode {mode!r}")
        self.node_id = node_id
        self.key = key
        self.mode = mode


class UnknownEdgeTargetError(ValueError):
    """Raised when an edge (or ``on_fail``) names a target that is neither
    another graph node nor a member of ``vocab.UNIVERSAL_HANDBACK_TYPES``."""

    def __init__(self, node_id: str, outcome: str, target: object) -> None:
        super().__init__(f"{node_id}[{outcome!r}]: unknown edge target {target!r}")
        self.node_id = node_id
        self.outcome = outcome
        self.target = target


class StrayOutcomeError(ValueError):
    """Raised when a node's edges name an outcome outside its own stage
    kind's outcome set (``vocab.STAGE_OUTCOMES[node.kind]``, or the
    profile's declared ``verdicts`` for a ``triage`` node)."""

    def __init__(self, node_id: str, stray: list[str]) -> None:
        super().__init__(f"{node_id}: outcome(s) outside its stage kind's set: {stray}")
        self.node_id = node_id
        self.stray = stray


class TriageVerdictTotalityError(ValueError):
    """Raised when a ``triage`` node's edges do not cover every verdict the
    profile declares (§ Design § Profile: "every node's verdict map is
    total over the profile's verdict vocabulary")."""

    def __init__(self, node_id: str, missing: list[str]) -> None:
        super().__init__(f"{node_id}: verdict map missing edge(s) for: {missing}")
        self.node_id = node_id
        self.missing = missing


class GraphCycleError(ValueError):
    """Raised when the graph carries a cycle through plain ``edges`` (never
    through ``on_fail``, which is the one permitted back-edge)."""

    def __init__(self, node_id: str, path: list[str]) -> None:
        super().__init__(f"{node_id}: non-on_fail cycle, path {path!r}")
        self.node_id = node_id
        self.path = path


class GraphOnFailTraversalError(ValueError):
    """Raised when a single path attempts a second ``on_fail`` traversal —
    the engine bounds every path to exactly one."""

    def __init__(self, node_id: str, path: list[str]) -> None:
        super().__init__(f"{node_id}: second on_fail traversal on path {path!r}")
        self.node_id = node_id
        self.path = path


class VerifyFloorError(ValueError):
    """Raised when a path reaches a commit after a ``fix`` with no
    ``verify`` pass downstream of the LAST fix on that path."""

    def __init__(self, path: list[str]) -> None:
        super().__init__(f"path {path!r} reaches commit without a verify downstream of its last fix")
        self.path = path


class ClosingFloorError(ValueError):
    """Raised when a path reaches a commit having passed through neither
    ``refute-close`` nor ``fix`` — no closing path may bypass both."""

    def __init__(self, path: list[str]) -> None:
        super().__init__(f"path {path!r} reaches commit without refute-close or fix")
        self.path = path


class ClosureBlockError(ValueError):
    """Raised when the profile's ``closure`` block is missing a required
    field or names an unknown closing branch."""


class ClosureBranchMissingError(ValueError):
    """Raised when the graph can reach a commit via a closing branch
    (``fix`` or ``refute-close``) that ``closure.closed_values`` does not
    map (EM note 3)."""

    def __init__(self, branch: str, path: list[str]) -> None:
        super().__init__(f"closure.closed_values has no entry for {branch!r} (path {path!r})")
        self.branch = branch
        self.path = path


class UnknownAppetitePresetError(ValueError):
    """Raised when ``resolve_appetite`` is asked for an appetite the
    vocabulary does not know, or the profile itself never declared."""


class UnoverridableKnobError(ValueError):
    """Raised when ``resolve_appetite`` receives an override for a knob
    outside ``vocab.OVERRIDABLE_KNOBS``."""

    def __init__(self, knob: str) -> None:
        super().__init__(f"knob {knob!r} is not overridable (only {sorted(vocab.OVERRIDABLE_KNOBS)})")
        self.knob = knob


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
        raise InvalidProfileNameError(f"invalid profile name: {name!r}")

    path = Path(profile_dir) / f"{name}.yaml"
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ProfileFieldTypeError("profile document must be a mapping")

    unknown = set(doc) - _TOP_LEVEL_KEYS
    if unknown:
        raise UnknownProfileKeyError(sorted(unknown))
    missing = _REQUIRED_TOP_LEVEL_KEYS - set(doc)
    if missing:
        raise ProfileFieldTypeError(f"missing required top-level key(s): {sorted(missing)}")

    row_id_key = doc["row_id_key"]
    if not isinstance(row_id_key, str) or not row_id_key:
        raise ProfileFieldTypeError("row_id_key must be a non-empty string")

    batch_key = doc["batch_key"]
    if not isinstance(batch_key, list) or not batch_key or not all(isinstance(k, str) for k in batch_key):
        raise ProfileFieldTypeError("batch_key must be a non-empty list of strings")

    priority = doc["priority"]
    if (
        not isinstance(priority, dict)
        or not isinstance(priority.get("field"), str)
        or priority.get("order") not in ("asc", "desc")
    ):
        raise ProfileFieldTypeError("priority must be {field: str, order: 'asc'|'desc'}")

    verdicts = doc["verdicts"]
    if not isinstance(verdicts, list) or not verdicts or not all(isinstance(v, str) for v in verdicts):
        raise ProfileFieldTypeError("verdicts must be a non-empty list of strings")

    graph = _build_graph(doc["graph"])
    closure = _build_closure(doc["closure"])
    appetite = _build_appetite(doc["appetite"])

    absent_sentinels_raw = doc.get("absent_sentinels", {}) or {}
    if not isinstance(absent_sentinels_raw, dict):
        raise ProfileFieldTypeError("absent_sentinels must be a mapping")
    absent_sentinels = {}
    for key, values in absent_sentinels_raw.items():
        if not isinstance(values, list):
            raise ProfileFieldTypeError(f"absent_sentinels[{key!r}] must be a list")
        absent_sentinels[key] = tuple(values)

    triage_policy = doc["triage_policy"]
    if not isinstance(triage_policy, str) or not triage_policy:
        raise ProfileFieldTypeError("triage_policy must be a non-empty string")
    digest = hashlib.sha256(triage_policy.encode("utf-8")).hexdigest()

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
        source_path=path,
    )


def _build_graph(raw: object) -> dict[str, GraphNode]:
    if not isinstance(raw, dict) or not raw:
        raise ProfileFieldTypeError("graph must be a non-empty mapping")
    nodes: dict[str, GraphNode] = {}
    for node_id, raw_node in raw.items():
        if not isinstance(raw_node, dict):
            raise ProfileFieldTypeError(f"graph node {node_id!r} must be a mapping")
        unknown = set(raw_node) - _NODE_KEYS
        if unknown:
            raise UnknownProfileKeyError(sorted(unknown), node=str(node_id))

        kind = raw_node.get("kind")
        if kind not in vocab.STAGE_KINDS:
            raise UnknownNodeKindError(str(node_id), kind)

        edges_raw = raw_node.get("edges", {}) or {}
        if not isinstance(edges_raw, dict):
            raise ProfileFieldTypeError(f"{node_id}: edges must be a mapping")
        edges = {str(k): str(v) for k, v in edges_raw.items()}

        on_fail = raw_node.get("on_fail")
        if on_fail is not None and not isinstance(on_fail, str):
            raise ProfileFieldTypeError(f"{node_id}: on_fail must be a string")

        verify = raw_node.get("verify")
        if verify is not None:
            if not isinstance(verify, dict):
                raise ProfileFieldTypeError(f"{node_id}: verify must be a mapping")
            if kind == "refute-close":
                raise RefuteCloseNotAgentOnlyError(str(node_id))
            if kind != "verify":
                raise ProfileFieldTypeError(f"{node_id}: verify block only valid on verify nodes")

        nodes[str(node_id)] = GraphNode(id=str(node_id), kind=kind, edges=edges, on_fail=on_fail, verify=verify)
    return nodes


def _build_closure(raw: object) -> Closure:
    if not isinstance(raw, dict):
        raise ClosureBlockError("closure must be a mapping")
    missing = vocab.CLOSURE_BLOCK_REQUIRED_FIELDS - set(raw)
    if missing:
        raise ClosureBlockError(f"closure block missing field(s): {sorted(missing)}")

    closed_values = raw["closed_values"]
    if not isinstance(closed_values, dict) or not closed_values:
        raise ClosureBlockError("closure.closed_values must be a non-empty mapping")
    stray = set(closed_values) - vocab.CLOSURE_CLOSING_BRANCHES
    if stray:
        raise ClosureBlockError(f"closure.closed_values names unknown branch(es): {sorted(stray)}")

    stamp_fields = raw["stamp_fields"]
    if not isinstance(stamp_fields, list) or not all(isinstance(f, str) for f in stamp_fields):
        raise ClosureBlockError("closure.stamp_fields must be a list of strings")

    status_field = raw["status_field"]
    if not isinstance(status_field, str) or not status_field:
        raise ClosureBlockError("closure.status_field must be a non-empty string")

    return Closure(
        status_field=status_field,
        closed_values={str(k): str(v) for k, v in closed_values.items()},
        stamp_fields=tuple(stamp_fields),
    )


def _build_appetite(raw: object) -> dict[str, dict]:
    if not isinstance(raw, dict) or not raw:
        raise ProfileFieldTypeError("appetite must be a non-empty mapping")
    stray = set(raw) - vocab.APPETITE_PRESETS
    if stray:
        raise UnknownProfileKeyError(sorted(stray), node="appetite")

    presets: dict[str, dict] = {}
    for preset_name, preset in raw.items():
        if not isinstance(preset, dict):
            raise ProfileFieldTypeError(f"appetite.{preset_name} must be a mapping")
        stray_knobs = set(preset) - vocab.KNOB_NAMES
        if stray_knobs:
            raise UnknownProfileKeyError(sorted(stray_knobs), node=f"appetite.{preset_name}")
        missing_knobs = _REQUIRED_KNOBS - set(preset)
        if missing_knobs:
            raise ProfileFieldTypeError(f"appetite.{preset_name} missing knob(s): {sorted(missing_knobs)}")
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


def _check_outcome_membership(profile: Profile) -> None:
    for node_id, node in profile.graph.items():
        outcome_set = set(profile.verdicts) if node.kind == "triage" else set(vocab.STAGE_OUTCOMES[node.kind])
        stray = set(node.edges) - outcome_set
        if stray:
            raise StrayOutcomeError(node_id, sorted(stray))
        if node.kind == "triage":
            missing = outcome_set - set(node.edges)
            if missing:
                raise TriageVerdictTotalityError(node_id, sorted(missing))


def _check_verify_nodes(profile: Profile) -> None:
    for node_id, node in profile.graph.items():
        if node.kind != "verify":
            continue
        verify_map = node.verify or {}
        if "default" not in verify_map:
            raise VerifyDefaultMissingError(node_id)
        for key, mode in verify_map.items():
            if isinstance(mode, dict):
                if mode.get("mode") != "op" or mode.get("op") not in vocab.VERIFY_OPS:
                    raise VerifyOpUnknownError(node_id, key, mode)
            elif mode not in vocab.VERIFY_MODES:
                raise VerifyOpUnknownError(node_id, key, mode)


def _check_edge_targets(profile: Profile) -> None:
    for node_id, node in profile.graph.items():
        for outcome, target in node.edges.items():
            if target not in profile.graph and target not in vocab.UNIVERSAL_HANDBACK_TYPES:
                raise UnknownEdgeTargetError(node_id, outcome, target)
        if node.on_fail is not None and node.on_fail not in profile.graph:
            raise UnknownEdgeTargetError(node_id, "on_fail", node.on_fail)


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
                raise GraphCycleError(node_id, path + [target])
            if color[target] == white:
                visit(target, path + [target])
        color[node_id] = black

    for node_id in profile.graph:
        if color[node_id] == white:
            visit(node_id, [node_id])


def _check_paths(profile: Profile) -> None:
    starts = [node_id for node_id, node in profile.graph.items() if node.kind == "triage"]
    if not starts:
        raise ProfileFieldTypeError("graph has no triage entry node")
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
            raise VerifyFloorError(path)
        if not saw_fix and not saw_refute_close:
            raise ClosingFloorError(path)
        if saw_refute_close and "refute-close" not in profile.closure.closed_values:
            raise ClosureBranchMissingError("refute-close", path)
        if saw_fix and "fix" not in profile.closure.closed_values:
            raise ClosureBranchMissingError("fix", path)
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

    if node.on_fail is not None:
        if on_fail_used:
            raise GraphOnFailTraversalError(node_id, path)
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
    value is written into the returned knobs — no ``cpu_count`` term
    (overengineering-reviewer #5): the runtime applies its own host cap
    independently, the emit host is not necessarily the run host, and a
    cpu_count-dependent clamp would make the emitted bytes vary with the
    emitting host, which re-emit determinism does not want."""
    if appetite not in vocab.APPETITE_PRESETS:
        raise UnknownAppetitePresetError(f"unknown appetite: {appetite!r}")
    if appetite not in profile.appetite:
        raise UnknownAppetitePresetError(f"profile {profile.name!r} has no {appetite!r} preset")

    overrides = overrides or {}
    for knob in overrides:
        if knob not in vocab.OVERRIDABLE_KNOBS:
            raise UnoverridableKnobError(knob)

    resolved = copy.deepcopy(profile.appetite[appetite])
    resolved.update(overrides)
    if "concurrency" in resolved:
        resolved["concurrency"] = min(resolved["concurrency"], vocab.ENGINE_CONCURRENCY_CEILING)
    return resolved
