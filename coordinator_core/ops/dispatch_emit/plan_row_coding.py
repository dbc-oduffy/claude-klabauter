"""
coordinator_core.ops.dispatch_emit.plan_row_coding -- coding plan spine rows against landed commits.

``source_rows_by_plan`` maps committed chunk ids to the source-plan rows they close (a minted
inventory spine is never stamped); ``flip_rows_coded`` is the line-level ``open`` -> ``coded``
stamper; ``outside_landings`` / ``rows_landed_outside_executor`` find rows whose declared writes a
commit outside the run already landed, by an in-process first-parent walk (zero spawns).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import SPINE_SUFFIX
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
    CHECKPOINT_PLAN_LINE,
    CHECKPOINT_SUBJECT,
    is_stamp_commit,
)
from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

MINTED_SPINE_ORIGIN = "mise inventory record"

#: Commits a first-parent walk covers before it gives up.
REVIEW_WALK_CAP = 500

NOOP_DETAIL = (
    "resolved as a no-op: the row's files carry no change from this run "
    "(already landed, or a conditional no-change outcome); closed against the run's commit"
)


def yaml_load(text):
    """``yaml.safe_load`` on libyaml when present: same resolvers, same ``yaml.YAMLError``."""
    import yaml

    return yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def row_rank(row: dict) -> int:
    from coordinator_core.frontmatter.schema_validate import (
        _PLAN_TASKS_GROUPING_ORDER,
        _plan_tasks_row_grouping,
    )

    return _PLAN_TASKS_GROUPING_ORDER.index(_plan_tasks_row_grouping(row))


def spine_row_ids(plan_text: str) -> Optional[set]:
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

    located = locate_fenced_block(plan_text)
    if located.status != LocateStatus.LOCATED:
        return None
    try:
        rows = yaml_load(located.body) or []
    except yaml.YAMLError:
        return None
    if not isinstance(rows, list):
        return None
    return {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}


def inventory_record_rel(worktree_root: Path, spine_rel: str) -> Optional[tuple]:
    """``(inventory record rel, its text)`` for a minted spine, else None. A part spine
    (``-pN``) falls back to the unsplit record."""
    if not spine_rel.endswith(SPINE_SUFFIX):
        return None
    stem = spine_rel[: -len(SPINE_SUFFIX)]
    inventory_rel = stem + ".md"
    text = read_rel(worktree_root, inventory_rel)
    if text is None and re.search(r"-p\d+$", stem):
        inventory_rel = re.sub(r"-p\d+$", "", stem) + ".md"
        text = read_rel(worktree_root, inventory_rel)
    return None if text is None else (inventory_rel, text)


def source_rows_by_plan(
    worktree_root: Path, plan_path: Optional[str], chunk_ids: list
) -> dict:
    """Committed chunk ids -> ``{source plan (worktree-relative): {row id}}``.

    A plan-mode marker names the plan itself, and a chunk id IS its row id. An
    inventory-mode marker names the minted spine (``derived_from: mise
    inventory record``), which must never be stamped -- the next mint
    overwrites it; its rows map back through the sibling inventory record's
    Chunk table: ``<item>.<row>`` is row ``<row>`` of item's spec plan, and a
    bare ``<item>`` is a single-row reference only when it (or its
    prefix-stripped form) names a row of that plan -- a whole-plan item names
    no row, so nothing flips for it. Unresolvable pieces are skipped: the
    commit itself never fails over plan bookkeeping.
    """
    if not plan_path or not chunk_ids:
        return {}
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    marker_text = read_rel(worktree_root, plan_path)
    if marker_text is None:
        return {}
    # Read-only parses below are LF-shaped; a CRLF checkout must map the same.
    marker_text = marker_text.replace("\r\n", "\n")
    split = split_frontmatter(marker_text)
    origin = read_fm_field_unquoted(split.fm_text, "derived_from") if split else None
    if origin != MINTED_SPINE_ORIGIN:
        return {plan_path: set(chunk_ids)}

    from coordinator_core.ops.dispatch_emit.inventory_mint import (
        InventoryMintError,
        _bare_plan_row_id,
        _resolve_spec_plan_path,
        parse_chunk_table,
    )

    found = inventory_record_rel(worktree_root, plan_path)
    if found is None:
        return {}
    inventory_rel, inventory_text = found
    try:
        table = parse_chunk_table(inventory_text.replace("\r\n", "\n"))
    except InventoryMintError:
        return {}
    spec_by_item = {
        row.get("id", "").strip(): row.get("spec path", "").strip().strip("`")
        for row in table
    }

    out: dict = {}
    row_ids_cache: dict = {}
    for chunk_id in chunk_ids:
        item, _, row = chunk_id.partition(".")
        spec = spec_by_item.get(item)
        if not spec:
            continue
        abs_plan = _resolve_spec_plan_path(worktree_root / inventory_rel, spec)
        guarded = contained_path(abs_plan, [worktree_root])
        if guarded is None:
            continue
        rel = guarded.relative_to(worktree_root.resolve()).as_posix()
        if rel not in row_ids_cache:
            text = read_rel(worktree_root, rel)
            row_ids_cache[rel] = spine_row_ids(text.replace("\r\n", "\n")) if text is not None else None
        spine_ids = row_ids_cache[rel]
        if not spine_ids:
            continue
        if not row:
            row = item if item in spine_ids else _bare_plan_row_id(item)
        if row in spine_ids:
            out.setdefault(rel, set()).add(row)
    return out


def outside_landing_detail(shas: list) -> str:
    return (
        "resolved as a no-op: the row's declared writes landed outside this run's executor at "
        + ", ".join(s[:10] for s in shas)
        + "; the run's executor changed nothing"
    )


def flip_rows_coded(plan_text: str, row_ids: set, sha: str, details: Optional[dict] = None) -> tuple:
    """Line-level ``open`` -> ``coded`` + ``disposition_ref: <sha>`` on
    ``row_ids`` (``row_spans._stamp_rows_in_body``), then a stable D5 re-sort
    of row spans (an open row may not follow a coded one).

    Never a YAML round-trip: every untouched line survives byte-identical.
    ``details`` (row id -> ``disposition_detail`` prose) rides the same stamp.
    Only rows currently ``open`` flip; any other disposition is left alone.
    Returns ``(new_text, flipped_ids)``; ``flipped_ids`` empty means no edit.
    """
    import yaml

    from coordinator_core.execute_plan_assemble.row_spans import (
        _find_row_spans,
        _line_ending,
        _row_disposition,
        _stamp_rows_in_body,
    )

    # The row-span stamper matches LF lines only; a uniformly CRLF plan (any
    # Windows checkout) matched nothing and silently flipped no row.
    if "\r\n" in plan_text and "\n" not in plan_text.replace("\r\n", ""):
        new_text, flipped = flip_rows_coded(plan_text.replace("\r\n", "\n"), row_ids, sha, details)
        return (new_text.replace("\n", "\r\n"), flipped) if flipped else (plan_text, [])
    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

    located = locate_fenced_block(plan_text)
    if located.status != LocateStatus.LOCATED or located.span is None:
        return plan_text, []
    try:
        rows = yaml_load(located.body) or []
    except yaml.YAMLError:
        return plan_text, []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return plan_text, []
    row_by_id = {str(r.get("id")): r for r in rows}
    from coordinator_core.ops.dispatch_emit.memo_rows import _is_memo_send_row

    targets = {
        rid for rid in row_ids
        if rid in row_by_id and _row_disposition(row_by_id[rid]) == "open"
        and not _is_memo_send_row(row_by_id[rid])
    }
    if not targets:
        return plan_text, []

    start, end = located.span
    body = plan_text[start:end]
    stamped, err = _stamp_rows_in_body(
        body, {rid: sha for rid in targets},
        {rid: d for rid, d in (details or {}).items() if rid in targets},
    )
    if stamped is None:
        return plan_text, []
    ended_with_newline = stamped.endswith(("\n", "\r"))
    lines = stamped.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += _line_ending(lines[0])

    spans = _find_row_spans(lines)
    if spans:
        head = lines[: spans[0][0]]
        chunks = [(chunk_id, lines[s:e]) for s, e, chunk_id in spans]

        def rank(item: tuple) -> int:
            row = dict(row_by_id.get(item[0], {}))
            if item[0] in targets:
                row["disposition"] = "coded"
            return row_rank(row)

        chunks = sorted(chunks, key=rank)
        lines = head + [line for _cid, chunk in chunks for line in chunk]

    new_body = "".join(lines)
    if not ended_with_newline:
        new_body = new_body[:-2] if new_body.endswith("\r\n") else new_body[:-1]
    return plan_text[:start] + new_body + plan_text[end:], sorted(targets)


def close_memo_rows_delivered(plan_text: str, receipts: dict) -> tuple:
    """Line-level close of open memo-send rows as ``delivered_elsewhere`` via ``memo``.

    ``receipts`` maps row id -> ``(receipt_ref, receiver)``; each open row named there gets
    ``disposition_ref: <receipt_ref>``, ``delivered_via: memo`` and a ``disposition_detail``
    naming the receiver (the fields ``plan.tasks.mutate`` resolve writes). Untouched lines stay
    byte-identical. Returns ``(new_text, closed_ids)``; empty ``closed_ids`` means no edit.
    """
    import yaml

    from coordinator_core.execute_plan_assemble.row_spans import (
        _find_row_spans,
        _line_ending,
        _measure_row_content_indent,
        _row_disposition,
        _row_key_line_indices,
    )
    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.frontmatter.primitives import serialize_yaml_scalar

    if "\r\n" in plan_text and "\n" not in plan_text.replace("\r\n", ""):
        new_text, closed = close_memo_rows_delivered(plan_text.replace("\r\n", "\n"), receipts)
        return (new_text.replace("\n", "\r\n"), closed) if closed else (plan_text, [])
    located = locate_fenced_block(plan_text)
    if located.status != LocateStatus.LOCATED or located.span is None:
        return plan_text, []
    try:
        rows = yaml_load(located.body) or []
    except yaml.YAMLError:
        return plan_text, []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return plan_text, []
    row_by_id = {str(r.get("id")): r for r in rows}
    targets = {
        rid for rid in receipts
        if rid in row_by_id and _row_disposition(row_by_id[rid]) == "open"
    }
    if not targets:
        return plan_text, []

    start, end = located.span
    body = plan_text[start:end]
    ended_with_newline = body.endswith(("\n", "\r"))
    lines = body.splitlines(keepends=True)
    for s, e, rid in sorted(_find_row_spans(lines), key=lambda t: t[0], reverse=True):
        if rid not in targets:
            continue
        ref, receiver = receipts[rid]
        newline = _line_ending(lines[s])
        dash_indent = len(lines[s]) - len(lines[s].lstrip(" \t"))
        pad = " " * _measure_row_content_indent(lines, s, e, dash_indent)
        keys = _row_key_line_indices(lines, s + 1, e, len(pad))
        wanted = {
            "disposition": "delivered_elsewhere",
            "delivered_via": "memo",
            "disposition_ref": serialize_yaml_scalar(ref, numeric_quoting=True),
            "disposition_detail": serialize_yaml_scalar(f"memo sent to {receiver}; receipt {ref}"),
        }
        fresh = []
        for key, value in wanted.items():
            line = f"{pad}{key}: {value}{newline}"
            if key in keys:
                lines[keys[key]] = line
            else:
                fresh.append(line)
        if fresh:
            at = keys["deferred"] + 1 if "deferred" in keys else e
            if at > 0 and not lines[at - 1].endswith(("\n", "\r\n")):
                lines[at - 1] += newline
            lines[at:at] = fresh
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += _line_ending(lines[0])

    spans = _find_row_spans(lines)
    if spans:
        head = lines[: spans[0][0]]
        chunks = [(cid, lines[s:e]) for s, e, cid in spans]

        def rank(item: tuple) -> int:
            row = dict(row_by_id.get(item[0], {}))
            if item[0] in targets:
                row["disposition"] = "delivered_elsewhere"
            return row_rank(row)

        lines = head + [line for _cid, chunk in sorted(chunks, key=rank) for line in chunk]
    new_body = "".join(lines)
    if not ended_with_newline:
        new_body = new_body[:-2] if new_body.endswith("\r\n") else new_body[:-1]
    return plan_text[:start] + new_body + plan_text[end:], sorted(targets)


def outside_landings(
    worktree_root: Path, terminal_sha: str, plan_path: Optional[str], chunks: list, *,
    include_start: bool = False, after_creation: bool = False,
) -> dict:
    """Chunk id -> landing shas (newest first) of commits outside this run that landed every declared write.

    One in-process first-parent walk (zero spawns) from the terminal commit's parent,
    stepping over stamp commits and checkpoint commits of ``plan_path``. It stops at
    ``REVIEW_WALK_CAP`` or at the first commit without the plan file, so nothing older
    than the plan counts. A commit lands a path when its blob differs from its first
    parent's. A chunk is present only when every entry of ``paths`` has a landing commit.
    """
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit as _resolve_blob

    wanted = [c for c in chunks if c.paths]
    if not wanted or not plan_path:
        return {}
    blobs: dict = {}  # (sha, path) -> blob; scoped to this call so it never outlives the walk

    def _blob_at_commit(cdir, csha, path):
        key = (csha, path)
        if key not in blobs:
            blobs[key] = _resolve_blob(cdir, csha, path)
        return blobs[key]

    common_dir = resolve_git_common_dir(worktree_root)
    meta = commit_meta(common_dir, terminal_sha)
    parents = (meta or {}).get("parents") or []
    sha = terminal_sha if include_start else (parents[0] if parents else None)
    walked: list = []  # newest first: (sha, first parent or None)
    for _ in range(REVIEW_WALK_CAP):
        meta = commit_meta(common_dir, sha) if sha else None
        if meta is None:
            break
        blob = _blob_at_commit(common_dir, sha, plan_path)
        if blob is None or blob is ABSENT:
            break
        message = meta.get("message") or ""
        parent = (meta.get("parents") or [None])[0]
        if after_creation:
            parent_blob = _blob_at_commit(common_dir, parent, plan_path) if parent else ABSENT
            if parent_blob is None or parent_blob is ABSENT:
                break
        stepped_over = is_stamp_commit(message) or (
            CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
            and (m := CHECKPOINT_PLAN_LINE.search(message)) is not None and m.group("v") == plan_path
        )
        if not stepped_over:
            walked.append((sha, parent))
        if parent is None:
            break
        sha = parent
    out: dict = {}
    for chunk in wanted:
        shas: list = []
        for path in chunk.paths:
            hit = next(
                (
                    csha for csha, parent in walked
                    if _blob_at_commit(common_dir, csha, path)
                    != (_blob_at_commit(common_dir, parent, path) if parent else ABSENT)
                ),
                None,
            )
            if hit is None:
                break
            shas.append(hit)
        else:
            out[chunk.id] = sorted(set(shas), key=lambda s: [w[0] for w in walked].index(s))
    return out


def outside_landing_attribution(
    worktree_root: Path, terminal_sha: str, plan_path: Optional[str], chunks: list,
) -> dict:
    """Chunk id -> newest sha of the commit outside this run that landed its declared writes."""
    return {
        cid: shas[0] for cid, shas in outside_landings(worktree_root, terminal_sha, plan_path, chunks).items()
    }


def rows_landed_outside_executor(worktree_root: Path, plan_path: str, rows: list) -> dict:
    """Row id -> newest landing sha for each ``open`` row of ``plan_path`` whose every declared write
    a commit at or before HEAD already landed. The walk is in-process (zero spawns) and bounded by the
    plan file's creation commit, itself excluded, so a write at or before the plan never counts. A row with no ``writes`` is absent."""
    from types import SimpleNamespace

    from coordinator_core.git.git_state import head_sha

    chunks = [
        SimpleNamespace(id=r["id"], paths=[w for w in r["writes"] if isinstance(w, str) and w])
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("writes"), list)
    ]
    head = head_sha(worktree_root)
    if not head or not chunks:
        return {}
    return {
        cid: shas[0]
        for cid, shas in outside_landings(
            worktree_root, head, plan_path, chunks, include_start=True, after_creation=True,
        ).items()
    }
