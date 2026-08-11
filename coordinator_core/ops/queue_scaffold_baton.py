"""
coordinator_core.ops.queue_scaffold_baton — JSON-RPC "handoff.scaffold_from_queue" operation.

Purpose: MUTATING engine primitive that turns a queue-triage selection (solo entry or
themed cluster) into a real ``state/handoffs/*.md`` baton, carrying queue-family and
class context forward instead of leaving the EM to hand-author one. This is the
op-proposes half of the queue-triage terminus (DR-047 boundary: claude-klabauter owns the op that
produces/transforms structured state; the sibling ceremony that disposes the source rows
— stamping ``closed_by`` — is EM-side and NOT this op's job).

Op key is ``handoff.scaffold_from_queue``, NOT ``queue.scaffold_baton`` — this op writes
``state/handoffs/``, which is outside DR-213's five named queue subdirs
(``docs/decisions/DR-213-queue-write-substrate-carveout.md`` § D2(v)); D4/Invariant-3 forbid a
handler operating under different semantics from inheriting the ``queue.*`` carve-out. The
module FILENAME stays ``queue_scaffold_baton.py`` by convention; only the op KEY carries
admission semantics.

Accepted input shapes
----------------------
Exactly one of ``params["entry"]`` (solo) or ``params["cluster"]`` (themed) must be supplied:

- solo:   ``entry``   = ``{"id": str, "path": str, "title": str}`` (id/title optional,
          derived from ``path`` when absent).
- themed: ``cluster``  = ``{"signal": str, "value": str, "suggestedLabel": str,
          "items": [{"id": str, "path": str, "title": str}, ...]}`` — the exact envelope
          ``queue.cluster`` (``coordinator_core.ops.queue_cluster``) returns.

Each item's queue-family is derived from ``Path(item["path"]).parent.name`` and validated
against ``coordinator_core.ops.queue_family.normalize_family`` — the ONE family map this
module reuses (no second family ladder is authored here). Class context (the family's
schema-declared required-plus-optional fields beyond the common boilerplate — e.g.
``severity``, ``risk``, ``change_kind`` — read via
``coordinator_core.ops.queue_family.fields_for_family``, generically, never via an
``if family == ...`` branch) is loaded through ``queue_family.load_family_records`` (itself
delegating to ``records_query.query_records`` — no glob, no frontmatter parse, no record
loader authored here) and rendered into the new baton's body for human legibility. A
missing class field on a matched record is normal input, not an error (read-defensively
convention).

Ambiguity handling
------------------
``origin_plan_id`` (scalar) and ``origin_goal_id`` (list) auto-resolve via
``coordinator_core.ops.match_core.resolve_candidate`` when the caller omits them — the
SAME score-load-bearing decision ``handoff_author_fork.py`` uses, not the raw candidate
COUNT of the scanned directory: a repo with 230 plans and a repo with 1 plan both auto-
resolve when the query clearly names one of them, and both refuse to when it doesn't (a
lone candidate that fails the score floor is NOT auto-resolved just for being alone).
Zero candidates resolves to ``null`` silently (graceful-absent); a top candidate that
clears neither the score floor nor the runner-up gap returns ``needs_disambiguation``
instead of guessing.

Response envelope — RATIFIED 2026-07-23 by claude-central-em
--------------------------------------------------------------
(``cross-repo/inbox/2026-07-23-claude-central-em-queue-terminus-envelopes-four-additions.md``;
see also ``docs/plans/2026-07-23-queue-triage-terminus-ops.md`` § Ratified response
envelopes.) Mirror this shape EXACTLY — a near-miss variant is worse than either extreme.

Success::

    {
        "status":       "ok",
        "handoff_path": str,               # absolute path to the new handoff file
        "handoff_id":   str,               # filename stem
        "category":     "queue-derived-baton",   # literal — written into frontmatter
                                                  # directly, never inherited as "infra"
        "source_entries": [{"id": str, "path": str}, ...],  # echoes the queue rows this
                                                             # baton was derived from, so
                                                             # the caller can stamp
                                                             # closed_by without an
                                                             # out-of-band consumed-set
    }

``needs_disambiguation`` — mirrors ``handoff_author_fork.py`` (``_handler``, success/
disambiguation section) LITERALLY: same key names, same nesting, same scored-candidates
shape::

    {
        "status":     "needs_disambiguation",
        "candidates": {
            "plans": [{"plan_id": str, "title": str, "score": float}, ...],
            "goals": [{"goal_id": str, "title": str, "score": float}, ...],
        },
    }

Error::

    {"exit_code": 1, "error": str}

Empty result is an empty list in every case, never null.

Write mechanics
----------------
Frontmatter written DIRECTLY via ``coordinator_core.frontmatter.primitives`` (no
``coordinator-doc-new`` shell-out — this bypass is the answer already sent to
claude-central-em on the emitter-category question). New file created under
``{worktree}/state/handoffs/`` via ``locked_rmw(missing_ok=True)`` (atomic mkstemp +
os.replace under flock) — never a bare ``open()``. ``origin_*`` provenance auto-fills from
live session state on the ``handoff_author_fork.py`` pattern (this module reuses that
module's ``_resolve_origin_handoff``/``_fork_handoff_filename`` helpers directly — not a
reimplementation).

MUTATING op: writes ONLY ``state/handoffs/`` in the caller's worktree. No git commit from
the handler. Blocking FS I/O wrapped in ``asyncio.to_thread``.

``_OP_KEY_SCOPE: common_dir`` (assigned by the registration chunk, not this module) —
handler receives ``git_common_dir(caller_worktree)`` via ipc.py; derives worktree via
``main_worktree_root(repo_root)`` before any path construction.

Spec backlink: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C5

Negative-spec:
    - Does NOT close, delete, or mutate any source queue row — op-proposes only; the
      caller's EM disposes (stamps ``closed_by``) using the echoed ``source_entries``.
    - Does NOT shell out to ``coordinator-doc-new``.
    - Does NOT author a directory glob, a frontmatter parser, or a record loader — all
      record loading routes through ``queue_family.load_family_records`` /
      ``records_query.query_records``.
    - Does NOT mint a second queue-family map, an ``if family == ...`` ladder, or a
      theme/subsystem frontmatter field.
    - Does NOT inherit ``category: infra`` from a generic scaffolding default — the
      literal ``queue-derived-baton`` is written directly and also returned in the
      envelope so the caller can assert on it.
    - Does NOT surface plan and goal ambiguity simultaneously (mirrors
      ``handoff_author_fork.py``'s ordering: plan resolved before goal).
    - Does NOT write outside ``state/handoffs/`` in the caller's worktree.
    - Does NOT overwrite an existing handoff file — the generated filename is
      timestamp-keyed + uuid-suffixed; a collision triggers ``MutateAbort``
      (mirrors ``handoff_author_fork.py``'s identical guard).
    - Does NOT treat "exactly one candidate" as sufficient to auto-resolve on its
      own — a lone plan/goal candidate still has to clear
      ``match_core.AUTO_RESOLVE_MIN_SCORE`` (mirrors ``resolve_candidate``'s own
      negative-spec).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.frontmatter.body_blocks import _compile_heading_re
from coordinator_core.frontmatter.primitives import insert_fm_field, serialize_yaml_scalar
from coordinator_core.handoff_creation_guard import (
    HandoffArchivedTwinError,
    assert_no_archived_twin,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.goals_match import _collect_goals
from coordinator_core.ops.handoff_author_fork import (
    _append_fm_array_field,
    _fork_handoff_filename,
    _resolve_origin_handoff,
)
from coordinator_core.ops.handoff_normalize import (
    _NO_FRONTMATTER,
    _normalize_one_text,
    _resolve_claimed_plan_deliverable_id,
)
from coordinator_core.ops.match_core import ResolutionReason, resolve_candidate
from coordinator_core.ops.plan_match import _collect_plans
from coordinator_core.ops.queue_family import (
    UnknownQueueFamilyError,
    fields_for_family,
    load_family_records,
    normalize_family,
)
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session_ledger import SESSION_LEDGER_BLOCK_LINES

_LOG = logging.getLogger(__name__)

CATEGORY_QUEUE_DERIVED_BATON = "queue-derived-baton"
"""Literal frontmatter ``category`` value this op always stamps — never inherited."""


# ---------------------------------------------------------------------------
# Input-shape normalization — solo entry / themed cluster -> one flat item list
# ---------------------------------------------------------------------------


def _normalize_item(raw: dict) -> Dict[str, str]:
    """Normalize one queue-row item to ``{"id", "path", "title"}`` (all str).

    ``path`` is the only truly required key on a raw item; ``id``/``title`` are
    derived from ``path`` (stem) when absent — the same read-defensively posture as
    the rest of this queue-triage surface.
    """
    path = str(raw.get("path") or "").strip()
    raw_id = str(raw.get("id") or "").strip()
    item_id = raw_id or (Path(path).stem if path else "")
    title = str(raw.get("title") or "").strip() or item_id or path
    return {"id": item_id, "path": path, "title": title}


def _extract_items(params: dict) -> "tuple[Optional[List[Dict[str, str]]], Optional[str], Optional[dict]]":
    """Extract a flat, normalized item list plus a default title from ``params``.

    Returns ``(items, default_title, error_dict)`` — exactly one of the first two is
    non-``None`` when ``error_dict`` is ``None``. ``error_dict`` (an ``_err(...)``
    shape) is returned when neither ``entry`` nor ``cluster`` is supplied, or both are.
    """
    entry = params.get("entry")
    cluster = params.get("cluster")
    entry_supplied = "entry" in params
    cluster_supplied = "cluster" in params
    if entry_supplied and cluster_supplied:
        return None, None, _err("supply exactly one of 'entry' or 'cluster', not both")
    if entry_supplied:
        if not isinstance(entry, dict):
            return None, None, _err("'entry' must be an object")
        item = _normalize_item(entry)
        if not item["path"]:
            return None, None, _err("'entry.path' is required")
        return [item], item["title"], None
    if cluster_supplied:
        if not isinstance(cluster, dict):
            return None, None, _err("'cluster' must be an object")
        raw_items = cluster.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return None, None, _err("'cluster.items' must be a non-empty array")
        items = [_normalize_item(raw) for raw in raw_items]
        for item in items:
            if not item["path"]:
                return None, None, _err("every 'cluster.items[]' entry requires 'path'")
        default_title = str(cluster.get("suggestedLabel") or "").strip() or None
        return items, default_title, None
    return None, None, _err("supply one of 'entry' (solo) or 'cluster' (themed)")


# ---------------------------------------------------------------------------
# Queue-family / class-context resolution — reuses queue_family's ONE family map
# ---------------------------------------------------------------------------


def _family_for_path(path: str) -> str:
    """Derive the queue-family directory name from a repo-relative queue-row path."""
    return Path(path).parent.name


_BOILERPLATE_FIELDS = frozenset({"created", "title", "body", "status"})
"""Fields every queue-family record already carries, redundant in a rendered
class-context section (the item line already shows id/path/title)."""


def _class_context_for_items(
    items: List[Dict[str, str]], worktree_root: Path
) -> Dict[str, Dict[str, object]]:
    """Load each item's underlying record and pull its family's optional class fields.

    Groups items by family, loads each family ONCE via
    ``queue_family.load_family_records``, then looks up each item's record by
    filename match. Returns ``{path: {field_name: value, ...}}`` for every item whose
    record was found and carried at least one populated optional field; an item with
    no match or no populated optional fields is simply absent from the result (normal
    input, not an error).
    """
    by_family: Dict[str, List[Dict[str, str]]] = {}
    for item in items:
        family = _family_for_path(item["path"])
        by_family.setdefault(family, []).append(item)

    result: Dict[str, Dict[str, object]] = {}
    for family, family_items in by_family.items():
        try:
            normalize_family(family)
        except UnknownQueueFamilyError:
            continue
        records = load_family_records(family, worktree_root)
        by_name = {Path(rec["path"]).name: rec.get("frontmatter") or {} for rec in records}
        field_table = fields_for_family(family)
        class_fields = tuple(
            f for f in (*field_table["required"], *field_table["optional"])
            if f not in _BOILERPLATE_FIELDS
        )
        for item in family_items:
            fm = by_name.get(Path(item["path"]).name)
            if fm is None:
                continue
            populated = {f: fm[f] for f in class_fields if fm.get(f) not in (None, "", [])}
            if populated:
                result[item["path"]] = populated
    return result


def _render_source_entries_body(
    items: List[Dict[str, str]], class_context: Dict[str, Dict[str, object]]
) -> str:
    """Render a markdown "## Source entries" section carrying family + class context."""
    lines = ["## Source entries", ""]
    for item in items:
        family = _family_for_path(item["path"])
        lines.append(f"- [{family}] {item['id']} ({item['path']}) — {item['title']}")
        context = class_context.get(item["path"])
        if context:
            for field_name, value in context.items():
                lines.append(f"  - {field_name}: {value}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Frontmatter construction
# ---------------------------------------------------------------------------


def _build_baton_frontmatter(
    title: str,
    created_date: str,
    branch: str,
    kind: str,
    workstream: Optional[str],
    provenance: Dict[str, object],
    body: str,
) -> str:
    """Build the initial frontmatter string for a queue-derived baton.

    Mirrors ``handoff_author_fork._build_fork_frontmatter`` in shape, with two
    differences: ``category`` is stamped directly as the literal
    ``queue-derived-baton`` (never left for ``_normalize_one_text`` to backfill —
    that helper only fills a MISSING category, so writing it up front is what keeps
    it from ever regressing to the ``infra`` default), and ``origin_goal_id`` uses the
    same array-serialization convention.
    """
    fm = f"title: {serialize_yaml_scalar(title)}\n"
    fm += f"created: {created_date}\n"
    fm += f"branch: {serialize_yaml_scalar(branch)}\n"
    fm += "status: open\n"
    fm += "predecessor: none\n"
    fm += f"kind: {serialize_yaml_scalar(kind)}\n"
    fm += f"category: {serialize_yaml_scalar(CATEGORY_QUEUE_DERIVED_BATON)}\n"
    if workstream is not None:
        fm += f"workstream: {serialize_yaml_scalar(workstream)}\n"

    for logical_name in ("origin_session", "origin_handoff", "origin_handoff_id", "origin_plan_id"):
        value = provenance.get(logical_name)
        fm = insert_fm_field(fm, logical_name, value)
    # Review: code-reviewer — reuse handoff_author_fork's array-serialization
    # helper instead of hand-rolling the null/[]/[a, b] three-way branch, so
    # the two modules can't silently drift on this convention.
    fm = _append_fm_array_field(fm, "origin_goal_id", provenance.get("origin_goal_id"))

    full_text = "---\n" + fm.rstrip("\n") + "\n---\n"
    if body:
        full_text += "\n" + body.lstrip("\n")
    return full_text


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.scaffold_from_queue")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC ``handoff.scaffold_from_queue`` handler — see module docstring for the
    ratified response envelope and accepted input shapes.

    Required params (exactly one):
        entry   (dict)  — solo shape ``{"id", "path", "title"}``.
        cluster (dict)  — themed shape (``queue.cluster``'s output envelope).

    Optional params:
        title          (str)       — overrides the auto-derived title (cluster's
                                     ``suggestedLabel`` or the solo entry's title).
        branch         (str)       — branch context; default "none".
        kind           (str)       — handoff kind; default "spinoff" (schema-valid;
                                     matches the ``predecessor: none`` invariant).
        workstream     (str | null) — workstream slug; default null.
        body           (str)       — extra body text appended after the auto-rendered
                                     source-entries section.
        origin_plan_id (str | null) — explicit plan id; absent → auto-resolve or disambiguate.
        origin_goal_id (list | null) — explicit goal id list; absent → auto-resolve or disambiguate.
        match_text     (str)       — text used for candidate ranking when disambiguating;
                                     defaults to the resolved title when absent.
    """
    items, default_title, err = _extract_items(params)
    if err is not None:
        return err
    assert items is not None  # for type-checkers; _extract_items guarantees this on no-error

    title: str = (params.get("title") or default_title or "").strip()
    if not title:
        return _err("no title supplied and none derivable from 'entry'/'cluster'")

    branch: str = (params.get("branch") or "none").strip()
    kind: str = (params.get("kind") or "spinoff").strip()
    workstream: Optional[str] = params.get("workstream") or None
    extra_body: str = params.get("body") or ""

    plan_id_supplied = "origin_plan_id" in params
    origin_plan_id_raw = params.get("origin_plan_id")

    goal_id_supplied = "origin_goal_id" in params
    origin_goal_id_raw = params.get("origin_goal_id")

    match_text: str = (params.get("match_text") or title).strip()

    if repo_root is None:
        return _err(
            "no repo_root resolved — _origin_worktree required for handoff.scaffold_from_queue"
        )
    worktree_root = main_worktree_root(repo_root)

    origin_session = resolve_current_session_id(worktree_root)

    handoffs_dir = worktree_root / "state" / "handoffs"
    try:
        origin_handoff, origin_handoff_id = await asyncio.to_thread(
            _resolve_origin_handoff, handoffs_dir, origin_session
        )
    except OSError as exc:
        return _err(f"cannot enumerate {handoffs_dir} to resolve origin_handoff: {exc}")

    origin_plan_id: Optional[str]
    if plan_id_supplied:
        origin_plan_id = origin_plan_id_raw if origin_plan_id_raw else None
    else:
        plans_dir = worktree_root / "docs" / "plans"
        plan_items = await asyncio.to_thread(_collect_plans, plans_dir)
        plan_resolution = resolve_candidate(match_text, plan_items)
        origin_plan_id = plan_resolution["resolved_id"]
        plan_reason = plan_resolution["reason"]
        if plan_reason is not None and plan_reason != ResolutionReason.NO_CANDIDATES:
            return {
                "status": "needs_disambiguation",
                "candidates": {
                    "plans": [
                        {"plan_id": e["id"], "title": e["title"], "score": e["score"]}
                        for e in plan_resolution["ranked"]
                    ]
                },
            }

    origin_goal_id: Optional[List[str]]
    if goal_id_supplied:
        if origin_goal_id_raw is None:
            origin_goal_id = None
        elif isinstance(origin_goal_id_raw, list):
            origin_goal_id = [str(g) for g in origin_goal_id_raw if g]
        else:
            origin_goal_id = [str(origin_goal_id_raw)] if origin_goal_id_raw else None
    else:
        goals_dir = worktree_root / "state" / "goals"
        goal_items = await asyncio.to_thread(_collect_goals, goals_dir)
        goal_resolution = resolve_candidate(match_text, goal_items)
        resolved_goal_id = goal_resolution["resolved_id"]
        origin_goal_id = [resolved_goal_id] if resolved_goal_id is not None else None
        goal_reason = goal_resolution["reason"]
        if goal_reason is not None and goal_reason != ResolutionReason.NO_CANDIDATES:
            return {
                "status": "needs_disambiguation",
                "candidates": {
                    "goals": [
                        {"goal_id": e["id"], "title": e["title"], "score": e["score"]}
                        for e in goal_resolution["ranked"]
                    ]
                },
            }

    provenance = {
        "origin_session": origin_session,
        "origin_handoff": origin_handoff,
        "origin_handoff_id": origin_handoff_id,
        "origin_plan_id": origin_plan_id,
        "origin_goal_id": origin_goal_id,
    }

    class_context = await asyncio.to_thread(_class_context_for_items, items, worktree_root)
    source_body = _render_source_entries_body(items, class_context)
    body = source_body + ("\n" + extra_body.lstrip("\n") if extra_body else "")
    # C2 (ledger-owing handoff kinds): this op assembles its body from a caller
    # param rather than through coordinator-doc-new, so it does not inherit
    # C1's scaffolder-side emission. Append the canonical block when the
    # composed body doesn't already carry one — never duplicate it.
    if not _compile_heading_re("Session Ledger").search(body):
        body = body.rstrip("\n") + "\n\n" + "\n".join(SESSION_LEDGER_BLOCK_LINES)

    created_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    filename = _fork_handoff_filename(title)

    handoffs_dir.mkdir(parents=True, exist_ok=True)
    out_path = handoffs_dir / filename

    # Guard: refuse to create a live handoff sharing an already-archived
    # record's filename (this op does not stamp its own handoff_id at
    # creation, so only the filename basis applies here — see
    # handoff_creation_guard's module docstring for the full invariant).
    try:
        assert_no_archived_twin(out_path, worktree_root)
    except HandoffArchivedTwinError as exc:
        return _err(str(exc))

    # Review: code-reviewer (F2) — resolved once, in this function's own scope, so
    # a queue-scaffolded baton authored while a plan is claimed carries that plan's
    # deliverable_id instead of always minting a fresh one (DR-207 DD#1 second door).
    carried_deliverable_id = _resolve_claimed_plan_deliverable_id(worktree_root)

    def _mutate(old_text: str) -> str:
        if old_text:
            raise MutateAbort(
                f"handoff.scaffold_from_queue: collision — file already exists: {filename}"
            )
        content = _build_baton_frontmatter(
            title=title,
            created_date=created_date,
            branch=branch,
            kind=kind,
            workstream=workstream,
            provenance=provenance,
            body=body,
        )
        norm = _normalize_one_text(content, out_path, carried_deliverable_id)
        if norm is not None and norm is not _NO_FRONTMATTER:
            rebuilt = norm["rebuilt"]
            # _normalize_one_text only backfills an ABSENT category — ours is always
            # present up front, so this is a defense-in-depth assertion, not a fixup.
            return rebuilt
        return content

    try:
        await asyncio.to_thread(
            locked_rmw, out_path, _mutate, repo_root=repo_root, missing_ok=True
        )
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock for new handoff: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"I/O error creating queue-derived baton: {exc}")

    _LOG.info(
        "handoff.scaffold_from_queue: created %s from %d source entr%s",
        out_path, len(items), "y" if len(items) == 1 else "ies",
    )

    return {
        "status": "ok",
        "handoff_path": str(out_path),
        "handoff_id": out_path.stem,
        "category": CATEGORY_QUEUE_DERIVED_BATON,
        "source_entries": [{"id": item["id"], "path": item["path"]} for item in items],
    }


# ---------------------------------------------------------------------------
# Error-shape helper
# ---------------------------------------------------------------------------


def _err(msg: str) -> dict:
    """Return an exit_code=1 error reply dict."""
    _LOG.warning("handoff.scaffold_from_queue: %s", msg)
    return {"exit_code": 1, "error": msg}
