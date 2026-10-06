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
  - Does NOT commit a dirty file no chunk declares: ``undeclared_dirty``
    reports it, because widening the pathspec would hide a wrong spine.
  - Does NOT write a receipt when there is nothing to commit.
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
  3. Opens any file for write (including sentinel creation)?              YES.
     Exclusive-creates the run's completion-receipt files
     (``completion_receipt.write_run_receipts``) before the commit; all other
     writes (index splice, object write) are delegated to ``ceremony.commit_v2``.
  4. Mutates shared mutable state outside its own module?                 YES.
     The landed commit and moved ref, via the delegated commit_v2 call, are
     read by every subsequent dispatch against this repo.
  5. Persistent state changes observable across process boundaries?      YES.
     The commit sha and ref move persist across the whole box.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
Spec: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md § D3
"""

from __future__ import annotations

import ast
import json
import re
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
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.claimed_write import replace_text
from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    PREFIX_CLAIM_LABEL,
    CommitRequest,
    MalformedCommitRequestError,
    parse_manifest_marker,
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
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave


_PARAM_FIELDS = (
    Field("script_path", "nonempty_str", required=True),
    Field("incomplete_chunks", "str_list", required=True),
    Field("landed_chunks", "str_list"),
    Field("inline_review", "dict"),
    Field("task_output_path", "nonempty_str"),
    Field("plan_path", "nonempty_str"),
)


def _params_from_task_output(path_raw: str) -> dict:
    """`next_action.params` out of a Workflow task-output file, bare or `{summary, logs, result}`-wrapped.

    Same envelope unwrap as `land-wave.py :: _wave_result`: a dict `result` is the digest.
    Raises ValueError naming the file when no `next_action.params` dict is found.
    """
    path = Path(path_raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read params.task_output_path {path_raw!r}: {exc}") from exc
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    action = data.get("next_action") if isinstance(data, dict) else None
    found = action.get("params") if isinstance(action, dict) else None
    if not isinstance(found, dict):
        raise ValueError(
            f"{path_raw!r} carries no next_action.params: neither a digest nor a "
            "task-output envelope wrapping one"
        )
    return found


def _anchor_request(worktree_root: Path, params: dict, inline_review: Optional[dict]) -> Optional[CommitRequest]:
    """The zero-chunk request for a marker-less run whose review delivered PASS
    with no product file: it still owes the plan a fresh `Inline-Review`
    commit for `review_stamp` to anchor on. ``None`` for any other shape."""
    plan_rel = params.get("plan_path")
    if not isinstance(plan_rel, str) or not isinstance(inline_review, dict):
        return None
    delivery = inline_review.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("verdict") != "PASS":
        return None
    files = delivery.get("product_files")
    if isinstance(files, bool) or not isinstance(files, int) or files != 0:
        return None
    plan_text = _read_rel(worktree_root, plan_rel)
    if plan_text is None:
        return None
    return CommitRequest(
        chunks=(), plan_path=plan_rel, deliverable_id=plan_deliverable_id(plan_text)
    )


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
    # Read-only parses below are LF-shaped; a CRLF checkout must map the same.
    marker_text = marker_text.replace("\r\n", "\n")
    split = split_frontmatter(marker_text)
    origin = read_fm_field_unquoted(split.fm_text, "derived_from") if split else None
    if origin != _MINTED_SPINE_ORIGIN:
        return {plan_path: set(chunk_ids)}

    if not plan_path.endswith(_SPINE_SUFFIX):
        return {}
    stem = plan_path[: -len(_SPINE_SUFFIX)]
    inventory_rel = stem + ".md"
    inventory_text = _read_rel(worktree_root, inventory_rel)
    if inventory_text is None and re.search(r"-p\d+$", stem):
        inventory_rel = re.sub(r"-p\d+$", "", stem) + ".md"
        inventory_text = _read_rel(worktree_root, inventory_rel)
    if inventory_text is None:
        return {}
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
            text = _read_rel(worktree_root, rel)
            row_ids_cache[rel] = _spine_row_ids(text.replace("\r\n", "\n")) if text is not None else None
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
    # The row-span stamper matches LF lines only; a uniformly CRLF plan (any
    # Windows checkout) matched nothing and silently flipped no row.
    if "\r\n" in plan_text and "\n" not in plan_text.replace("\r\n", ""):
        new_text, flipped = _flip_rows_coded(plan_text.replace("\r\n", "\n"), row_ids, sha)
        return (new_text.replace("\n", "\r\n"), flipped) if flipped else (plan_text, [])
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
    sha: str, session_id: Optional[str], also_commit: tuple = (),
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
            replace_text(worktree_root / rel, text)

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
        replace_text(worktree_root / plan_rel, updated)
        originals[plan_rel] = original
        rows_coded[plan_rel] = flipped

    # A plan `review_stamp.mint` just wrote rides this commit even when none of
    # its rows flipped (a re-fire over already-coded rows).
    paths = sorted(set(rows_coded) | set(also_commit))
    # `git status` lists a path only when a commit over it can carry it: an
    # untracked gitignored plan (a warp run spine) is absent, so never staged.
    stageable = _changed_paths(worktree_root, paths) if paths else None
    if stageable is not None:
        paths = [p for p in paths if p in stageable]
    if not paths:
        return {"rows_coded": {}, "coded_sha": None}
    n = sum(len(v) for v in rows_coded.values())
    message = f"mark {n} rows coded ({sha[:7]})" if n else f"review stamp ({sha[:7]})"
    params: dict = {"paths": paths, "message": message}
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
    out = {"rows_coded": rows_coded, "coded_sha": reply.get("sha")}
    if reply.get("index_stale"):
        out["coded_index_stale"] = list(reply["index_stale"])
    return out


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
    # Match on whole path segments: a bare prefix ``src/foo`` must not admit
    # the leading-string sibling ``src/foobar/x``.
    bounded = [
        (prefix, prefix if prefix.endswith("/") else prefix + "/")
        for prefix in chunk.prefixes
    ]
    return [
        path
        for path in claims
        if any(path == prefix or path.startswith(under) for prefix, under in bounded)
    ]


def _stage_returns(inline_review: dict) -> Optional[dict]:
    """The run record's stage-return fields out of the digest's
    ``inline_review``, or ``None`` when the digest predates them (no ``prep``
    block). Maps the digest's names onto the record keys ``review_stamp.mint``
    reads."""
    if not isinstance(inline_review.get("prep"), dict):
        return None
    out: dict = {k: inline_review.get(k) for k in ("prep", "delivery", "tests", "criterion")}
    fixes = inline_review.get("fixes")
    if isinstance(fixes, int) and not isinstance(fixes, bool):
        out["fixes_applied"] = fixes
    integration = inline_review.get("integration")
    if isinstance(integration, dict):
        out["integration_sidecar"] = integration.get("sidecar")
        out["unresolved"] = integration.get("unresolved") or []
        out["confinement_violations"] = integration.get("confinement_violations") or 0
    return out


def _mint_review_stamp(
    worktree_root: Path, plan_rel: Optional[str], sha: str, record_abs: Path, record: dict
) -> dict:
    """Mint the plan's ``review_stamp`` against the commit this op just
    landed. A refusal is reported, never raised: the product commit stands."""
    if not plan_rel:
        return {}
    guarded = contained_path(worktree_root / plan_rel, [worktree_root])
    if guarded is None:
        return {}
    from coordinator_core.ops.review_stamp import MintRefusal, mint

    try:
        mint(guarded, worktree_root, build_test_path=None, resolved=(sha, record_abs, record))
    except MintRefusal as exc:
        return {"review_stamp": "refused", "review_stamp_refusal": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"review_stamp": "refused", "review_stamp_refusal": repr(exc)}
    return {"review_stamp": "minted"}


def _stamp_plan_implemented(worktree_root: Path, plan_rel: str, sha: str) -> dict:
    """Flip the plan to ``implemented`` when the review stamp it now carries
    records a ``met`` criterion. The verdict is read off the plan on disk,
    never a param, so only a minted stamp can discharge it. A refusal (open
    spine rows, the goal gate) is reported; every commit before it stands."""
    import contextlib
    import io

    from coordinator_core.archive_stamp import cs_stamp_plan_implemented
    from coordinator_core.ops.plan_status_transition import _FALSIFIER_OUTPUT_MAX

    plan_abs = worktree_root / plan_rel
    text = _read_rel(worktree_root, plan_rel)
    split = split_frontmatter(text.replace("\r\n", "\n")) if text is not None else None
    try:
        fm = (yaml.safe_load(split.fm_text) or {}) if split is not None else {}
    except yaml.YAMLError:
        fm = {}
    stamp = fm.get("review_stamp") if isinstance(fm, dict) else None
    criterion = stamp.get("criterion") if isinstance(stamp, dict) else None
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return {"plan_status": "not-stamped", "plan_status_reason": "review_stamp criterion is not met"}
    observation = str(criterion.get("observation") or "").strip()
    if not observation:
        return {"plan_status": "not-stamped", "plan_status_reason": "criterion observation is empty"}
    source = criterion.get("sidecar") or "the run's criterion leg"
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cs_stamp_plan_implemented(
            str(plan_abs),
            falsifier_verdict="pass",
            falsifier_output=observation[:_FALSIFIER_OUTPUT_MAX],
            prose=f"criterion met at {sha} per {source}",
            refuse_open_spine_rows=True,
        )
    if rc != 0:
        return {"plan_status": "refused", "plan_status_refusal": err.getvalue().strip()[-600:]}
    return {"plan_status": "implemented"}


_UNDECLARED_DIRTY_CAP = 100


def _glob_literal(text: str) -> str:
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in text)


def _scope_pathspecs(request: CommitRequest) -> list:
    """Git pathspecs naming the directories a run's declared writes sit in:
    each declared path's own directory, non-recursive, and each declared
    prefix, recursive."""
    specs: dict = {}
    for chunk in request.chunks:
        for path in chunk.paths:
            parent = path.rpartition("/")[0]
            specs[f":(glob){_glob_literal(parent + '/') if parent else ''}*"] = None
        for prefix in chunk.prefixes:
            specs[f":(literal){prefix.rstrip('/')}/"] = None
    return list(specs)


def _undeclared_dirty(worktree_root: Path, request: CommitRequest) -> dict:
    """Dirty files beside the run's declared writes that no chunk declares.

    The terminal commit lands only declared paths and own-report prefix
    claims, so an executor-written file at an undeclared path is never staged
    by anything. It is reported, never committed: a wrong spine is the
    signal. One scoped ``git status`` spawn; the shared tree means a peer's
    dirt in the same directory is listed too, so a row is a candidate for
    inspection, not proof of this run's authorship. ``undeclared_dirty`` is
    ``None`` when git could not answer.
    """
    specs = _scope_pathspecs(request)
    if not specs:
        return {"undeclared_dirty": []}
    result = run_git(
        [
            "-C", str(worktree_root), "--no-optional-locks", "status",
            "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--", *specs,
        ],
        binary=True,
    )
    if not result.ok:
        return {"undeclared_dirty": None}
    declared = {p for c in request.chunks for p in c.paths}
    dirty = sorted(
        {
            entry[3:].decode("utf-8", "surrogateescape")
            for entry in result.stdout_bytes.split(b"\0")
            if len(entry) > 3
        }
        - declared
    )
    out: dict = {"undeclared_dirty": dirty[:_UNDECLARED_DIRTY_CAP]}
    if len(dirty) > _UNDECLARED_DIRTY_CAP:
        out["undeclared_dirty_total"] = len(dirty)
    return out


_DELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}DONE(?:_WITH_CONCERNS)?[*_`]{0,2}:|<exit-status>DONE</exit-status>',
    re.MULTILINE,
)
_UNDELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}(?:PARTIAL|BLOCKED)[*_`]{0,2}:|<exit-status>(?:PARTIAL|BLOCKED)</exit-status>',
    re.MULTILINE,
)
_PARTIAL_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}PARTIAL[*_`]{0,2}:|<exit-status>PARTIAL</exit-status>', re.MULTILINE
)
_UNDONE_LINE_RE = re.compile(r"^\s*[-*]?\s*[*_`]{0,2}(?:not done|undone|remaining)", re.IGNORECASE)
_UNDONE_CAP = 400


def _undone_summary(text: str) -> str:
    """The report's named undone work: its ``Not done``/``Remaining`` lines, else its PARTIAL line."""
    lines = text.splitlines()
    picked = [ln.strip() for ln in lines if _UNDONE_LINE_RE.match(ln)]
    if not picked:
        picked = [ln.strip() for ln in lines if re.match(r'\s*"?[*_`]{0,2}PARTIAL', ln)]
    return " | ".join(picked)[:_UNDONE_CAP]


def _partial_chunks(
    worktree_root: Path, request: CommitRequest, incomplete: set, withheld: Optional[dict] = None
) -> list:
    """Incomplete chunks whose executor itself reported DONE/DONE_WITH_CONCERNS: the row
    is incomplete only because the delivery verdict failed (an AC needing a later runtime
    step), and its files were reviewed with the wave. An executor PARTIAL or BLOCKED, an
    unreadable report, or one carrying no DONE status stays stranded; a PARTIAL row is
    recorded in ``withheld`` as ``{id: undone AC text}``."""
    out: list = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is None:
            continue
        if _DELIVERED_REPORT_RE.search(text) and not _UNDELIVERED_REPORT_RE.search(text):
            out.append(chunk)
        elif withheld is not None and _PARTIAL_REPORT_RE.search(text):
            withheld[chunk.id] = _undone_summary(text)
    return out


def _changed_paths(worktree_root: Path, paths: list) -> Optional[set]:
    """The subset of ``paths`` whose worktree bytes differ from HEAD (modified,
    added, untracked or deleted): the files a commit over them can carry. One
    scoped ``git status`` spawn; ``None`` when git could not answer."""
    if not paths:
        return set()
    result = run_git(
        [
            "-C", str(worktree_root), "--no-optional-locks", "status",
            "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--",
            *[f":(literal){p}" for p in paths],
        ],
        binary=True,
    )
    if not result.ok:
        return None
    return {
        entry[3:].decode("utf-8", "surrogateescape")
        for entry in result.stdout_bytes.split(b"\0")
        if len(entry) > 3
    }


def _entangled_chunks(
    worktree_root: Path, request: CommitRequest, stranded_ids: set, report_cache: dict
) -> dict:
    """Committable chunks whose ``.py`` files import a file of a stranded chunk, transitively.

    Committing such a chunk lands an importer whose target is absent from the
    tree at that sha (``leak_gate`` / ``import_closure`` would refuse the whole
    commit). Only stranded files that differ from HEAD count: an unchanged one
    already resolves. Returns ``{chunk id: sorted ids of the stranded chunks it
    needs}``; a chunk needing only another entangled chunk names that chain's root.
    """
    from coordinator_core.authoring_leaks.import_closure import _needed_paths
    from coordinator_core.pyimports import collect_imports

    by_id = {c.id: c for c in request.chunks}
    declared: dict = {}
    prefix_roots: list = []
    for cid in stranded_ids:
        chunk = by_id.get(cid)
        if chunk is None:
            continue
        for path in chunk.paths:
            declared[path] = cid
        for prefix in chunk.prefixes:
            prefix_roots.append((prefix.rstrip("/"), cid))
    specs = [p for p in declared if p.endswith(".py")] + [root for root, _ in prefix_roots]
    changed = _changed_paths(worktree_root, specs)
    blocked_files: dict = {}
    if changed is None:
        blocked_files = {p: c for p, c in declared.items() if p.endswith(".py")}
    else:
        for path in changed:
            if not path.endswith(".py"):
                continue
            if path in declared:
                blocked_files[path] = declared[path]
                continue
            for root, cid in prefix_roots:
                if path == root or path.startswith(root + "/"):
                    blocked_files[path] = cid
                    break
    if not blocked_files:
        return {}

    files_of: dict = {}
    imports_of: dict = {}
    for chunk in request.chunks:
        if chunk.id in stranded_ids:
            continue
        prefix_files = _own_prefix_files(worktree_root, chunk, report_cache) or []
        needed: set = set()
        for rel in list(chunk.paths) + list(prefix_files):
            text = _read_rel(worktree_root, rel) if rel.endswith(".py") else None
            if text is None:
                continue
            try:
                records, _ = collect_imports(ast.parse(text))
            except (SyntaxError, ValueError):
                continue
            needed |= _needed_paths((rel, rec) for rec in records)
        imports_of[chunk.id] = needed
        files_of[chunk.id] = set(chunk.paths) | set(prefix_files)

    entangled: dict = {}
    grew = True
    while grew:
        grew = False
        for cid, needed in imports_of.items():
            if cid in entangled:
                continue
            roots = {blocked_files[p] for p in needed if p in blocked_files}
            for other, other_roots in entangled.items():
                if files_of[other] & needed:
                    roots |= set(other_roots)
            if roots:
                entangled[cid] = sorted(roots)
                grew = True
    return entangled


def _subject(contributing: list, fallback: str) -> str:
    ids = ", ".join(c.id for c in contributing)
    titles = "; ".join(c.title for c in contributing if c.title.strip())
    if not ids:
        return fallback
    return f"{ids}: {titles}" if titles else ids


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
        landed_chunks (list[str], optional)  -- chunk ids the digest listed
                                             incomplete that the driver landed
                                             during recovery (a resumed run
                                             replays their cached BLOCKED
                                             report). Treated as DONE: removed
                                             from the incomplete set before
                                             the commit and receipts are built.
        inline_review (dict, required when the script carries a marker) --
                                             ``{integration_stem, slices,
                                             fixes}``, relayed verbatim from
                                             the digest's
                                             ``next_action.params.inline_review``.
                                             Absent or missing stem/slices
                                             refuses (``refused="unreviewed"``);
                                             its trailer is appended to the
                                             commit message.
        task_output_path (str, optional) -- a Workflow task-output file; its
                                             ``next_action.params`` supplies
                                             script_path / incomplete_chunks /
                                             inline_review, explicit params win.
        session_id (str, optional)       -- forwarded to ``ceremony.commit_v2``
                                             when canonical-UUID shaped.

    Returns: ``ceremony.commit_v2``'s reply dict plus ``chunks_committed``
    (ids), ``dropped_absent`` (paths dropped as absent-and-untracked) and
    ``prefix_files`` (own-prefix-claimed files folded into the commit),
    ``incomplete_chunks`` (present only when ``landed_chunks`` was passed: the
    digest's incomplete list minus them) and
    ``rows_coded`` (``{plan path: [row ids]}`` flipped ``open`` -> ``coded``)
    and ``coded_sha`` (the second, plan-only commit), or ``coded_stamp_error``
    when that second step failed -- the product commit stands regardless.
    ``incomplete_reasons`` maps every incomplete id to its reason: ``external_gate``
    (a row the ask manifest withheld; it carries no paths, so nothing is stranded),
    ``executor_partial: <Not-done lines>`` or ``incomplete_unreported``;
    ``index_stale`` lists committed paths whose index entry could not be
    spliced to the landed blob (``[]`` when every entry equals it); a
    non-empty list means the run is not clean.
    ``review_stamp`` (``minted``/``refused``) reports the stamp, and on a
    minted stamp with no incomplete chunk ``plan_status`` reports the
    ``implemented`` flip (``implemented``/``refused``/``not-stamped``), which
    ``plan_status_transition`` commits on its own. A
    script with no marker returns ``{"committed": False, "nothing_to_commit":
    True}`` without error. ``commit_v2``'s own ``nothing_to_commit: True``
    (a peer already landed the bytes) passes through unmodified, as a
    non-error. Every reply also carries ``stranded`` -- ``{chunk id: [declared
    paths]}`` for each ``incomplete_chunks`` id the request marker names, the
    work the commit left uncommitted; ``{}`` when none, or when no marker was read.
    ``entangled`` (present only when non-empty) maps each chunk held back from the commit to
    the stranded chunk ids whose uncommitted ``.py`` files it imports (transitively): landing it
    would leave an unresolvable import at that sha. It is stranded with them, its reason
    ``entangled: ...``; land the pair by hand once the definer is finished.
    ``blockers`` (present only when non-empty) maps each executor-PARTIAL row, whose
    files stay uncommitted, to the undone work its report names.
    Once the run reaches its commit (every refusal and no-op before that omits it),
    the reply also carries ``undeclared_dirty`` -- the
    dirty files (modified or untracked) in the directories of the run's declared
    writes, or under its declared prefixes, that no chunk declares: work the
    commit could never see. Reported, never committed. ``None`` when git could
    not answer; ``undeclared_dirty_total`` appears when the list is capped.
    """
    stranded: dict = {}
    scope: dict = {}
    gated_ids: set = set()
    blockers: dict = {}
    reply = _terminal_commit(params, repo_root, stranded, scope, gated_ids, blockers)
    reply["stranded"] = stranded
    if blockers:
        reply["blockers"] = blockers
    if scope:
        reply.update(_undeclared_dirty(scope["root"], scope["request"]))
    incomplete = params.get("incomplete_chunks") if isinstance(params, dict) else None
    landed = (params.get("landed_chunks") if isinstance(params, dict) else None) or []
    if landed and isinstance(incomplete, list) and isinstance(landed, list):
        reply["incomplete_chunks"] = sorted(set(incomplete) - set(landed))
    if isinstance(incomplete, list) and isinstance(landed, list):
        entangled = reply.get("entangled") or {}
        open_ids = (set(incomplete) - set(landed)) | set(entangled)
        reasons = {}
        for i in sorted(open_ids):
            if i in gated_ids:
                reasons[i] = "external_gate"
            elif i in entangled:
                reasons[i] = "entangled: imports stranded " + ", ".join(entangled[i])
            elif i in blockers:
                reasons[i] = f"executor_partial: {blockers[i]}" if blockers[i] else "executor_partial"
            else:
                reasons[i] = "incomplete_unreported"
        if reasons:
            reply["incomplete_reasons"] = reasons
    return reply


def _terminal_commit(
    params: dict, repo_root: Optional[Path], stranded: dict, scope: dict,
    gated_ids: Optional[set] = None, blockers: Optional[dict] = None,
) -> dict:
    if repo_root is None:
        return _error(
            "dispatch.terminal_commit requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    if isinstance(params, dict) and isinstance(params.get("task_output_path"), str):
        try:
            params = {**_params_from_task_output(params["task_output_path"]), **{
                k: v for k, v in params.items() if k != "task_output_path"}}
        except ValueError as exc:
            return _error(str(exc))

    refusal = validate_params("dispatch.terminal_commit", params, _PARAM_FIELDS)
    if refusal is not None:
        return _error(refusal["error"])

    script_path_raw = params["script_path"]
    incomplete_chunks = set(params["incomplete_chunks"]) - set(params.get("landed_chunks") or [])
    inline_review = params.get("inline_review")

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

    manifest_rel: Optional[str] = None
    request: Optional[CommitRequest] = None
    try:
        manifest_rel = parse_manifest_marker(script_text)
        if manifest_rel is not None:
            if any(ln.startswith(MARKER_PREFIX) for ln in script_text.splitlines()):
                return _error(
                    "script carries both an inline terminal-commit-request marker "
                    "and an ask-run-manifest marker; emit one",
                    refused="both-markers",
                )
            run_root = worktree_root / RUN_DIR_ROOT
            manifest_abs = contained_path(worktree_root / manifest_rel, [run_root])
            if manifest_abs is None:
                return _error(
                    f"ask-run-manifest path {manifest_rel!r} does not resolve under "
                    f"{RUN_DIR_ROOT}",
                    refused="manifest-escapes-run-root",
                )
            try:
                manifest = StageManifest.from_json(
                    json.loads(manifest_abs.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _error(f"cannot read ask-run manifest {manifest_rel!r}: {exc!r}")
            if gated_ids is not None:
                gated_ids.update(g.id for g in manifest.gated)
            request_abs = contained_path(worktree_root / manifest.marker_path, [run_root])
            if request_abs is None:
                return _error(
                    f"manifest marker_path {manifest.marker_path!r} does not resolve "
                    f"under {RUN_DIR_ROOT}",
                    refused="manifest-escapes-run-root",
                )
            try:
                request = parse_marker(request_abs.read_text(encoding="utf-8"))
            except OSError as exc:
                return _error(f"cannot read manifest marker_path {manifest.marker_path!r}: {exc}")
        else:
            request = parse_marker(script_text)
    except MalformedCommitRequestError as exc:
        return _error(f"malformed commit request: {exc}", refused="malformed-request")
    if request is None:
        request = _anchor_request(worktree_root, params, inline_review)
    if request is None:
        return {"committed": False, "nothing_to_commit": True}
    anchor_only = not request.chunks

    # Review at close is deterministic (PM ruling 2026-10-01): a run whose
    # result carries no review-stage output cannot land code, however the
    # script was composed or stripped.
    if inline_review is None:
        return _error(
            "params.inline_review is absent -- the run carries no review-stage "
            "output; re-emit with a review stage and re-fire",
            refused="unreviewed",
        )
    if not inline_review.get("integration_stem") or inline_review.get("slices") is None:
        return _error(
            "params.inline_review is missing integration_stem and/or slices -- "
            f"no review-stage output to land against (got: {inline_review!r})",
            refused="unreviewed",
        )

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
    record_abs: Optional[Path] = None
    record: Optional[dict] = None
    stage_returns = _stage_returns(inline_review) if inline_review is not None else None
    if (
        inline_review is not None
        and inline_review.get("plan_id")
        and isinstance(inline_review.get("wave_sidecar_paths"), list)
        and (inline_review.get("prep_sidecar") or stage_returns)
    ):
        if not session_id:
            return _error(
                "params.inline_review names a bookkeeping stem (wave_sidecar_paths/"
                "plan_id present) but params.session_id is missing or "
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
                prep_sidecar=inline_review.get("prep_sidecar"),
                record_stem=inline_review["integration_stem"],
                stage_returns=stage_returns,
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silently dropped
            return _error(f"review_mint.bookkeep_wave failed: {exc}")
        record_path_abs = record_abs = Path(record["sidecar_path"])
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
    stranded.update(
        {c.id: list(c.paths) for c in request.chunks if c.id in incomplete_chunks}
    )

    report_cache: dict = {}
    withheld_partial: dict = {}
    partial_chunks = _partial_chunks(worktree_root, request, incomplete_chunks, withheld_partial)
    partial_ids = {c.id for c in partial_chunks}
    entangled = _entangled_chunks(
        worktree_root, request, incomplete_chunks - partial_ids, report_cache
    )
    if entangled:
        incomplete_chunks = incomplete_chunks | set(entangled)
        partial_chunks = [c for c in partial_chunks if c.id not in entangled]
        stranded.update({c.id: list(c.paths) for c in request.chunks if c.id in entangled})
    done_chunks = [c for c in request.chunks if c.id not in incomplete_chunks]

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

    if withheld_partial and blockers is not None:
        blockers.update(withheld_partial)
    for chunk in partial_chunks:
        own_prefix_files = _own_prefix_files(worktree_root, chunk, report_cache) or []
        all_paths.extend(list(chunk.paths) + own_prefix_files)
        prefix_files.extend(own_prefix_files)

    if bookkeeping_record_path is not None and bookkeeping_record_path not in all_paths:
        all_paths.append(bookkeeping_record_path)

    if not all_paths:
        return {"committed": False, "nothing_to_commit": True}

    declared_writes = {p for c in done_chunks + partial_chunks for p in c.paths}
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

    scope.update(root=worktree_root, request=request)
    final_paths_set = set(all_paths)
    changed = _changed_paths(worktree_root, all_paths)

    def contributes(c) -> bool:
        def in_commit(p: str) -> bool:
            return p in final_paths_set and (changed is None or p in changed)

        return any(in_commit(p) or p in deleted_paths for p in list(c.paths)) or any(
            in_commit(p) for p in _own_prefix_files(worktree_root, c, report_cache) or []
        )

    contributing_chunks = [c for c in done_chunks if contributes(c)]
    contributing_partial = [c for c in partial_chunks if contributes(c)]
    for c in contributing_partial:
        stranded.pop(c.id, None)

    from datetime import datetime, timezone

    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit.completion_receipt import (
        _remove,
        build_run_receipts,
        write_run_receipts,
    )

    receipt_paths: list = []
    receipts_built: list = []
    try:
        receipts_built = [] if anchor_only else build_run_receipts(
            worktree_root,
            request,
            done_ids=[c.id for c in done_chunks],
            incomplete_ids=sorted(incomplete_chunks),
            record=record,
            script_path=script_path_raw,
            session_id=session_id,
            base_sha=head_sha(worktree_root),
            branch=observed_branch,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        receipt_paths = write_run_receipts(worktree_root, receipts_built) if receipts_built else []
    except Exception as exc:  # noqa: BLE001 -- write_run_receipts already removed its partial writes
        return _error(f"completion receipt write failed: {exc}", refused="receipt-write-failed")
    all_paths.extend(receipt_paths)

    subject = (
        f"review anchor: {request.plan_path} -- delivery PASS, no product files"
        if anchor_only
        else _subject(
            contributing_chunks + contributing_partial,
            f"review trail: {inline_review['integration_stem']}",
        )
    )
    if not subject.strip(" :"):
        _remove(worktree_root, receipt_paths)
        return _error(
            f"terminal commit subject {subject!r} is empty; chunk ids and the "
            "integration stem are both blank",
            refused="empty-subject",
        )
    paragraphs = [subject]
    if deleted_paths:
        # The undeclared-staged-deletion guard reads the message for a removal verb.
        # Its own paragraph: a non-trailer line inside the trailer block makes git
        # stop parsing the block, and review_stamp keys on the Inline-Review trailer.
        paragraphs.append("Removes declared write(s): " + ", ".join(deleted_paths))
    trailers = []
    if request.deliverable_id:
        trailers.append(f"Deliverable-Id: {request.deliverable_id}")
    trailers.append(
        f"Inline-Review: applies {inline_review['integration_stem']} -- execute-review: "
        f"{inline_review['slices']} slices, {inline_review.get('fixes')} fixes"
    )
    paragraphs.append("\n".join(trailers))
    message = "\n\n".join(paragraphs)

    def commit_v2(params: dict, root: Path):
        return reentrant_dispatch("ceremony.commit_v2", params, repo_root=root)

    commit_params: dict = {"paths": all_paths, "message": message}
    if bookkeeping_record_path is not None and bookkeeping_record_path in all_paths:
        # The review record lives under the gitignored subagent-share dir and is tracked by design.
        commit_params["force_ignored"] = [bookkeeping_record_path]
    if deleted_paths:
        commit_params["deleted_paths"] = deleted_paths
        # A DONE chunk's own declared write that is now absent is a planned
        # deletion; when it removes a file added since the rollback window
        # opened, commit_v2's staged-rollback check reads it as restoring the
        # older absence and refuses the run.
        commit_params["declared_reverts"] = deleted_paths
    if session_id is not None:
        commit_params["session_id"] = session_id

    try:
        reply = commit_v2(commit_params, repo_root)
    except OpUnavailableError:
        _remove(worktree_root, receipt_paths)
        return _error("ceremony.commit_v2 is not registered")
    except BaseException:
        _remove(worktree_root, receipt_paths)
        raise
    if not isinstance(reply, dict):
        reply = {"committed": False, "sha": None, "error": f"unexpected commit_v2 reply: {reply!r}"}

    reply = dict(reply)
    if not reply.get("committed"):
        _remove(worktree_root, receipt_paths)
        receipt_paths = []
    if reply.get("committed") and reply.get("sha"):
        # Advance plan status so a re-fire (which selects live `open` rows)
        # cannot redo landed work. incomplete_chunks never reach here.
        no_delta = set(reply.get("no_delta") or [])
        coded_chunks = [
            c for c in contributing_chunks
            if any(
                p in deleted_paths or p not in no_delta
                for p in list(c.paths) + (_own_prefix_files(worktree_root, c, report_cache) or [])
                if p in final_paths_set or p in deleted_paths
            )
        ]
        if len(coded_chunks) != len(contributing_chunks):
            kept = {c.id for c in coded_chunks}
            reply["no_product_hunk"] = [c.id for c in contributing_chunks if c.id not in kept]
        source_rows = (
            {request.plan_path: set()}
            if anchor_only
            else _source_rows_by_plan(
                worktree_root, request.plan_path, [c.id for c in coded_chunks]
            )
        )
        # Before the coded stamp: that commit carries the plan, so a stamp
        # minted here lands in it and costs no commit of its own.
        if record_abs is not None and record is not None and request.plan_path in source_rows:
            reply.update(
                _mint_review_stamp(
                    worktree_root, request.plan_path, str(reply["sha"]), record_abs, record
                )
            )
        reply.update(
            _stamp_coded_commit(
                commit_v2, worktree_root, repo_root, source_rows,
                str(reply["sha"]), session_id,
                also_commit=(request.plan_path,) if reply.get("review_stamp") == "minted" else (),
            )
        )
        coded_stale = reply.pop("coded_index_stale", [])
        if coded_stale:
            reply["index_stale"] = sorted(set(reply.get("index_stale") or []) | set(coded_stale))
        if (
            request.plan_path
            and reply.get("review_stamp") == "minted"
            and not incomplete_chunks
            and reply.get("coded_sha")
        ):
            reply.update(_stamp_plan_implemented(worktree_root, request.plan_path, str(reply["sha"])))
    reply["receipts"] = receipt_paths
    reply["receipt_coverage"] = "written" if receipt_paths else "unidentified"
    if (
        receipt_paths
        and reply.get("plan_status", "implemented") != "implemented"
        and any(fm.get("verdict") == "agent-delivered" for fm, _prose in receipts_built)
    ):
        reply["receipt_divergence"] = (
            f"receipt verdict agent-delivered beside plan_status {reply['plan_status']!r}"
        )
    reply["branch_check"] = branch_check
    reply["chunks_committed"] = [c.id for c in contributing_chunks]
    no_hunk = [c for c in done_chunks if c not in contributing_chunks and c.id not in reply.get("no_product_hunk", [])]
    if no_hunk:
        reply["no_product_hunk"] = sorted(set(reply.get("no_product_hunk", [])) | {c.id for c in no_hunk})
    if entangled:
        reply["entangled"] = entangled
    reply["partial_committed"] = [c.id for c in contributing_partial]
    reply["dropped_absent"] = dropped_absent
    reply["deleted_paths"] = deleted_paths
    reply["unmarked_incomplete"] = unmarked_incomplete
    reply["prefix_files"] = prefix_files
    return reply
