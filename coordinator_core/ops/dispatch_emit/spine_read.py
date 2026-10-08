"""
coordinator_core.ops.dispatch_emit.spine_read — plan file -> normalized emitter rows.

Purpose: the single spine-reading entry point for the dispatch-emit pipeline
(docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md § C1).
Every downstream chunk (wave derivation, pathspec derivation, script
emission, op registration) consumes ``read_spine``'s ``EmitterRow`` list —
none of them re-parses a plan file.

Reuse, do not re-parse: locating and YAML-loading the `` ```yaml plan-tasks ``
fenced block is entirely delegated to
``coordinator_core.ops.plan_tasks_render.load_rows``, which itself wraps
``coordinator_core.frontmatter.body_blocks.locate_fenced_block``. This
module adds exactly two things load_rows does not do: the UNDECLARED-vs-
empty-list distinction on ``writes`` (AC2), and depends_on referent
resolution against the row-id set (AC6). It has no fenced-block or YAML
parsing code of its own.

Six fail-loud behaviours, three AC-bearing and three closing gaps the ACs
didn't name:

  1. AC2 — a row with no ``writes:`` key, or a present-but-empty value
     (``writes:`` with nothing after it, or ``writes: null``), gets the
     ``UNDECLARED`` sentinel — never ``None`` and never ``[]``.
     ``writes: []`` (declared empty) stays an empty list, a structurally
     different value from UNDECLARED so a wave-builder cannot conflate
     "writes nothing" with "unknown, treat as colliding with everything"
     (see the plan's Anti-scope: absent writes: must never be read as an
     empty set).
  2. AC6 — every ``depends_on[].chunk`` referent is resolved against the
     spine's row ``id`` set at read time. A dangling referent — a
     well-formed ``{chunk: ..., gate_kind: ...}`` edge whose ``chunk``
     names no row in this spine — raises ``DanglingDependencyError``
     naming both the row holding the edge and the unresolvable id. A
     MALFORMED edge (not an object, or an object with no ``chunk`` key —
     a bare string, a bare scalar, or a dict missing the key) is a
     DIFFERENT failure and raises ``MalformedDependencyEdgeError``
     instead, naming the offending edge value itself and the required
     shape: the edge was never looked at closely enough to know what
     chunk id it meant, so it must never be reported as chunk ``None``.
     Before this edge value ever reaches either check,
     ``schema_validate.check_plan_tasks_source`` runs as a preflight; when
     the FIRST schema-shape error it finds for this spine is under
     ``depends_on``, that error's own field/reason (e.g. an out-of-enum
     ``gate_kind``, or the exact same expected-object-got-str diagnosis)
     is what gets raised, in ``MalformedDependencyEdgeError``'s voice —
     the shipped validator's diagnosis, not a hand-rolled one. This
     referent is asserted in schema prose (plan-tasks.schema.json's
     ``depends_on`` description) but was enforced by no code anywhere in
     the fleet before this module.
  3. A scalar ``writes:``/``reads:``/``depends_on:`` value (e.g.
     ``writes: some/path.py`` instead of a one-item list) raises
     ``InvalidFieldTypeError`` naming the row and the value, rather than
     being silently coerced to a one-item list — left uncoerced, a bare
     string iterates into single characters downstream and silently
     corrupts any overlap comparison.
  4. A missing/non-string ``id``, or an ``id`` that duplicates another
     row's, raises ``InvalidRowIdError`` naming the offending row/id. Not
     an AC in the source plan, but the same failure class as points 2 and
     3 above: downstream, ``wave_map._predecessors`` keys its predecessor
     dict by ``id``, so a duplicate silently collapses two rows' edges
     into one dict entry and corrupts the wave order without raising.
     Enforced once here rather than in every id-keyed consumer.
  5. ``writes_under:`` names directory PREFIXES for a row whose output
     filenames are chosen at run time -- a dated audit, a live-run row's
     output, a write into a growing corpus. Every entry must end in ``/``
     or ``\\``; a file-shaped entry raises ``FileShapedPrefixError``
     because a file belongs in ``writes:``, and taking one here would blur
     the directory-shaped refusal that keeps the two fields apart
     (``pathspec.DirectoryShapedWriteError``). A scalar raises
     ``InvalidFieldTypeError``, as it does for ``writes:``. A row declaring
     a prefix has declared WHERE it writes, so an absent ``writes:`` on
     that row reads as ``[]``, never UNDECLARED: its files are unknown by
     name, not by location, and the epistemic-premise holdout (which keys
     on UNDECLARED) must not hold it. Source:
     state/improvement-queue/2026-09-11-dispatch-emit-takes-a-writes-under-prefi-309100e2b36b.yaml.
  6. A row carrying ``awaiting_gate`` raises ``UndeclaredGateKeyError``
     rather than being read tolerantly like the fields above.
     ``awaiting_gate`` is not a property plan-tasks.schema.json declares,
     the schema sets no ``additionalProperties: false`` to catch it, and
     the only gate this module (or coordinator-content-repo's wave-builder) ever reads
     is ``external_gate``. Read tolerantly, a row carrying it validates
     clean and dispatches exactly as though unblocked, silently discarding
     whatever cross-repo blocker the author meant to name — the dangerous
     direction, since silence reads as "not blocked" rather than as an
     error. Source:
     state/bug-backlog/2026-08-28-awaiting-gate-is-read-by-nothing-an-unde-de280708447e.yaml.

A further behaviour, not one of the fail-loud ones above but load-bearing:
``read_spine`` excludes non-dispatchable rows (closed ``disposition``
values, ``deferred: true``, and an uncleared ``external_gate`` entry that
blocks execution) from its returned list entirely, per coordinator-content-repo's
``skills/execute-plan/SKILL.md`` § Chunk-SET derivation. A row already
shipped (``disposition: coded``), explicitly deferred, or still waiting on
a cross-repo blocker must never reach the dispatch-emit pipeline and re-run
(or first-run) as a live ``coordinator:executor`` call. An
``external_gate`` entry excludes its row only when it is BOTH uncleared (no
explicit ``cleared: true`` — see ``_has_uncleared_execution_gate``) AND its
``blocks`` resolves to ``execution`` — the default when ``blocks`` is
absent, per plan-tasks.schema.json. ``blocks: ac-closure`` never excludes the row: that
gate holds only a named acceptance criterion open, not the row's execution,
and conflating the two stalls an executable chunk. ``depends_on`` referent
resolution (AC6) still runs against the FULL row-id set, before this
filter — a live row legitimately depends on a shipped/gated row, and that
edge is satisfied, not dangling. Once filtering happens, any surviving
row's ``depends_on`` edge pointing at a filtered-out row is stripped (the
edge is satisfied; leaving it would either crash
``wave_map._predecessors`` or wrongly delay the wave). An edge that
resolves to nothing at all is still a hard ``DanglingDependencyError`` —
filtering never softens that check.

Negative-spec:
  - Does NOT validate rows against the full plan-tasks schema (required
    fields, disposition cross-field rules, etc.) — that is
    ``schema_validate.py``'s surface. This module reads tolerantly for
    every field except the two AC-bearing ones it fails loudly on, plus
    the closed-disposition/deferred exclusion described above — a value
    filter, not a cross-field validation rule.
  - The ``check_plan_tasks_source`` preflight (point 2 above) does NOT
    change that: it is consulted, but only its verdict on ``depends_on``
    is ever acted on, and only when at least one raw row declares
    ``depends_on`` at all — a spine with none never pays the full
    schema+cross-field validation cost. ``check_plan_tasks_source``
    returns at most ONE error — the first row-order failure anywhere in
    the spine — so a row missing ``change_kind`` (say) ahead of the
    malformed edge in file order suppresses the depends_on diagnosis for
    that read entirely and this module falls back to its own message;
    that gap is inherent to the shared door's single-error contract, not
    something this module papers over with a second pass. The
    2026-08-06 ruling that a schema-shape violation WARNS, never BLOCKS,
    at write time (``write_guards/validate_frontmatter_schema_deny.py``)
    is UNCHANGED by this — that policy governs the write guard, not the
    emitter, and the emitter already fails loud on a malformed edge
    regardless; the preflight only improves which message it fails loud
    WITH.
  - Does NOT derive waves, pathspecs, or script text — those are C2/C3/C4.
  - Does NOT special-case ``gate_kind`` or any depends_on field beyond
    ``chunk`` — resolving the referent is this module's whole job here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple, Optional

_LOGGER = logging.getLogger(__name__)

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
from coordinator_core.ops.plan_tasks_render import RowsResult, load_rows


class _Undeclared:
    """Sentinel type for an absent (or present-but-empty) ``writes:`` field (AC2).

    A distinct type, not ``None``. This class defines no ``__bool__`` or
    ``__len__``, so ``UNDECLARED`` is TRUTHY — a loose ``if not row.writes``
    check is False for ``UNDECLARED`` and True for ``[]``. The two are NOT
    interchangeable under truthiness: a caller that needs to tell "unknown"
    apart from "declared empty" MUST use ``is UNDECLARED`` (identity). A
    caller that blindly truthiness-tests does NOT get correct behaviour
    either way — it silently reads UNDECLARED as "has writes."
    """

    def __repr__(self) -> str:  # pragma: no cover - repr aid only
        return "UNDECLARED"


UNDECLARED = _Undeclared()

NON_DISPATCHABLE_DISPOSITIONS = frozenset({"coded", "spun_off", "backlogged", "wont_do"})

#: Authored aliases for a canonical disposition. Spines written before `coded`
#: was enforced say `done`; every reader folds them where rows are read.
DISPOSITION_ALIASES = {"done": "coded"}


def with_canonical_disposition(row):
    """`row` with an aliased `disposition` rewritten to its canonical value;
    any other row (or a non-mapping) is returned as is."""
    if not isinstance(row, dict):
        return row
    alias = DISPOSITION_ALIASES.get(str(row.get("disposition") or "").strip())
    return {**row, "disposition": alias} if alias else row


# The schema's COMPLETE enum -- the closed values plus `open`. Named
# separately because the two sets answer different questions, and conflating
# them is what let an unrecognized value dispatch: membership in
# NON_DISPATCHABLE_DISPOSITIONS answers "is this row done", while membership
# here answers "is this a disposition at all".
KNOWN_DISPOSITIONS = NON_DISPATCHABLE_DISPOSITIONS | {"open"}

_GATE_BLOCKS_AC_CLOSURE = "ac-closure"
_GATE_CLOSURE_EVIDENCE_KEY = "closure_evidence"
_GATE_CLEARED_KEY = "cleared"

_UNDECLARED_GATE_KEY = "awaiting_gate"


_TERMINAL_NON_CODED = NON_DISPATCHABLE_DISPOSITIONS - {"coded"}


def _repo_root_of(plan_path) -> Optional[Path]:
    here = Path(plan_path).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _plan_status(target: Path) -> Optional[str]:
    """`target`'s frontmatter ``status``; ``None`` when the file is absent."""
    try:
        split = split_frontmatter(target.read_text(encoding="utf-8"))
    except OSError:
        return None
    if split is None:
        return ""
    try:
        doc = load_frontmatter_doc(split.fm_text)
    except yaml.YAMLError:
        return ""
    return str(doc.get("status") or "") if isinstance(doc, dict) else ""


def _status_edge_hold(owner: str, label: str, wanted: str, actual: Optional[str]) -> Optional[str]:
    """Hold detail for a ``{plan, status}`` edge; ``None`` once ``actual`` has
    reached ``wanted`` along the plan lifecycle. A plan that ended
    ``abandoned``/``superseded`` can never reach it."""
    from coordinator_core.roadmap.plan_gate import _STATUS_LIFECYCLE_ORDER as order

    if actual is None:
        raise DanglingPlanDependencyError(f"{owner} depends_on_plan {label}: plan is absent")
    if wanted not in order:
        raise MalformedDependencyEdgeError(
            f"{owner} depends_on_plan {label}: status {wanted!r} is not a plan status"
        )
    if actual in ("abandoned", "superseded"):
        raise DanglingPlanDependencyError(
            f"{owner} depends_on_plan {label}: plan is {actual}, never {wanted}"
        )
    if actual in order and actual != "deferred" and order.index(actual) >= order.index(wanted):
        return None
    return f"depends_on_plan {label}: plan status is {actual or 'unset'}, not yet {wanted}"


def _unlanded_plan_edge(
    raw: dict, plan_path, repo_root: Optional[Path], plan_cache: dict
) -> Optional[str]:
    """The first ``depends_on_plan`` edge of ``raw`` not yet satisfied, as a
    detail string; ``None`` when every edge is satisfied or the row declares
    none. An edge is ``{plan, chunk}`` (named row ``coded``) or ``{plan, status}``
    (plan reached that lifecycle status). Raises ``DanglingPlanDependencyError``
    for an edge that can never be satisfied. With no repo root to resolve
    against, an edge cannot be shown satisfied and withholds the row."""
    return unlanded_plan_edges(
        f"row {raw.get('id')!r}", raw.get("depends_on_plan"), repo_root, plan_cache
    )


class PlanEdgeResolution(NamedTuple):
    """``named_row`` the predecessor row (canonical disposition) of a chunk edge,
    None for a status edge; ``hold`` the detail while the edge is unsatisfied,
    None once satisfied."""

    named_row: Optional[dict]
    hold: Optional[str]


def resolve_plan_edge(
    owner: str, edge, repo_root: Optional[Path], plan_cache: dict
) -> PlanEdgeResolution:
    """One ``depends_on_plan`` edge: raises ``MalformedDependencyEdgeError`` or
    ``DanglingPlanDependencyError`` for a bad edge, else reports hold state."""
    if (
        not isinstance(edge, dict)
        or not edge.get("plan")
        or bool(edge.get("chunk")) == bool(edge.get("status"))
    ):
        raise MalformedDependencyEdgeError(
            f"{owner} depends_on_plan entry {edge!r} must be "
            "{plan: <repo-relative .md path>, chunk: <row id> | status: <plan status>, "
            "gate_kind: <kind>}"
        )
    rel = str(edge["plan"]).replace("\\", "/")
    chunk = edge.get("chunk")
    label = f"{rel} {chunk or edge['status']}"
    if ".." in rel.split("/") or rel.startswith("/"):
        raise DanglingPlanDependencyError(
            f"{owner} depends_on_plan {label}: path escapes the repo"
        )
    if repo_root is None:
        return PlanEdgeResolution(
            None, f"depends_on_plan {label}: no repo root to resolve it against"
        )
    from coordinator_core.roadmap.plan_gate import archived_plan_path

    target = repo_root / rel
    archived = False
    if not target.is_file():
        moved = archived_plan_path(repo_root, rel)
        if moved is not None:
            target, archived = moved, True
    if not chunk:
        actual = _plan_status(target)
        if archived and actual not in ("abandoned", "superseded"):
            actual = str(edge["status"])
        return PlanEdgeResolution(None, _status_edge_hold(owner, label, str(edge["status"]), actual))
    if target not in plan_cache:
        try:
            loaded = load_rows(target.read_text(encoding="utf-8"))
        except OSError:
            plan_cache[target] = None
        else:
            plan_cache[target] = (
                {
                    r["id"]: with_canonical_disposition(r)
                    for r in loaded.rows
                    if isinstance(r, dict) and r.get("id")
                }
                if loaded.status is LocateStatus.LOCATED
                else None
            )
    rows_by_id = plan_cache[target]
    if archived:
        if _plan_status(target) in ("abandoned", "superseded"):
            raise DanglingPlanDependencyError(
                f"{owner} depends_on_plan {label}: archived plan is {_plan_status(target)}, never coded"
            )
        return PlanEdgeResolution((rows_by_id or {}).get(chunk), None)
    if rows_by_id is None:
        raise DanglingPlanDependencyError(
            f"{owner} depends_on_plan {label}: plan is absent or has no spine"
        )
    named = rows_by_id.get(chunk)
    if named is None:
        raise DanglingPlanDependencyError(
            f"{owner} depends_on_plan {label}: no such row in that plan"
        )
    disposition = named.get("disposition")
    if disposition in _TERMINAL_NON_CODED:
        raise DanglingPlanDependencyError(
            f"{owner} depends_on_plan {label}: predecessor is {disposition}, "
            "never coded"
        )
    hold = (
        None
        if disposition == "coded"
        else f"depends_on_plan {label}: predecessor not yet coded"
    )
    return PlanEdgeResolution(named, hold)


def unlanded_plan_edges(
    owner: str, edges, repo_root: Optional[Path], plan_cache: dict
) -> Optional[str]:
    """``_unlanded_plan_edge``'s body over any edge list, ``owner`` naming whose
    it is (a row, or the plan itself for frontmatter edges)."""
    if not edges:
        return None
    if not isinstance(edges, list):
        raise MalformedDependencyEdgeError(f"{owner} depends_on_plan is {edges!r}, not a list")
    held: Optional[str] = None
    for edge in edges:
        hold = resolve_plan_edge(owner, edge, repo_root, plan_cache).hold
        held = held or hold
    return held


def _is_operator_row(raw: dict) -> bool:
    return raw.get("execution_mode") == "operator"


_MEMO_SEND_BRIEF_RE = re.compile(r"\bcross-repo-memo(?:\.py)?\s+send\b|\bmemo\.send\b")


def _is_memo_send_row(raw: dict) -> bool:
    """True when the row's deliverable is a cross-repo memo send.

    A subagent cannot send one (``block-subagent-destructive-action``), so
    the row is an EM step. Decided by ``kind``, by a surface or write that is
    a staged memo-outbox draft, or by the brief naming the send verb.
    """
    if raw.get("kind") in ("memo-send", "memo_send"):
        return True
    paths = [raw.get("surface"), *(raw.get("writes") or ())]
    if any(isinstance(p, str) and "outbox" in p for p in paths):
        from coordinator_core.ops.fleet.memo_wire import memo_outbox_topic

        if any(isinstance(p, str) and memo_outbox_topic(p) for p in paths):
            return True
    brief = raw.get("brief") or raw.get("body")
    return isinstance(brief, str) and _MEMO_SEND_BRIEF_RE.search(brief) is not None


def _has_uncleared_execution_gate(raw: dict, extra_gates: tuple = ()) -> bool:
    """True if `raw`'s row-level ``external_gate`` (plus any ``extra_gates``
    resolved onto this row from plan frontmatter — see
    ``_frontmatter_external_gates``) carries an entry that is both uncleared
    (no explicit ``cleared: true``) and blocks ``execution`` (the default
    when ``blocks`` is absent or None).

    Two shapes now count, both from claude-klabauter#43 — a gate declared
    where this reader used to look past it, so the row dispatched as though
    open:

    - A row-level ``external_gate`` entry that is a plain STRING rather than
      a mapping (``external_gate: [some-gate-id]``). A string cannot carry
      ``cleared``/``blocks`` at all, so there is no way for it to declare
      itself cleared or ac-closure-scoped — it always counts as an uncleared
      EXECUTION gate, unconditionally.
    - A frontmatter-level ``external_gate`` entry carrying ``row: <id>``,
      resolved by ``_frontmatter_external_gates`` and threaded in here as
      ``extra_gates`` for the one row it names. Once resolved onto the row,
      it is evaluated exactly like a native row-level mapping entry
      (``cleared``/``blocks`` read the same way).

    ``blocks: ac-closure`` entries never count here — that gate holds only
    a named acceptance criterion open, not the row's execution.

    A gate is cleared ONLY by an explicit ``cleared: true`` — the ONE
    clearing path. ``closure_evidence`` is purely descriptive: it names how
    a gate was or will be verified, and never by itself clears anything,
    however truthy its content, and a ``cleared: false`` (or any value other
    than the literal ``true``) leaves the gate exactly as uncleared as no
    ``cleared`` key at all. A ``closure_evidence`` is typically authored at
    plan-writing time, before the evidence has arrived, so its natural
    content is a description of what is still being AWAITED — treating its
    mere presence as clearing therefore self-cleared the exact gates it was
    meant to describe. This is the retired half of the joint two-repo bump
    (see the schema's own x-bump-note: 1.9.0 landed only the additive
    ``cleared: false`` override as a first step; presence-as-cleared was
    deferred pending this widening, matched on the same schedule against
    coordinator-content-repo's ``_uncleared_execution_gate``).

    Before ``cleared`` was read at all, an author who wrote a status note
    into ``closure_evidence`` and wanted to say "not actually discharged"
    had no way to say so — the schema promised a ``cleared: false`` override
    the reader did not yet implement, and any truthy ``closure_evidence``
    silently disarmed the gate regardless. Found against sat-06 C4, a row
    that writes into a sibling repo's tree, where the disarm would have
    scheduled a cross-tree write on a DR that is still `status: proposed`.
    ``cleared: false`` has been honoured since that fix, and this bump
    subsumes it: both now fall out of the single ``cleared is True`` check.

    NOT tolerant of a malformed ``external_gate`` shape (klabauter#43) —
    the two common authoring mistakes both dispatch a row its own plan
    gated if read tolerantly:

      1. ``external_gate`` present but not a list — e.g. a single gate
         object authored directly instead of wrapped in a one-item list.
         Reading it as "no gate" and returning ``False`` dispatches the row.
      2. A list entry that is not a dict — e.g. a bare string gate note.
         Skipping it and continuing the scan silently drops the one gate
         that was actually declared; an entry that never resolves to a
         mapping can neither be checked for ``cleared: true`` nor for
         ``blocks``, so it can never be shown to be safe.

    Both shapes now GATE the row (return ``True``) rather than being
    skipped: an unparseable or unrecognized declared gate must never read
    as proceed — the same class of defect ``UnknownDispositionError``
    refuses loud on above, a value the reader could not positively confirm
    was safe being treated as though it had. A well-formed
    uncleared-execution entry elsewhere in the same (partially malformed)
    list still excludes the row regardless — malformed neighbors never mask
    a real gate.

    Absence is unaffected: no ``external_gate`` key at all, or an explicit
    ``external_gate: null``, still means no gate was declared and returns
    ``False`` — only a PRESENT-but-malformed value gates.
    """
    own_gate = raw.get("external_gate")
    if own_gate is None and not extra_gates:
        return False
    entries: list = []
    if own_gate is not None:
        if not isinstance(own_gate, list):
            return True
        entries.extend(own_gate)
    entries.extend(extra_gates)
    for entry in entries:
        if not isinstance(entry, dict):
            return True
        if entry.get(_GATE_CLEARED_KEY) is True:
            continue
        if entry.get("blocks") != _GATE_BLOCKS_AC_CLOSURE:
            return True
    return False


class SpineReadError(ValueError):
    """Raised when the plan's `` ```yaml plan-tasks `` block cannot be read.

    Covers every non-LOCATED outcome from ``load_rows`` (ABSENT, MALFORMED)
    — this module does not attempt to emit against a spine it could not
    locate or parse.
    """


class UnknownDispositionError(SpineReadError):
    """Raised when a row's ``disposition`` is not a value the schema defines.

    The exclusion filter tests membership in
    ``NON_DISPATCHABLE_DISPOSITIONS``, so before this check every
    unrecognized value read as dispatchable by falling through it. That is a
    fail-OPEN on exactly the authoring mistake a reconciliation pass invites:
    an operator marking finished rows reaches for a plausible word (`done`,
    `complete`, `shipped`), the schema rejects it, but nothing on the emit
    path validates and the row dispatches anyway. Observed outcome
    (example-retrieval-repo-ue-addon, 2026-08-20): a wave map covering already-executed
    chunks -- a well-intentioned reconciliation landing in the same place as
    no reconciliation at all, and worse than a stale-but-honest `open`
    because its author believes it was handled.

    Refusing is deliberate, over treating an unknown value as
    non-dispatchable. Silently excluding fails CLOSED -- safe for this run,
    but it drops real work with no signal, and an author who typoed one row
    would get a quietly narrower wave instead of a correction. The refusal
    names the row, the value, and the legal set, so the fix is readable off
    the error rather than out of the schema.
    """


class DanglingDependencyError(SpineReadError):
    """Raised when a ``depends_on[].chunk`` referent has no matching row id (AC6).

    Reserved for a WELL-FORMED edge (an object carrying a ``chunk`` key)
    whose value genuinely resolves to nothing in this spine's row-id set.
    A malformed edge — not an object, or an object with no ``chunk`` key
    at all — is never routed here; see ``MalformedDependencyEdgeError``.

    Extends ``SpineReadError`` (not bare ``ValueError``) so a caller that
    catches ``SpineReadError`` around ``read_spine`` — e.g.
    ``plan_assemble.predicates.composition_graph``'s ``chunk_overlap`` and
    ``path_rename_or_move``, which degrade to ``undetermined(...)`` on any
    unreadable spine — degrades gracefully on a dangling edge exactly as it
    already does on an absent/malformed spine block, instead of raising
    uncaught. A dangling referent is still a real authoring defect (AC6
    keeps raising it), but it is a spine-content defect the same class as
    every other one this module fails loud on, not a reason to crash a
    predicate that has no way to fix the plan file it is reading.
    """


class MalformedDependencyEdgeError(SpineReadError):
    pass


class DanglingPlanDependencyError(SpineReadError):
    """A ``depends_on_plan`` edge whose predecessor can never land: the plan
    file is absent (moved terminal), escapes the repo, the named row is not in
    its spine, or the row reached a terminal non-``coded`` disposition. Waiting
    on it would withhold the dependent row forever."""


class InvalidFieldTypeError(SpineReadError):
    pass


class UndeclaredGateKeyError(SpineReadError):
    pass


class InvalidRowIdError(SpineReadError):
    """Raised when a spine row's ``id`` is missing, non-string, or a
    duplicate of another row's ``id`` in the same spine.

    ``wave_map.
    _predecessors`` keys its predecessor dict by ``id``; a duplicate or
    missing id silently collapses two rows into one dict entry rather than
    raising, corrupting the predecessor graph and producing a wrong wave
    order with no error at all. ``spine_read`` is where every other
    row-shape guarantee (AC2 UNDECLARED, AC6 dangling depends_on, scalar
    writes:/reads:) is already established and enforced once for every
    downstream consumer, so id presence/uniqueness belongs here rather than
    as an ad hoc check duplicated in ``wave_map`` (and any future consumer
    of ``read_spine``'s output).
    """


class FileShapedPrefixError(SpineReadError):
    pass


class AmbiguousExternalGateError(SpineReadError):
    pass


class ContradictoryReadDeclarationError(InvalidFieldTypeError):
    """Raised when a row names the same path in both ``reads_at_head:``
    (never orders) and ``consumes:``/``reads:`` (orders) — D5.

    A subclass of ``InvalidFieldTypeError`` (the module's existing
    field-type refusal), not a bare ``SpineReadError``: the two declarations
    disagree about whether the same path orders this row, and there is no
    way to pick a side that would not silently discard the author's other
    declaration.
    """


class EmitterRow(NamedTuple):
    """One normalized task-spine row for the dispatch-emit pipeline.

    ``agent_type``/``agent_model`` (state/sizings/2026-09-05-a-plan-row-can-
    name-the-agent-that-runs.yaml; plan-tasks.schema.json 1.13.0) are the
    optional per-row overrides ``wave_map.build_waves`` reads off this row
    via ``getattr`` into ``WaveRow`` and ``emit.py``'s ``_row_agent_type``/
    ``_model_opt`` resolve at emit time. Read tolerantly here, matching
    every field on this row but the four fail-loud ones (module docstring):
    a malformed value is `emit.py`'s ``MalformedAgentOverrideError`` to
    raise, not this module's — re-checking the grammar here would be a
    second place the pattern could drift from the vendored schema's own
    ``pattern``. Both default to ``None``, and a spine declaring neither key
    carries both as ``None`` here exactly as before this field existed.

    ``body`` is carried for exactly one reader: ``emit.py``'s
    ``_row_agent_type`` asks whether a row's verification has to RUN
    something, which decides whether the agent it derives can do the row at
    all. Nothing here interprets it; it defaults to ``""`` like the other
    tolerant fields.

    ``verification_runs`` is the DECLARED answer to that same question, and
    it wins over the body when present. The author knows whether their
    verification has to run something; ``emit.py``'s
    ``_verification_requires_a_run`` can only guess it from the words they
    happened to use, and a phrasebook classifier gets better with every
    phrase it learns and never becomes correct. ``None`` means the row
    declared nothing and the prose fallback answers, which is every row
    written before this key existed.

    ``change_kind`` rides the same tolerant read for the same reason: it is
    what tells ``emit.py`` a row's WORK class, which the write path alone
    cannot answer. A ``verification`` row writing only an immutable plan body
    is un-routable — the executor is the plan-body guard's sole block target,
    the enricher's charter forbids execution-tier work — and without this
    field ``_row_agent_type`` cannot see the contradiction, so the row emits,
    dispatches, and burns a wave. Defaults to ``None``; a spine declaring no
    ``change_kind`` maps exactly as before the field existed.

    ``reads`` is the ORDERING read set (§ Design D5): the union of a row's
    declared ``consumes:`` entries and any ``reads:`` entries (``reads:`` is
    the pre-D5 spelling of the same ordering meaning, kept for back-compat
    and logged once per ``read_spine`` call — see ``read_spine``).
    ``wave_map.build_waves`` orders on this field alone and needs no change.

    ``reads_at_head`` is a SEPARATE, non-ordering read set: a path a row
    reads for its verdict at the plan's base revision, never at another
    row's write. It never contributes a wave edge. Defaults to ``()`` so
    coordinator-content-repo's ``emit-dispatch-workflow.py``, which calls ``read_spine``
    and does not know this field, is unaffected by its addition.
    """

    id: str
    title: str
    surface: str
    writes: object  # list[str], or the UNDECLARED sentinel
    reads: list
    depends_on: list
    agent_type: Optional[str] = None
    agent_model: Optional[str] = None
    body: str = ""
    writes_under: tuple = ()
    verification_runs: Optional[bool] = None
    change_kind: Optional[str] = None
    reads_at_head: tuple = ()


_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_MEMO_LIMIT = 256
_FM_DOCS: dict = {}
_ROWS_MEMO: dict = {}


def _remember(memo: dict, key: str, value):
    if len(memo) >= _MEMO_LIMIT:
        memo.clear()
    memo[key] = value
    return value


def load_frontmatter_doc(fm_text: str):
    """``yaml.safe_load(fm_text)`` memoised per process by text; raises ``yaml.YAMLError``.

    The returned document is shared: callers must treat it as read-only.
    """
    try:
        return _FM_DOCS[fm_text]
    except KeyError:
        return _remember(_FM_DOCS, fm_text, yaml.load(fm_text, Loader=_YAML_LOADER))


def _parse_rows(source: str) -> RowsResult:
    """``plan_tasks_render.load_rows`` with libyaml: the same LOCATED/ABSENT/MALFORMED
    contract, ~10x cheaper on a real spine (pure-Python ``safe_load`` was ~95% of the
    prep gate's cost on a large plan)."""
    located = locate_fenced_block(source)
    if located.status is not LocateStatus.LOCATED:
        return RowsResult(status=located.status, rows=[])
    try:
        rows = yaml.load(located.body, Loader=_YAML_LOADER) or []
    except yaml.YAMLError:
        return RowsResult(status=LocateStatus.MALFORMED, rows=[])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return RowsResult(status=LocateStatus.MALFORMED, rows=[])
    return RowsResult(status=LocateStatus.LOCATED, rows=rows)


def load_rows_memo(source: str):
    """Spine rows of `source`, parsed once per process per text; the result is read-only."""
    hit = _ROWS_MEMO.get(source)
    if hit is None:
        hit = _remember(_ROWS_MEMO, source, _parse_rows(source))
    return hit


def _frontmatter_external_gates(source: str, row_ids: set) -> dict:
    split = split_frontmatter(source)
    if split is None:
        return {}
    try:
        doc = load_frontmatter_doc(split.fm_text)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    gate_list = doc.get("external_gate")
    if gate_list is None:
        return {}
    if not isinstance(gate_list, list):
        raise AmbiguousExternalGateError(
            f"plan frontmatter external_gate is {gate_list!r}, not a list "
            "of gate entries"
        )
    by_row: dict = {}
    for entry in gate_list:
        if not isinstance(entry, dict):
            raise AmbiguousExternalGateError(
                f"plan frontmatter external_gate entry {entry!r} is not a "
                "mapping; a frontmatter gate entry must be a mapping "
                "carrying row: <row id>"
            )
        row_id = entry.get("row")
        if row_id is None:
            continue
        if not isinstance(row_id, str) or row_id not in row_ids:
            raise AmbiguousExternalGateError(
                f"plan frontmatter external_gate entry {entry.get('id', entry)!r} "
                f"declares row: {row_id!r}, which does not match any row id "
                "in this plan's task spine"
            )
        by_row.setdefault(row_id, []).append(entry)
    return by_row


def frontmatter_plan_edges(source: str):
    """The plan's own frontmatter ``depends_on_plan`` list (an execution
    precondition on the whole plan), or ``None`` when it declares none."""
    split = split_frontmatter(source)
    if split is None:
        return None
    try:
        doc = load_frontmatter_doc(split.fm_text)
    except yaml.YAMLError:
        return None
    return doc.get("depends_on_plan") if isinstance(doc, dict) else None


def coded_row_ids(plan_path) -> frozenset:
    """Ids of the rows whose canonical ``disposition`` is ``coded``."""
    with open(plan_path, encoding="utf-8") as handle:
        result = load_rows_memo(handle.read())
    if result.status is not LocateStatus.LOCATED:
        raise SpineReadError(
            f"plan {plan_path!r} task-spine block is {result.status.name}, not LOCATED"
        )
    return frozenset(
        raw["id"]
        for raw in map(with_canonical_disposition, result.rows)
        if isinstance(raw, dict) and raw.get("disposition") == "coded" and isinstance(raw.get("id"), str)
    )


def contradictory_read_paths(reads, reads_at_head) -> list:
    """Sorted paths a row names in both the ordering set (``reads:`` /
    ``consumes:``) and ``reads_at_head:``. Non-list or unhashable input yields
    no overlap; type errors stay ``read_spine``'s to raise. The one predicate
    behind ``ContradictoryReadDeclarationError`` and the pre-emit lints."""
    try:
        return sorted(set(reads or ()) & set(reads_at_head or ()))
    except TypeError:
        return []


def read_spine(
    plan_path, exclusions: Optional[list] = None, keep_coded: frozenset = frozenset()
) -> list[EmitterRow]:
    """Read `plan_path`'s task-spine and return normalized ``EmitterRow`` objects.

    ``keep_coded`` names ``coded`` rows to return as rows anyway (a review of
    hand-landed work needs the row the dispatch would otherwise skip).

    Raises ``SpineReadError`` if the spine block is absent or malformed,
    ``InvalidRowIdError`` if any row's ``id`` is missing/non-string or
    duplicates another row's id, ``MalformedDependencyEdgeError`` if any
    ``depends_on`` entry is not an object with a ``chunk`` key,
    ``DanglingDependencyError`` if a well-formed ``depends_on[].chunk``
    does not resolve against the spine's row-id set (AC6),
    ``InvalidFieldTypeError`` if ``writes:``/``reads:``/``depends_on:`` is
    declared as a non-list value rather than a list, and
    ``UndeclaredGateKeyError`` if any row carries ``awaiting_gate`` (module
    docstring point 6).
    ``writes`` is UNDECLARED (AC2) on any row that omits the key or
    declares it present-but-empty; ``reads`` and ``depends_on`` both
    default to ``[]`` when omitted (neither carries an undeclared-vs-empty
    distinction per the schema — only ``writes`` does, per
    plan-tasks.schema.json).

    ``reads`` (§ Design D5) is the union of declared ``reads:`` and
    ``consumes:`` — both order. ``reads_at_head`` defaults to ``()`` and
    never orders. A row naming the same path in ``reads_at_head:`` and in
    ``reads:``/``consumes:`` raises ``ContradictoryReadDeclarationError``.
    Any row using the (deprecated but still-honoured) ``reads:`` key logs
    one ``logging.warning`` per ``read_spine`` call naming every such row.

    Rows whose ``disposition`` is closed (see
    ``NON_DISPATCHABLE_DISPOSITIONS``), whose ``deferred`` is ``true``, which
    carry an uncleared ``external_gate`` entry blocking ``execution`` (see
    ``_has_uncleared_execution_gate``), or which declare
    ``execution_mode: operator`` (see ``_is_operator_row``), or which name a
    ``depends_on_plan`` row not yet ``coded`` (``_unlanded_plan_edge``; a
    predecessor that can never land raises ``DanglingPlanDependencyError``) are
    excluded from the returned list — they are not dispatchable.

    ``exclusions``, when a list is passed, is APPENDED with one
    ``{"id", "reason", "detail"}`` dict per row this function drops. It is an
    out-parameter rather than a changed return type so no existing caller
    moves, and it re-reads nothing — the classification already happens here.

    Passing it is how a caller avoids a SILENT SKIP, and for
    ``execution_mode: operator`` that is not optional in spirit. A row a human
    must run, dropped without a word, is strictly worse than the mis-dispatch
    this field exists to prevent: a mis-dispatched row at least reports that
    it could not proceed, whereas a vanished one leaves a plan that reads
    fully executed with a step nobody performed. The same argument applies to
    the gate and deferral exclusions that predate this parameter, which is why
    it reports all of them rather than only the new one. An ``external_gate`` entry with
    ``blocks: ac-closure`` does NOT exclude its row — that row executes
    normally; only a named acceptance criterion stays open. depends_on
    referent resolution runs against the full row-id set before this
    exclusion, so an edge onto an excluded row never dangles.

    The two exclusion reasons are NOT interchangeable past that point. A
    closed disposition row's work is done, so a live
    row's edge onto it is satisfied — that edge is stripped from the
    surviving row's ``depends_on`` and the surviving row is otherwise
    unaffected. An uncleared execution-blocking gate is the opposite: the
    gated row's work has NOT run, so a live row's edge onto it is not
    satisfied — that row, and every row transitively depending on it, is
    itself excluded rather than edge-stripped. A row excluded for both
    reasons (closed disposition AND its own uncleared gate) resolves as
    satisfied: its work shipped, so the stale gate is bookkeeping, not a
    live blocker. A ``deferred: true`` row's work did NOT ship: a row
    depending on it, transitively, is withheld and reported with reason
    ``withheld_by_deferred_dependency``.

    (Review: staff-eng, Finding 12) The same stop-at-satisfied rule also
    governs the PASS-THROUGH case: a satisfied row's own predecessor may
    itself be gated (excluded, not satisfied), but propagation deliberately
    stops at the satisfied row rather than laundering that block onto the
    satisfied row's own dependents — its work has demonstrably already run,
    so a stale upstream gate is not their blocker either.
    """
    with open(plan_path, encoding="utf-8") as handle:
        source = handle.read()

    result = load_rows_memo(source)
    if result.status is not LocateStatus.LOCATED:
        raise SpineReadError(
            f"plan {plan_path!r} task-spine block is {result.status.name}, not LOCATED"
        )

    raw_rows = [with_canonical_disposition(raw) for raw in result.rows]

    if any(isinstance(raw, dict) and raw.get("depends_on") for raw in raw_rows):
        schema_error = check_plan_tasks_source(source)
        if schema_error is not None and schema_error["field"].startswith("depends_on"):
            raise MalformedDependencyEdgeError(
                f"plan {plan_path!r} spine field {schema_error['field']}: "
                f"{schema_error['error']} ({schema_error['hint']})"
            )

    seen_ids: set[str] = set()
    for raw in raw_rows:
        row_id = raw.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise InvalidRowIdError(f"row {raw!r} has a missing or non-string id")
        if row_id in seen_ids:
            raise InvalidRowIdError(f"duplicate row id {row_id!r} in spine")
        seen_ids.add(row_id)
        if _UNDECLARED_GATE_KEY in raw:
            raise UndeclaredGateKeyError(
                f"row {row_id!r} declares {_UNDECLARED_GATE_KEY!r}, which "
                "plan-tasks.schema.json does not define and no reader in "
                "this pipeline consults; the row would dispatch exactly as "
                "though unblocked. Use external_gate to declare a cross-repo "
                "blocker instead."
            )

    row_ids = seen_ids

    frontmatter_gates = _frontmatter_external_gates(source, row_ids)

    rows: list[EmitterRow] = []
    rows_using_reads: list[str] = []
    for raw in raw_rows:
        row_id = raw.get("id")
        writes = raw.get("writes")
        if writes is None:
            # Absent key AND present-but-empty value (`writes:` with no
            # scalar/list, or `writes: null`) both collapse to UNDECLARED —
            # AC2 admits exactly two states, never a third (see module
            # docstring point 1).
            writes = UNDECLARED
        elif not isinstance(writes, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares writes: as {writes!r}, not a list"
            )
        writes_under = raw.get("writes_under")
        if writes_under is None:
            writes_under = ()
        elif not isinstance(writes_under, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares writes_under: as {writes_under!r}, not a list"
            )
        else:
            for prefix in writes_under:
                if not isinstance(prefix, str) or not prefix.endswith(("/", "\\")):
                    raise FileShapedPrefixError(
                        f"row {row_id!r} declares writes_under: entry {prefix!r}, "
                        "which does not end in a path separator. A single file "
                        "belongs in `writes:`."
                    )
            writes_under = tuple(prefix.replace("\\", "/") for prefix in writes_under)
            if writes is UNDECLARED:
                writes = []
        declared_reads = raw.get("reads")
        if declared_reads is None:
            declared_reads = []
        elif not isinstance(declared_reads, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares reads: as {declared_reads!r}, not a list"
            )
        else:
            rows_using_reads.append(row_id)

        consumes = raw.get("consumes")
        if consumes is None:
            consumes = []
        elif not isinstance(consumes, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares consumes: as {consumes!r}, not a list"
            )

        reads_at_head = raw.get("reads_at_head")
        if reads_at_head is None:
            reads_at_head = ()
        elif not isinstance(reads_at_head, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares reads_at_head: as {reads_at_head!r}, not a list"
            )
        else:
            reads_at_head = tuple(reads_at_head)

        # reads is the ordering set: declared `reads:` union `consumes:`.
        reads = list(dict.fromkeys([*declared_reads, *consumes]))

        overlap = contradictory_read_paths(reads, reads_at_head)
        if overlap:
            raise ContradictoryReadDeclarationError(
                f"row {row_id!r} declares {overlap!r} in both "
                "reads_at_head: (never orders) and consumes:/reads: (orders); "
                "a path cannot be both"
            )

        depends_on = raw.get("depends_on")
        if depends_on is None:
            depends_on = []
        elif not isinstance(depends_on, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares depends_on: as {depends_on!r}, not a list"
            )

        for edge in depends_on:
            if not isinstance(edge, dict) or "chunk" not in edge:
                raise MalformedDependencyEdgeError(
                    f"row {row_id!r} depends_on entry {edge!r} is not an "
                    "object with a chunk key; depends_on entries must be "
                    "shaped {chunk: <row id>, gate_kind: <kind>[, note: ...]}"
                )
            chunk = edge["chunk"]
            if chunk not in row_ids:
                raise DanglingDependencyError(
                    f"row {row_id!r} depends_on unresolvable chunk {chunk!r}"
                )

        rows.append(
            EmitterRow(
                id=row_id,
                title=raw.get("title", ""),
                surface=raw.get("surface", ""),
                writes=writes,
                reads=reads,
                depends_on=depends_on,
                agent_type=raw.get("agent_type"),
                agent_model=raw.get("agent_model"),
                body=raw.get("body") or "",
                writes_under=writes_under,
                verification_runs=(
                    raw["verification_runs"]
                    if isinstance(raw.get("verification_runs"), bool)
                    else None
                ),
                change_kind=raw.get("change_kind"),
                reads_at_head=reads_at_head,
            )
        )

    if rows_using_reads:
        _LOGGER.warning(
            "read_spine: row(s) %s use the deprecated reads: key; reads: "
            "still orders (kept for back-compat), but new rows should use "
            "consumes: (orders) or reads_at_head: (never orders) instead",
            ", ".join(rows_using_reads),
        )

    satisfied_ids: set[str] = set()
    blocked_ids: set[str] = set()
    deferred_ids: set[str] = set()
    plan_edge_root: Optional[Path] = None
    plan_edge_cache: dict = {}
    plan_level_edges = frontmatter_plan_edges(source)
    if plan_level_edges or any(
        isinstance(raw, dict) and raw.get("depends_on_plan") for raw in raw_rows
    ):
        plan_edge_root = _repo_root_of(plan_path)
    plan_level_hold = unlanded_plan_edges(
        "plan frontmatter", plan_level_edges, plan_edge_root, plan_edge_cache
    )
    for raw in raw_rows:
        disposition = raw.get("disposition")
        deferred = raw.get("deferred") is True or bool(raw.get("deferred_until"))
        em_performed = raw.get("performer") == "em" or _is_memo_send_row(raw)
        plan_hold = None
        if not (
            disposition in NON_DISPATCHABLE_DISPOSITIONS or deferred is True or em_performed
        ):
            plan_hold = plan_level_hold or _unlanded_plan_edge(
                raw, plan_path, plan_edge_root, plan_edge_cache
            )
        if disposition is not None and disposition not in KNOWN_DISPOSITIONS:
            raise UnknownDispositionError(
                f"row {raw.get('id')!r} has disposition {disposition!r}, which is "
                "not a value plan-tasks.schema.json defines: refusing rather than "
                "treating it as dispatchable (legal values: "
                f"{', '.join(sorted(KNOWN_DISPOSITIONS))}). A row that shipped in "
                "a commit is 'coded'."
            )
        if exclusions is not None:
            _reason = None
            if disposition in NON_DISPATCHABLE_DISPOSITIONS:
                _reason = ("disposition", "disposition: %s" % disposition)
            elif _has_uncleared_execution_gate(raw, tuple(frontmatter_gates.get(raw.get("id"), ()))):
                # Ahead of deferred/em-performed/operator: a gated row of any
                # mode must reach the gate ledger (StageManifest.gated).
                _reason = ("external_gate", "uncleared external_gate blocking execution")
            elif deferred is True:
                _reason = (
                    "deferred",
                    "deferred: true" if raw.get("deferred") is True else "deferred_until hold",
                )
            elif em_performed:
                _reason = (
                    "em-performed",
                    "performer: em"
                    if raw.get("performer") == "em"
                    else "EM STEP: cross-repo memo send -- a subagent cannot "
                    "send it; the EM runs `cross-repo-memo send` after this run",
                )
            elif _is_operator_row(raw):
                _reason = (
                    "operator",
                    "execution_mode: operator — a human must run this row; it "
                    "was NOT dispatched and has NOT been done",
                )
            elif plan_hold is not None:
                _reason = ("depends_on_plan", plan_hold)
            if _reason is not None:
                exclusions.append(
                    {"id": raw.get("id"), "reason": _reason[0], "detail": _reason[1]}
                )

        if disposition in NON_DISPATCHABLE_DISPOSITIONS or em_performed:
            satisfied_ids.add(raw.get("id"))
        elif deferred is True:
            deferred_ids.add(raw.get("id"))
            blocked_ids.add(raw.get("id"))
        elif (
            _has_uncleared_execution_gate(raw, tuple(frontmatter_gates.get(raw.get("id"), ())))
            or _is_operator_row(raw)
            or plan_hold is not None
        ):
            blocked_ids.add(raw.get("id"))

    dependents: dict[str, list[str]] = {}
    for row in rows:
        for edge in row.depends_on:
            if not isinstance(edge, dict):
                continue
            chunk = edge.get("chunk")
            if isinstance(chunk, str) and chunk:
                dependents.setdefault(chunk, []).append(row.id)

    # Restated from coordinator-content-repo emit-dispatch-workflow.py's
    # `_transitive_gate_closure` (commit aee52a6a5): a row excluded here not
    # because it carries a gate/operator mode itself, but because it
    # `depends_on`, directly or through a chain, a row this loop already
    # blocked, gets NO `exclusions` entry from the classification loop above
    # -- that loop runs once, before this propagation, and only ever sees the
    # row's own fields. Left unreported, such a row is silently absent from
    # both the dispatched output AND the exclusions ledger, indistinguishable
    # from a row that never existed. `transitive_root` records, for every
    # row blocked ONLY by this propagation, the direct predecessor edge that
    # blocked it, so the appended entry can name the full chain back to the
    # row that actually carries the gate/operator mode.
    transitive_root: dict[str, str] = {}
    frontier = list(blocked_ids)
    while frontier:
        current = frontier.pop()
        for dependent_id in dependents.get(current, ()):
            if dependent_id in satisfied_ids or dependent_id in blocked_ids:
                continue
            blocked_ids.add(dependent_id)
            transitive_root[dependent_id] = current
            frontier.append(dependent_id)

    if exclusions is not None and transitive_root:
        already_reported = {entry.get("id") for entry in exclusions}
        for row_id in transitive_root:
            if row_id in already_reported:
                continue
            chain = [row_id]
            cur = row_id
            while cur in transitive_root:
                cur = transitive_root[cur]
                chain.append(cur)
            if chain[-1] in deferred_ids:
                exclusions.append(
                    {
                        "id": row_id,
                        "reason": "withheld_by_deferred_dependency",
                        "detail": (
                            "withheld: depends_on -> "
                            + " -> ".join(chain[1:])
                            + f"; root {chain[-1]} is deferred, not delivered"
                        ),
                    }
                )
                continue
            exclusions.append(
                {
                    "id": row_id,
                    "reason": "transitive_gate_closure",
                    "detail": (
                        "transitively gated via depends_on -> "
                        + " -> ".join(chain[1:])
                        + f"; root {chain[-1]} carries the uncleared gate/operator mode"
                    ),
                }
            )

    satisfied_ids -= keep_coded
    excluded_ids = satisfied_ids | blocked_ids
    dispatchable_rows = [row for row in rows if row.id not in excluded_ids]
    for i, row in enumerate(dispatchable_rows):
        if not row.depends_on:
            continue
        stripped = [
            edge
            for edge in row.depends_on
            if not (isinstance(edge, dict) and edge.get("chunk") in satisfied_ids)
        ]
        if len(stripped) != len(row.depends_on):
            dispatchable_rows[i] = row._replace(depends_on=stripped)

    return dispatchable_rows


def executable_body(title: str, body: str) -> bool:
    words = _body_words(body)
    return bool(words) and words != _body_words(title)


def _body_words(text: str) -> "list[str]":
    return re.findall(r"[a-z0-9]+", (text or "").lower())
