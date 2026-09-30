"""
coordinator_core.ops.dispatch_emit.terminal_commit -- JSON-RPC
"dispatch.terminal_commit" operation.

Purpose: § Design D3 of docs/plans/2026-09-27-emitter-dag-terminal-commit-
wake-digest.md. The emitted DAG script (C12) no longer dispatches a
``coordinator:git-commit-agent`` per wave -- it writes ONE terminal-commit-
request marker (D2, ``commit_request.py``) recording what the run promises
the terminal commit. This op is fired once, after the run finishes, by the
driver (C7 for a headless fire, DoE D1/D2 for the EM) and lands exactly one
``ceremony.commit_v2`` call over the run's DONE chunks.

Keying scope: common_dir (mirrors ``ceremony.commit_v2`` itself, which this
handler calls in-process) -- the caller's own worktree, never a
``params.repo_root`` override (D3 consistency semantics only, same as
commit_v2).

Negative-spec:
  - Does NOT call ``ceremony.commit_v2`` more than once. Every DONE chunk's
    paths and prefix-claimed files fold into ONE call.
  - Does NOT infer prefix-claimed files from ``git status`` or prose -- only
    a DONE chunk's OWN report, read verbatim, under its OWN declared
    prefixes (mirrors emit.py's ``_prefix_commit_rule`` executor contract).
  - Declares to ``commit_v2`` only deletions a DONE chunk declared: a path
    in a chunk's own ``paths`` (its declared ``writes``) that is gone from
    the worktree but tracked at HEAD is passed as ``deleted_paths``. Any
    other absent tracked path (a prefix claim, the bookkeeping record) is an
    undeclared deletion and refuses the WHOLE commit; an absent path
    untracked at HEAD is dropped (nothing to declare).
  - Does NOT compare head_sha: peers commit to the shared branch mid-run.
    Only the branch NAME is checked (detached, unreadable, or differing from
    the marker's ``expected_branch`` refuses before any write).
  - Does NOT retry or catch commit_v2's structured refusals -- returned to
    the caller unmodified in substance, same posture commit_v2 itself takes
    toward ``commit_paths``.

DR-208 five-question affirmation (MUTATING; citing this handler, modelled on
``ceremony.commit_v2``'s own affirmation, coordinator_core/authz/
classification.py):
  1. Writes, deletes, or reorders any state file, queue, or git object?   YES.
     Indirectly: the one in-process ``ceremony.commit_v2`` call this handler
     makes writes git objects and moves the branch ref.
  2. Writes into rag's relational store?                                  No.
  3. Opens any file for write (including sentinel creation)?              No.
     This handler itself opens nothing for write -- it reads the emitted
     script and DONE chunks' report files, and delegates the one write
     (index splice, object write) to ``ceremony.commit_v2``.
  4. Mutates shared mutable state outside its own module?                 YES.
     The landed commit and moved ref, via the delegated commit_v2 call, are
     read by every subsequent dispatch against this repo.
  5. Persistent state changes observable across process boundaries?      YES.
     The commit sha and ref move persist across the whole box.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
Spec: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md § D3
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.execute_plan_assemble.row_spans import (
    _find_row_spans,
    _line_ending,
    _row_disposition,
    _stamp_rows_in_body,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.frontmatter.schema_validate import (
    _PLAN_TASKS_GROUPING_ORDER,
    _PLAN_TASKS_SUBORDER_BY_DISPOSITION,
    _plan_tasks_row_disposition,
    _plan_tasks_row_grouping,
    check_plan_tasks_source,
)
from coordinator_core.git.commit import partition_declared_deletions
from coordinator_core.git.commit_trailers import _UUID_RE
from coordinator_core.git.git_state import head_branch
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.commit_request import (
    PREFIX_CLAIM_LABEL,
    CommitRequest,
    parse_marker,
    plan_deliverable_id,
    valid_deliverable_id,
)
from coordinator_core.ops.dispatch_emit.inventory_mint import (
    InventoryMintError,
    _bare_plan_row_id,
    _resolve_spec_plan_path,
    parse_chunk_table,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave


def _error(message: str, **extra: object) -> dict:
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


_MINTED_SPINE_ORIGIN = "mise inventory record"
_SPINE_SUFFIX = ".spine.md"


def _read_rel(worktree_root: Path, rel: str) -> Optional[str]:
    guarded = contained_path(worktree_root / rel, [worktree_root])
    if guarded is None:
        return None
    try:
        with guarded.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError:
        return None


def _spine_row_ids(plan_text: str) -> Optional[set]:
    located = locate_fenced_block(plan_text)
    if located.status != LocateStatus.LOCATED:
        return None
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError:
        return None
    if not isinstance(rows, list):
        return None
    return {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}


def _source_rows_by_plan(
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
    marker_text = _read_rel(worktree_root, plan_path)
    if marker_text is None:
        return {}
    split = split_frontmatter(marker_text)
    origin = read_fm_field_unquoted(split.fm_text, "derived_from") if split else None
    if origin != _MINTED_SPINE_ORIGIN:
        return {plan_path: set(chunk_ids)}

    if not plan_path.endswith(_SPINE_SUFFIX):
        return {}
    inventory_rel = plan_path[: -len(_SPINE_SUFFIX)] + ".md"
    inventory_text = _read_rel(worktree_root, inventory_rel)
    if inventory_text is None:
        return {}
    try:
        table = parse_chunk_table(inventory_text)
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
            text = _read_rel(worktree_root, rel)
            row_ids_cache[rel] = _spine_row_ids(text) if text is not None else None
        spine_ids = row_ids_cache[rel]
        if not spine_ids:
            continue
        if not row:
            row = item if item in spine_ids else _bare_plan_row_id(item)
        if row in spine_ids:
            out.setdefault(rel, set()).add(row)
    return out


def _row_rank(row: dict) -> tuple:
    return (
        _PLAN_TASKS_GROUPING_ORDER.index(_plan_tasks_row_grouping(row)),
        _PLAN_TASKS_SUBORDER_BY_DISPOSITION.get(_plan_tasks_row_disposition(row), 0),
    )


def _flip_rows_coded(plan_text: str, row_ids: set, sha: str) -> tuple:
    """Line-level ``open`` -> ``coded`` + ``disposition_ref: <sha>`` on
    ``row_ids`` (``row_spans._stamp_rows_in_body``), then a stable D5 re-sort
    of row spans (an open row may not follow a coded one).

    Never a YAML round-trip: every untouched line survives byte-identical.
    Only rows currently ``open`` flip; any other disposition is left alone.
    Returns ``(new_text, flipped_ids)``; ``flipped_ids`` empty means no edit.
    """
    located = locate_fenced_block(plan_text)
    if located.status != LocateStatus.LOCATED or located.span is None:
        return plan_text, []
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError:
        return plan_text, []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return plan_text, []
    row_by_id = {str(r.get("id")): r for r in rows}
    targets = {
        rid for rid in row_ids
        if rid in row_by_id and _row_disposition(row_by_id[rid]) == "open"
    }
    if not targets:
        return plan_text, []

    start, end = located.span
    body = plan_text[start:end]
    stamped, err = _stamp_rows_in_body(body, {rid: sha for rid in targets})
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

        def rank(item: tuple) -> tuple:
            row = dict(row_by_id.get(item[0], {}))
            if item[0] in targets:
                row["disposition"] = "coded"
            return _row_rank(row)

        chunks = sorted(chunks, key=rank)
        lines = head + [line for _cid, chunk in chunks for line in chunk]

    new_body = "".join(lines)
    if not ended_with_newline:
        new_body = new_body[:-2] if new_body.endswith("\r\n") else new_body[:-1]
    return plan_text[:start] + new_body + plan_text[end:], sorted(targets)


def _stamp_coded_commit(
    commit_v2, worktree_root: Path, repo_root: Path, source_rows: dict,
    sha: str, session_id: Optional[str],
) -> dict:
    """Second, plan-only ``commit_v2`` call stamping the product commit's
    rows ``coded`` with ``disposition_ref: <sha>`` -- a SHA only exists once
    the product commit has landed, so this cannot ride in it. One call per
    run, never per row. An edit that would make a schema-valid plan invalid
    is refused before any write; on any failure every plan edit is restored.
    Returns ``{"rows_coded", "coded_sha"}`` or ``{"coded_stamp_error"}``.
    """
    originals: dict = {}
    rows_coded: dict = {}

    def restore() -> None:
        for rel, text in originals.items():
            (worktree_root / rel).write_text(text, encoding="utf-8", newline="")

    for plan_rel in sorted(source_rows):
        original = _read_rel(worktree_root, plan_rel)
        if original is None:
            continue
        updated, flipped = _flip_rows_coded(original, source_rows[plan_rel], sha)
        if not flipped:
            continue
        invalid = check_plan_tasks_source(updated)
        if invalid is not None and check_plan_tasks_source(original) is None:
            restore()
            return {"coded_stamp_error": f"{plan_rel}: stamped spine fails schema: {invalid}"}
        (worktree_root / plan_rel).write_text(updated, encoding="utf-8", newline="")
        originals[plan_rel] = original
        rows_coded[plan_rel] = flipped

    if not rows_coded:
        return {"rows_coded": {}, "coded_sha": None}
    n = sum(len(v) for v in rows_coded.values())
    params: dict = {"paths": sorted(rows_coded), "message": f"mark {n} rows coded ({sha[:7]})"}
    if session_id is not None:
        params["session_id"] = session_id
    try:
        reply = commit_v2(params, repo_root)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        reply = {"committed": False, "error": repr(exc)}
    if not isinstance(reply, dict) or not reply.get("committed"):
        restore()
        detail = reply.get("error") if isinstance(reply, dict) else reply
        return {"coded_stamp_error": f"coded-stamp commit did not land: {detail!r}"}
    return {"rows_coded": rows_coded, "coded_sha": reply.get("sha")}


def _parse_prefix_claims(report_text: str) -> Optional[list]:
    """Parse a DONE-report's ``created-under-prefix:`` claim list.

    Mirrors the executor contract emit.py's ``_prefix_claim_field`` states:
    one claim per line, each line ``created-under-prefix: <path>`` (or
    ``created-under-prefix: none`` for an explicit empty claim). Returns the
    (possibly empty) list of claimed paths, or ``None`` when the report
    carries NO ``created-under-prefix:`` line at all -- the "report has no
    list" refusal case D3 names.
    """
    claims: list = []
    saw_label = False
    for raw_line in report_text.splitlines():
        line = raw_line.strip().lstrip("-*").strip()
        if not line.startswith(PREFIX_CLAIM_LABEL):
            continue
        saw_label = True
        rest = line[len(PREFIX_CLAIM_LABEL) :].strip().strip("`'\"")
        if not rest or rest.lower() == "none":
            continue
        claims.append(rest)
    if not saw_label:
        return None
    return claims


def _own_prefix_files(worktree_root: Path, chunk, report_cache: dict) -> Optional[list]:
    """Files ``chunk``'s own report claims under ITS OWN declared prefixes.

    Returns ``None`` when the chunk declares prefixes but its report carries
    no ``created-under-prefix:`` list at all (the refusal case), or when the
    report file itself cannot be read. Returns ``[]`` when the report
    explicitly claims nothing (``created-under-prefix: none`` / no claim
    lines beyond that). A claimed file sitting OUTSIDE every one of the
    chunk's own prefixes is silently excluded -- only files under the
    chunk's OWN prefix are trusted, per D3.
    """
    if not chunk.prefixes:
        return []
    if chunk.report in report_cache:
        report_text = report_cache[chunk.report]
    else:
        try:
            report_text = (worktree_root / chunk.report).read_text(encoding="utf-8")
        except OSError:
            report_text = None
        report_cache[chunk.report] = report_text
    if report_text is None:
        return None
    claims = _parse_prefix_claims(report_text)
    if claims is None:
        return None
    kept = [
        path
        for path in claims
        if any(path == prefix or path.startswith(prefix) for prefix in chunk.prefixes)
    ]
    return kept


def _subject(contributing: list) -> str:
    ids = ", ".join(c.id for c in contributing)
    titles = "; ".join(c.title for c in contributing)
    return f"{ids}: {titles}"


@register_op("dispatch.terminal_commit")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.terminal_commit" handler -- mutating, sync.

    Params:
        script_path (str, required)      -- the emitted Workflow script to
                                             parse the terminal-commit-request
                                             marker (D2) out of. Guarded under
                                             the caller's own worktree before
                                             any read.
        incomplete_chunks (list[str], required, may be empty) -- chunk ids the
                                             run did NOT finish DONE. Any id
                                             not present in the marker's
                                             request refuses the call.
        inline_review (dict, optional)   -- ``{integration_stem, slices,
                                             fixes}``, relayed verbatim from
                                             the digest's
                                             ``next_action.params.inline_review``.
                                             When present, its trailer is
                                             appended to the commit message.
        session_id (str, optional)       -- forwarded to ``ceremony.commit_v2``
                                             when canonical-UUID shaped.

    Returns: ``ceremony.commit_v2``'s reply dict plus ``chunks_committed``
    (ids), ``dropped_absent`` (paths dropped as absent-and-untracked) and
    ``prefix_files`` (own-prefix-claimed files folded into the commit) and
    ``rows_coded`` (``{plan path: [row ids]}`` flipped ``open`` -> ``coded``)
    and ``coded_sha`` (the second, plan-only commit), or ``coded_stamp_error``
    when that second step failed -- the product commit stands regardless. A
    script with no marker returns ``{"committed": False, "nothing_to_commit":
    True}`` without error. ``commit_v2``'s own ``nothing_to_commit: True``
    (a peer already landed the bytes) passes through unmodified, as a
    non-error.
    """
    if repo_root is None:
        return _error(
            "dispatch.terminal_commit requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    script_path_raw = params.get("script_path")
    if not isinstance(script_path_raw, str) or not script_path_raw:
        return _error("params.script_path is required and must be a non-empty string")

    raw_incomplete = params.get("incomplete_chunks")
    if raw_incomplete is None:
        return _error("params.incomplete_chunks is required (may be an empty list)")
    if not isinstance(raw_incomplete, list) or not all(
        isinstance(c, str) for c in raw_incomplete
    ):
        return _error("params.incomplete_chunks must be a list of strings")
    incomplete_chunks = set(raw_incomplete)

    inline_review = params.get("inline_review")
    if inline_review is not None and not isinstance(inline_review, dict):
        return _error("params.inline_review must be an object or omitted")

    session_id = params.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id)
    ):
        session_id = None

    worktree_root = main_worktree_root(repo_root)

    guarded_script = contained_path(worktree_root / script_path_raw, [worktree_root])
    if guarded_script is None:
        return _error(
            f"params.script_path {script_path_raw!r} does not resolve under the "
            "caller's own worktree"
        )

    try:
        script_text = guarded_script.read_text(encoding="utf-8")
    except OSError as exc:
        return _error(f"cannot read params.script_path {script_path_raw!r}: {exc}")

    request: Optional[CommitRequest] = parse_marker(script_text)
    if request is None:
        return {"committed": False, "nothing_to_commit": True}

    observed_branch = head_branch(worktree_root)
    if observed_branch == "HEAD":
        return _error(
            "HEAD is detached; check out a branch and re-fire, or commit by hand",
            refused="detached-head",
            observed_branch="HEAD",
        )
    if observed_branch is None:
        return _error(
            "HEAD is unreadable; repair the worktree and re-fire, or commit by hand",
            refused="branch-unreadable",
        )
    expected_branch = request.expected_branch
    if expected_branch is not None and expected_branch != observed_branch:
        return _error(
            f"run expected branch {expected_branch!r} but HEAD is on "
            f"{observed_branch!r}; check out {expected_branch} and re-fire, "
            "or commit by hand",
            refused="branch-mismatch",
            expected_branch=expected_branch,
            observed_branch=observed_branch,
        )
    branch_check = "ok" if expected_branch is not None else "not-recorded"

    marker_id = request.deliverable_id
    if marker_id is not None:
        plan_id: Optional[str] = None
        plan_readable = True
        if request.plan_path:
            plan_text = _read_rel(worktree_root, request.plan_path)
            plan_readable = plan_text is not None
            plan_id = plan_deliverable_id(plan_text) if plan_text is not None else None
        refusal: Optional[str] = None
        if valid_deliverable_id(marker_id) is None:
            refusal = "is not a valid dlv- id"
        elif request.plan_path and not plan_readable:
            refusal = f"cannot be verified: plan_path {request.plan_path!r} is unreadable"
        elif request.plan_path and marker_id != plan_id:
            refusal = "differs from the plan's deliverable_id"
        if refusal is not None:
            return _error(
                f"marker deliverable_id {marker_id!r} {refusal}",
                deliverable_id_marker=marker_id,
                deliverable_id_plan=plan_id,
            )
    elif request.plan_path:
        plan_text = _read_rel(worktree_root, request.plan_path)
        plan_id = plan_deliverable_id(plan_text) if plan_text is not None else None
        if plan_id is not None:
            return _error(
                "marker carries no deliverable_id but the plan's is set",
                deliverable_id_marker=None,
                deliverable_id_plan=plan_id,
            )

    # Zero-integration-stage path (2026-09-28 PM order, step b'): a
    # `bookkeeping` inline_review carries `wave_sidecar_paths`/`prep_sidecar`/
    # `plan_id` (the one-stage `inline_review` never does -- its
    # `integration_stem` names a real integration sidecar an agent already
    # wrote). Run the mechanical bookkeeping step FIRST, before the commit is
    # built, so the record it writes lands in the SAME commit as the code it
    # reviews -- the only seam that can call it, since the emitted script has
    # no JS-callable "invoke a Python op" primitive
    # (review_mint/wave_bookkeeping.py's own module docstring).
    bookkeeping_record_path: Optional[str] = None
    if inline_review is not None and all(
        inline_review.get(k) for k in ("wave_sidecar_paths", "prep_sidecar", "plan_id")
    ):
        if not session_id:
            return _error(
                "params.inline_review names a bookkeeping stem (wave_sidecar_paths/"
                "prep_sidecar/plan_id present) but params.session_id is missing or "
                "not canonical-UUID-shaped -- bookkeep_wave needs it to place the record"
            )
        wave_sidecar_paths = [
            worktree_root / p for p in inline_review["wave_sidecar_paths"] if isinstance(p, str)
        ]
        try:
            record = bookkeep_wave(
                wave_sidecar_paths,
                repo_root=worktree_root,
                session_id=session_id,
                plan_id=inline_review["plan_id"],
                prep_sidecar=inline_review["prep_sidecar"],
                record_stem=inline_review["integration_stem"],
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silently dropped
            return _error(f"review_mint.bookkeep_wave failed: {exc}")
        record_path_abs = Path(record["sidecar_path"])
        try:
            bookkeeping_record_path = str(
                record_path_abs.relative_to(worktree_root)
            ).replace("\\", "/")
        except ValueError:
            bookkeeping_record_path = None

    known_ids = {chunk.id for chunk in request.chunks}
    # The wake digest lists every unfinished row, including rows that never
    # started and so never reached the marker; those carry no paths to hold
    # back, so they are reported, not refused.
    unmarked_incomplete = sorted(incomplete_chunks - known_ids)

    done_chunks = [c for c in request.chunks if c.id not in incomplete_chunks]

    report_cache: dict = {}
    all_paths: list = []
    prefix_files: list = []

    for chunk in done_chunks:
        own_prefix_files = _own_prefix_files(worktree_root, chunk, report_cache)
        if own_prefix_files is None:
            return _error(
                f"chunk {chunk.id!r} declares prefixes {list(chunk.prefixes)} but its "
                f"report {chunk.report!r} carries no `{PREFIX_CLAIM_LABEL}` list -- "
                "list it in params.incomplete_chunks to commit the rest"
            )
        chunk_paths = list(chunk.paths) + own_prefix_files
        all_paths.extend(chunk_paths)
        prefix_files.extend(own_prefix_files)

    if bookkeeping_record_path is not None and bookkeeping_record_path not in all_paths:
        all_paths.append(bookkeeping_record_path)

    if not all_paths:
        return {"committed": False, "nothing_to_commit": True}

    declared_writes = {p for c in done_chunks for p in c.paths}
    absent = [p for p in all_paths if not (worktree_root / p).exists()]
    dropped_absent: list = []
    deleted_paths: list = []
    if absent:
        partition = partition_declared_deletions(worktree_root, absent)
        if partition is None:
            return _error(
                "cannot resolve HEAD tree spine to classify absent path(s) "
                f"{absent}"
            )
        tracked_at_head, absent_from_head = partition
        undeclared = [p for p in tracked_at_head if p not in declared_writes]
        if undeclared:
            return _error(
                "path(s) gone from the worktree but still tracked at HEAD are not "
                f"in any DONE chunk's declared writes, an undeclared deletion: {undeclared}"
            )
        deleted_paths = sorted(set(tracked_at_head))
        dropped_absent = list(absent_from_head)
        removed = set(dropped_absent) | set(deleted_paths)
        all_paths = [p for p in all_paths if p not in removed]

    if not all_paths and not deleted_paths:
        return {"committed": False, "nothing_to_commit": True}

    final_paths_set = set(all_paths)
    contributing_chunks = [
        c
        for c in done_chunks
        if any(p in final_paths_set or p in deleted_paths for p in list(c.paths)) or any(
            p in final_paths_set
            for p in _own_prefix_files(worktree_root, c, report_cache) or []
        )
    ]

    message_lines = [_subject(contributing_chunks)]
    if deleted_paths:
        # The undeclared-staged-deletion guard reads the message for a removal verb.
        message_lines.append("Removes declared write(s): " + ", ".join(deleted_paths))
    if request.deliverable_id:
        message_lines.append(f"Deliverable-Id: {request.deliverable_id}")
    if inline_review is not None:
        stem = inline_review.get("integration_stem")
        slices = inline_review.get("slices")
        fixes = inline_review.get("fixes")
        if not stem or slices is None:
            return _error(
                "params.inline_review is missing integration_stem and/or slices -- "
                "refusing to write an Inline-Review trailer with a None field "
                f"(got: {inline_review!r})"
            )
        message_lines.append(
            f"Inline-Review: applies {stem} -- execute-review: {slices} slices, "
            f"{fixes} fixes"
        )
    message = "\n\n".join([message_lines[0], "\n".join(message_lines[1:])]) if len(
        message_lines
    ) > 1 else message_lines[0]

    commit_v2 = get_op_handler("ceremony.commit_v2")
    if commit_v2 is None:
        return _error("ceremony.commit_v2 is not registered")

    commit_params: dict = {"paths": all_paths, "message": message}
    if deleted_paths:
        commit_params["deleted_paths"] = deleted_paths
    if session_id is not None:
        commit_params["session_id"] = session_id

    reply = commit_v2(commit_params, repo_root)
    if not isinstance(reply, dict):
        reply = {"committed": False, "sha": None, "error": f"unexpected commit_v2 reply: {reply!r}"}

    reply = dict(reply)
    if reply.get("committed") and reply.get("sha"):
        # Advance plan status so a re-fire (which selects live `open` rows)
        # cannot redo landed work. incomplete_chunks never reach here.
        source_rows = _source_rows_by_plan(
            worktree_root, request.plan_path, [c.id for c in contributing_chunks]
        )
        reply.update(
            _stamp_coded_commit(
                commit_v2, worktree_root, repo_root, source_rows,
                str(reply["sha"]), session_id,
            )
        )
    reply["branch_check"] = branch_check
    reply["chunks_committed"] = [c.id for c in contributing_chunks]
    reply["dropped_absent"] = dropped_absent
    reply["deleted_paths"] = deleted_paths
    reply["unmarked_incomplete"] = unmarked_incomplete
    reply["prefix_files"] = prefix_files
    return reply
