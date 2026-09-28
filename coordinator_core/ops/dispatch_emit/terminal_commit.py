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
  - Does NOT declare a deletion to ``commit_v2``: this op never passes
    ``deleted_paths``. A path this run's own paths list that has since gone
    missing from the worktree is either dropped (untracked at HEAD --
    nothing to declare) or refuses the WHOLE commit (tracked at HEAD -- an
    undeclared deletion this op has no contract to authorize).
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

from coordinator_core.git.commit import partition_declared_deletions
from coordinator_core.git.commit_trailers import _UUID_RE
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.commit_request import (
    PREFIX_CLAIM_LABEL,
    CommitRequest,
    parse_marker,
)
from coordinator_core.ops.fleet._common import main_worktree_root


def _error(message: str, **extra: object) -> dict:
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


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
    ``prefix_files`` (own-prefix-claimed files folded into the commit). A
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

    known_ids = {chunk.id for chunk in request.chunks}
    unknown_incomplete = incomplete_chunks - known_ids
    if unknown_incomplete:
        return _error(
            "params.incomplete_chunks names id(s) the terminal-commit-request "
            f"marker does not carry: {sorted(unknown_incomplete)}"
        )

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

    if not all_paths:
        return {"committed": False, "nothing_to_commit": True}

    absent = [p for p in all_paths if not (worktree_root / p).exists()]
    dropped_absent: list = []
    if absent:
        partition = partition_declared_deletions(worktree_root, absent)
        if partition is None:
            return _error(
                "cannot resolve HEAD tree spine to classify absent path(s) "
                f"{absent}"
            )
        tracked_at_head, absent_from_head = partition
        if tracked_at_head:
            return _error(
                "path(s) declared by a DONE chunk are gone from the worktree "
                f"but still tracked at HEAD, an undeclared deletion: {tracked_at_head}"
            )
        dropped_absent = list(absent_from_head)
        dropped_set = set(dropped_absent)
        all_paths = [p for p in all_paths if p not in dropped_set]

    if not all_paths:
        return {"committed": False, "nothing_to_commit": True}

    final_paths_set = set(all_paths)
    contributing_chunks = [
        c
        for c in done_chunks
        if any(p in final_paths_set for p in list(c.paths)) or any(
            p in final_paths_set
            for p in _own_prefix_files(worktree_root, c, report_cache) or []
        )
    ]

    message_lines = [_subject(contributing_chunks)]
    if request.deliverable_id:
        message_lines.append(f"Deliverable-Id: {request.deliverable_id}")
    if inline_review is not None:
        stem = inline_review.get("integration_stem")
        slices = inline_review.get("slices")
        fixes = inline_review.get("fixes")
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
    if session_id is not None:
        commit_params["session_id"] = session_id

    reply = commit_v2(commit_params, repo_root)
    if not isinstance(reply, dict):
        reply = {"committed": False, "sha": None, "error": f"unexpected commit_v2 reply: {reply!r}"}

    reply = dict(reply)
    reply["chunks_committed"] = [c.id for c in contributing_chunks]
    reply["dropped_absent"] = dropped_absent
    reply["prefix_files"] = prefix_files
    return reply
