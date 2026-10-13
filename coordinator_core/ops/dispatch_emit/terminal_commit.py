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
  - Carries exactly one undeclared file: the ``state/sizings/`` sizing the
    plan (or minted spine) cites as ``sizing_object:``, which the ask arm
    rewrites mid-run. Never for an anchor-only run, never outside that dir.
  - A warp ask run (its script carries the ask-run-manifest marker) lands
    PRODUCT ONLY: its declared writes plus docs output (``docs/delivery/``, a
    wiki, a README, and the cited sizing's ``doc_consumers``/``ask_conflicts``). Its process records -- sizing, plan, review record,
    sidecars, completion receipts -- stay on disk (receipts under
    ``.coordinator-local/``) and are named by trailers instead
    (``Completion-Receipt``, ``Review``, ``Falsifier``, ``Resolves-Sizing``);
    the run makes no second, plan-only commit. When the run dir holds a
    requirements ledger, the commit refuses unless ``ledger_verdict`` passes
    ``requirements_ledger.judge`` against the live ledger, in addition to
    the delivery, criterion and falsifier gates at every size. A docs path
    rides the commit only if ``params.doc_writes`` declares it.
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
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.git.git_state import head_branch
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.claimed_write import replace_text
from coordinator_core.session.record_homes import home_dir
from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch
from coordinator_core.ops.dispatch_emit import gate_owed  # noqa: E402 -- sibling-package group
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    PREFIX_CLAIM_LABEL,
    ChunkCommit,
    CommitRequest,
    MalformedCommitRequestError,
    parse_manifest_marker,
    parse_marker,
    plan_deliverable_id,
    valid_deliverable_id,
)
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params


_PARAM_FIELDS = (
    Field("script_path", "nonempty_str", required=True),
    Field("incomplete_chunks", "str_list", required=True),
    Field("landed_chunks", "str_list"),
    Field("inline_review", "dict"),
    Field("task_output_path", "nonempty_str"),
    Field("plan_path", "nonempty_str"),
    Field("falsifier_broken", "list"),
    Field("held", "list"),
    Field("run_id", "nonempty_str"),
    Field("run_token", "nonempty_str"),
    Field("doc_paths", "str_list"),
    Field("doc_writes", "str_list"),
    Field("ledger_verdict", "dict"),
)

#: Where a run's docs stage may write and have the terminal commit carry it: product docs and
#: the run's delivery note, never run residue under `scratch/`.
_DOC_PATH_PREFIX = "docs/"
_DOC_SUFFIX = ".md"
#: An ask run's product commit admits only these docs: the delivery note, a wiki, a README.
_ASK_DOC_PREFIXES = ("docs/delivery/", "docs/wiki/", "wiki/")
_PYTEST_NODE = re.compile(r"[\w./-]+\.py::[\w.\[\]-]+(?:::[\w.\[\]-]+)*")


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


#: Refusals raised before the run's incomplete set is known: ``stranded`` would
#: read ``{}`` ("nothing left behind") when it is really unknown, so the key is omitted.
_STRANDED_UNKNOWN_REFUSALS = frozenset({"missing-run-outcome", "unreviewed"})


def _missing_run_outcome(params: object) -> Optional[dict]:
    """Refusal naming every run-outcome param the caller omitted, else ``None``.

    The emitted script's marker records what the run promised (chunks, paths),
    never how it ended: ``incomplete_chunks`` and the review-stage
    ``inline_review`` exist only in the run's result digest
    (``next_action.params``), so a bare ``script_path`` cannot supply them.
    Deriving them from chunk reports would land code the review wave never saw,
    which the deterministic-review ruling forbids. Checked once, ahead of the
    per-field validation, so the caller learns every gap in one reply.
    """
    if not isinstance(params, dict) or not isinstance(params.get("script_path"), str):
        return None
    absent = [
        name for name in ("incomplete_chunks", "inline_review") if params.get(name) is None
    ]
    if "incomplete_chunks" not in absent:
        return None
    return _error(
        f"params {absent} not passed. The run outcome is not derivable from "
        f"script_path {params['script_path']!r} (it carries only the commit-request marker: "
        "chunks and declared paths) and no params.task_output_path was given. Pass "
        "task_output_path=<the run's task-output file> (its next_action.params supplies "
        "incomplete_chunks and inline_review), or pass incomplete_chunks and inline_review "
        "from the digest's terminal_commit_cli line. A run that died before its review "
        "stage has no inline_review to pass: re-emit it review-only (emit-dispatch-workflow "
        "--inventory <inv> --review-only --rows <DONE ids> --run-base <sha>) and land from "
        "that run's terminal_commit_cli line",
        refused="missing-run-outcome",
        missing=absent,
    )


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


_XS_SPINE_ORIGIN = "sizing object"
_KIND_FIELD = re.compile(r"^(derived_from|scope_mode)[ \t]*:[ \t]*(.*?)[ \t]*$", re.M)


def _close_kind(plan_text: Optional[str]) -> tuple[str, bool]:
    """``(close kind, plan_governs)`` read off the run's plan path frontmatter.

    A minted inventory spine is ``mise-run``; a minted XS spine (``derived_from: sizing object``)
    is ``xs-s-run`` with no governing plan; an S ``scope_mode: spec-dispatch`` plan is ``xs-s-run``
    with one. Anything else, or no readable plan, is ``plan-run``. Values are matched after
    dropping a trailing `` # comment`` and quotes; the text is LF-normalised first.
    """
    if plan_text is None:
        return "plan-run", True
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(plan_text.replace("\r\n", "\n"))
    if split is None:
        return "plan-run", True
    fields: dict = {}
    for name, raw in _KIND_FIELD.findall(split.fm_text):
        fields.setdefault(name, re.sub(r"[ \t]+#.*$", "", raw).strip().strip("\"'"))
    origin = fields.get("derived_from")
    if origin == _MINTED_SPINE_ORIGIN:
        return "mise-run", False
    if origin == _XS_SPINE_ORIGIN:
        return "xs-s-run", False
    if fields.get("scope_mode") == "spec-dispatch":
        return "xs-s-run", True
    return "plan-run", True


def _cited_sizing(worktree_root: Path, plan_path: Optional[str]) -> Optional[str]:
    """Worktree-relative path of the sizing the run's plan or minted spine cites
    as ``sizing_object:``, when it is an existing file under ``state/sizings/``.

    The warp ask arm rewrites that sizing during the run (``status: routed``, the
    ``plan:`` link) and no chunk declares it, so the commit carries it by this
    citation. In-process reads only: no spawn.
    """
    if not plan_path:
        return None
    text = _read_rel(worktree_root, plan_path)
    if text is None:
        return None
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    split = split_frontmatter(text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    if not cited:
        return None
    return _sizing_file_rel(worktree_root, cited)


def _sizing_file_rel(worktree_root: Path, cited: str) -> Optional[str]:
    guarded = contained_path(worktree_root / cited, [Path(home_dir(str(worktree_root), "sizings"))])
    if guarded is None or not guarded.is_file():
        return None
    return guarded.relative_to(worktree_root.resolve()).as_posix()


def _register_target(
    worktree_root: Path, plan_path: Optional[str]
) -> tuple[Optional[str], frozenset, bool]:
    """``(register_sizing_rel, claimed_row_ids, is_own)``: the register sizing is the plan's
    ``register_claims.sizing`` when it names an existing sizing, else the cited ``sizing_object``;
    ``is_own`` is True when that is the plan's own ``sizing_object``."""
    text = _read_rel(worktree_root, plan_path) if plan_path else None
    if text is None:
        return None, frozenset(), True
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops import requirement_register as rr

    split = split_frontmatter(text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    own = _sizing_file_rel(worktree_root, cited) if cited else None
    claims_rel, ids = rr.plan_claims(split.fm_text) if split else (None, frozenset())
    target = _sizing_file_rel(worktree_root, claims_rel) if claims_rel else None
    if target is None:
        return own, ids, True
    return target, ids, target == own


def _register_gate(worktree_root: Path, plan_path: Optional[str], inline_review: dict) -> Optional[str]:
    """Demote a ``met`` criterion to ``not_met`` when a row this plan claims in its
    cited register-bearing sizing is not judged met, wired, on its register surface
    with an observed entry point. Returns the failure reason (also set on the criterion),
    or ``None`` when the gate does not apply or every claimed row passes. Reads the
    judge's returned ``criterion.register_rows`` and the register on disk; never inspects code.
    """
    criterion = inline_review.get("criterion")
    if not plan_path or not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None
    sizing_rel, claim_ids, is_own = _register_target(worktree_root, plan_path)
    if sizing_rel is None:
        return None
    from coordinator_core.ops import requirement_register as rr

    sizing_text = _read_rel(worktree_root, sizing_rel)
    register = rr.read_register(sizing_text) if sizing_text is not None else None
    gaps = rr.uncovered_turns(sizing_text) if is_own and sizing_text is not None else []
    if register is None and not gaps:
        return None
    claimed = rr.claimed_rows(register, plan_path, claim_ids) if register is not None else []
    judged = {
        str(r.get(rr.ROW_ID)): r
        for r in (criterion.get(rr.JUDGE_ROWS_KEY) or [])
        if isinstance(r, dict) and r.get(rr.JUDGE_CLAIM, rr.CLAIM_THIS_PLAN) == rr.CLAIM_THIS_PLAN
    }
    for row in claimed:
        rid, surface = row.get(rr.ROW_ID), row.get(rr.ROW_SURFACE)
        got = judged.get(str(rid))
        if got is None:
            why = "not judged"
        elif got.get(rr.ROW_STATUS) != rr.STATUS_MET:
            why = f"judged status {got.get(rr.ROW_STATUS)!r}, not met"
        elif got.get(rr.ROW_WIRED) is not True:
            why = "judged wired: false"
        elif got.get(rr.ROW_SURFACE) != surface:
            why = f"judged surface {got.get(rr.ROW_SURFACE)!r} differs from register surface {surface!r}"
        elif not str(got.get(rr.JUDGE_OBSERVED_REF) or "").strip():
            why = "no observed entry point (observed_ref is empty)"
        else:
            continue
        reason = f"register row {rid} (surface {surface}): {why}"
        criterion["status"] = "not_met"
        criterion["reason"] = reason
        criterion["observation"] = reason
        return reason
    if gaps:
        turn, head = gaps[0]
        reason = f"PM turn {turn} is covered by no register row or unrelated_turns entry: {head}"
        criterion["status"] = "not_met"
        criterion["reason"] = reason
        criterion["observation"] = reason
        return reason
    if is_own and register is not None:
        orphan = rr.blocking_unclaimed(register, [r.get(rr.ROW_ID) for r in claimed])
        if orphan is not None:
            reason = f"register row {orphan}: owed and claimed by no plan"
            criterion["status"] = "not_met"
            criterion["reason"] = reason
            criterion["observation"] = reason
            return reason
    return None


def _register_writeback(
    worktree_root: Path, plan_path: Optional[str], inline_review: Optional[dict], sha: str,
) -> dict:
    """Cascade the judge's per-row verdicts into the cited sizing's register and recompute its
    rollup, in place; the caller adds ``sizing`` to the coded-stamp commit's paths.

    Returns ``{}`` when nothing applies, ``{"register_sizing": rel}`` on a write, or
    ``{"register_writeback_error": reason}`` when the result fails the sizing schema (the file is
    left untouched). Never raises: the run's own commit has already landed.
    """
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    if not plan_path or not isinstance(criterion, dict):
        return {}
    from coordinator_core.ops import requirement_register as rr

    judged = [
        r for r in (criterion.get(rr.JUDGE_ROWS_KEY) or [])
        if isinstance(r, dict) and r.get(rr.JUDGE_CLAIM, rr.CLAIM_THIS_PLAN) == rr.CLAIM_THIS_PLAN
    ]
    sizing_rel, claim_ids, _ = _register_target(worktree_root, plan_path) if judged else (None, frozenset(), True)
    if sizing_rel is None:
        return {}
    from datetime import datetime, timezone

    import yaml

    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    schema_path = Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
    now = datetime.now(timezone.utc)

    def mutate(old: str) -> str:
        if rr.read_register(old) is None:
            raise MutateAbort("")
        new = rr.apply_verdicts(old, judged, plan_path, sha, now, claim_ids)
        if new == old:
            raise MutateAbort("")
        try:
            errors = validate_frontmatter(yaml.safe_load(new) or {}, schema_path)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
            raise MutateAbort(f"register write-back refused: {exc}")
        if errors:
            raise MutateAbort(f"register write-back refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(worktree_root / sizing_rel, mutate, repo_root=worktree_root)
    except MutateAbort as exc:
        reason = str(exc.args[0]) if exc.args else ""
        return {"register_writeback_error": reason} if reason else {}
    except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O: refuse, keep the commit
        return {"register_writeback_error": f"register write-back refused: {exc!r}"}
    return {"register_sizing": sizing_rel}


def _spine_row_ids(plan_text: str) -> Optional[set]:
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

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


def _inventory_record_rel(worktree_root: Path, spine_rel: str) -> Optional[tuple]:
    """``(inventory record rel, its text)`` for a minted spine, else None. A part spine
    (``-pN``) falls back to the unsplit record."""
    if not spine_rel.endswith(_SPINE_SUFFIX):
        return None
    stem = spine_rel[: -len(_SPINE_SUFFIX)]
    inventory_rel = stem + ".md"
    text = _read_rel(worktree_root, inventory_rel)
    if text is None and re.search(r"-p\d+$", stem):
        inventory_rel = re.sub(r"-p\d+$", "", stem) + ".md"
        text = _read_rel(worktree_root, inventory_rel)
    return None if text is None else (inventory_rel, text)


def _mint_mise_receipt(
    worktree_root: Path, spine_rel: str, sha: str, record_abs: Path, record: dict,
    tree: Optional[str] = None,
) -> dict:
    """Mint the receipt onto the mise inventory record behind ``spine_rel``. Returns
    ``{"review_receipt": rel}`` on success, ``{"review_stamp": "refused", ...}`` on a refusal,
    ``{}`` when no record resolves. The product commit stands either way."""
    found = _inventory_record_rel(worktree_root, spine_rel)
    if found is None:
        return {}
    inventory_rel = found[0]
    guarded = contained_path(worktree_root / inventory_rel, [worktree_root])
    if guarded is None:
        return {}
    from coordinator_core.ops.review_stamp import MintRefusal, mint_receipt

    try:
        mint_receipt(guarded, worktree_root, resolved=(sha, record_abs, record), terminal_tree_sha=tree)
    except MintRefusal as exc:
        return {"review_stamp": "refused", "review_stamp_refusal": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"review_stamp": "refused", "review_stamp_refusal": repr(exc)}
    return {"review_receipt": inventory_rel}


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
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops.dispatch_emit.inventory_mint import (
        InventoryMintError,
        _bare_plan_row_id,
        _resolve_spec_plan_path,
        parse_chunk_table,
    )

    marker_text = _read_rel(worktree_root, plan_path)
    if marker_text is None:
        return {}
    # Read-only parses below are LF-shaped; a CRLF checkout must map the same.
    marker_text = marker_text.replace("\r\n", "\n")
    split = split_frontmatter(marker_text)
    origin = read_fm_field_unquoted(split.fm_text, "derived_from") if split else None
    if origin != _MINTED_SPINE_ORIGIN:
        return {plan_path: set(chunk_ids)}

    found = _inventory_record_rel(worktree_root, plan_path)
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


#: Per-plan seam-check sidecar suffix; `plan.seam_record` writes `<plan-stem>` + this beside the plan.
_SEAM_SIDECAR_SUFFIX = ".seam.yaml"


def _seam_sidecars(worktree_root: Path, plan_path: Optional[str], chunk_ids: list) -> list:
    """Worktree-relative `.seam.yaml` sidecars on disk beside the run's plan and each
    source plan its committed rows came from, in sorted order."""
    if not plan_path:
        return []
    plans = {plan_path, *_source_rows_by_plan(worktree_root, plan_path, chunk_ids)}
    found = []
    for plan in sorted(plans):
        rel = str(PurePosixPath(plan).with_name(PurePosixPath(plan).stem + _SEAM_SIDECAR_SUFFIX))
        if (worktree_root / rel).is_file():
            found.append(rel)
    return found


def _memo_rows_without_receipt(worktree_root: Path, request: CommitRequest) -> dict:
    """``{row id: slug}`` of the plan's open memo-send rows whose sender-side receipt is absent.

    A memo row is an EM act, never dispatched; it closes on
    ``memo-outbox/sent/<slug>.md`` existing. Unreadable plan or spine: empty.
    """
    if not request.plan_path:
        return {}
    text = _read_rel(worktree_root, request.plan_path)
    if text is None:
        return {}
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.spine_read import (
        _is_memo_send_row,
        memo_receipt_exists,
        memo_row_slug,
    )

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return {}
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError:
        return {}
    out: dict = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("performer") == "em":
            continue
        if (row.get("disposition") or "open") != "open" or not _is_memo_send_row(row):
            continue
        slug = memo_row_slug(row)
        if slug is not None and not memo_receipt_exists(worktree_root, slug):
            out[str(row["id"])] = slug
    return out


def _row_rank(row: dict) -> int:
    from coordinator_core.frontmatter.schema_validate import (
        _PLAN_TASKS_GROUPING_ORDER,
        _plan_tasks_row_grouping,
    )

    return _PLAN_TASKS_GROUPING_ORDER.index(_plan_tasks_row_grouping(row))


_NOOP_DETAIL = (
    "resolved as a no-op: the row's files carry no change from this run "
    "(already landed, or a conditional no-change outcome); closed against the run's commit"
)


def _flip_rows_coded(plan_text: str, row_ids: set, sha: str, details: Optional[dict] = None) -> tuple:
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
        new_text, flipped = _flip_rows_coded(plan_text.replace("\r\n", "\n"), row_ids, sha, details)
        return (new_text.replace("\n", "\r\n"), flipped) if flipped else (plan_text, [])
    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

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
            return _row_rank(row)

        chunks = sorted(chunks, key=rank)
        lines = head + [line for _cid, chunk in chunks for line in chunk]

    new_body = "".join(lines)
    if not ended_with_newline:
        new_body = new_body[:-2] if new_body.endswith("\r\n") else new_body[:-1]
    return plan_text[:start] + new_body + plan_text[end:], sorted(targets)


_CHECKPOINT_SUBJECT = re.compile(r"^checkpoint\(wave [^)]*\): \d+ rows? — (?P<ids>.+)$")
_CHECKPOINT_PLAN_LINE = re.compile(r"^Checkpoint-Plan: (?P<v>\S.*?)\s*$", re.M)


def _checkpoint_attribution(worktree_root: Path, terminal_sha: str, plan_path: Optional[str], chunks: list) -> dict:
    """Chunk id -> sha of the run's own ``checkpoint(wave N)`` commit that landed its writes.

    Walks first-parent history in-process (zero spawns) from the terminal commit's
    parent while the subject is a checkpoint of this plan (``Checkpoint-Plan:``
    trailer), so the walk is bounded by the run. A commit lands a chunk's file when
    its blob differs from its parent's. Among the commits touching a chunk, one whose
    subject names the chunk id wins; otherwise the earliest. A chunk no checkpoint
    touched is absent: it stays a no-op row.
    """
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    meta = commit_meta(common_dir, terminal_sha)
    parents = (meta or {}).get("parents") or []
    sha = parents[0] if parents else None
    checkpoints: list = []  # newest first: (sha, parent, named ids)
    for _ in range(500):
        meta = commit_meta(common_dir, sha) if sha else None
        if meta is None:
            break
        message = meta.get("message") or ""
        subject = _CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
        plan = _CHECKPOINT_PLAN_LINE.search(message)
        if not subject or not meta.get("parents") or (plan_path and (not plan or plan.group("v") != plan_path)):
            break
        named = {i.strip() for i in subject.group("ids").split(",") if i.strip()}
        checkpoints.append((sha, meta["parents"][0], named))
        sha = meta["parents"][0]
    out: dict = {}
    for chunk in chunks:
        hits = [
            (csha, named) for csha, parent, named in reversed(checkpoints)
            if any(_blob_at_commit(common_dir, csha, p) != _blob_at_commit(common_dir, parent, p) for p in chunk.paths)
        ]
        if hits:
            out[chunk.id] = next((h[0] for h in hits if chunk.id in h[1]), hits[0][0])
    return out


# Commits a review-only run walks back from the terminal sha to reach its run base.
_REVIEW_WALK_CAP = 500


def _open_row_ids(worktree_root: Path, plan_rel: str) -> set:
    """Ids of the plan spine rows whose disposition is ``open`` on disk now."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition

    text = _read_rel(worktree_root, plan_rel)
    if text is None:
        return set()
    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return set()
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError:
        return set()
    if not isinstance(rows, list):
        return set()
    return {
        str(r.get("id")) for r in rows
        if isinstance(r, dict) and _row_disposition(r) == "open"
    }


def _review_only_coding(
    worktree_root: Path, terminal_sha: str, request: CommitRequest, inline_review: dict,
) -> tuple:
    """``({sha: {plan: {row id}}}, [row ids with no delta])`` for a review-only run.

    A review row is coded only when the marker recorded it ``open`` at emit, it is still
    ``open`` on disk, the inline review and judge passed, and some commit in
    ``run_base..terminal_sha`` changes a blob in its writes; it closes at the newest such
    commit. An open row no commit touched is reported, never closed. The walk is
    in-process (zero spawns) along first parents; a run base it never reaches (past the
    walk cap, on a merged-in branch, rewritten away) attributes nothing, and every
    still-open reviewed row then lands in the no-delta list.
    """
    if (
        not request.review_open_rows
        or not request.review_run_base
        or _reviewed_files_if_passed(inline_review) is None
    ):
        return {}, []
    criterion = inline_review.get("criterion")
    if isinstance(criterion, dict) and criterion.get("status") == "not_met":
        return {}, []
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    base = request.review_run_base
    delta: list = []  # newest first: (sha, parent)
    sha = terminal_sha
    reached = False
    for _ in range(_REVIEW_WALK_CAP):
        if sha.startswith(base):
            reached = True
            break
        meta = commit_meta(common_dir, sha)
        if meta is None or not meta.get("parents"):
            break
        delta.append((sha, meta["parents"][0]))
        sha = meta["parents"][0]
    if not reached:
        delta = []

    open_now: dict = {}
    coded: dict = {}
    no_delta: list = []
    wanted = set(request.review_open_rows)
    for chunk in request.chunks:
        if chunk.id not in wanted:
            continue
        by_plan = _source_rows_by_plan(worktree_root, request.plan_path, [chunk.id])
        live = {
            plan_rel: ids for plan_rel, ids in by_plan.items()
            if ids & open_now.setdefault(plan_rel, _open_row_ids(worktree_root, plan_rel))
        }
        if not live:
            continue
        hit = next(
            (
                csha for csha, parent in delta
                if any(
                    _blob_at_commit(common_dir, csha, p) != _blob_at_commit(common_dir, parent, p)
                    for p in chunk.paths
                )
            ),
            None,
        )
        if hit is None:
            no_delta.append(chunk.id)
            continue
        for plan_rel, ids in live.items():
            coded.setdefault(hit, {}).setdefault(plan_rel, set()).update(ids)
    return coded, no_delta


def _is_ask_doc(rel: str) -> bool:
    return rel.endswith(_DOC_SUFFIX) and (
        rel.startswith(_ASK_DOC_PREFIXES) or PurePosixPath(rel).name.upper().startswith("README")
    )


_DOC_TARGET_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|#.*)$")


def _sizing_doc_targets(worktree_root: Path, plan_path: Optional[str]) -> frozenset:
    """Paths the cited sizing names in ``doc_consumers`` and ``ask_conflicts`` (a ``:<line>`` or
    ``#<anchor>`` suffix stripped): the docs stage's mandate beyond README and wiki pages.
    A sizing or plan path is never a target -- those stay process records. In-process only."""
    sizing_rel = _cited_sizing(worktree_root, plan_path)
    text = _read_rel(worktree_root, sizing_rel) if sizing_rel else None
    if text is None:
        return frozenset()
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return frozenset()
    if not isinstance(doc, dict):
        return frozenset()
    raw = [p for p in doc.get("doc_consumers") or [] if isinstance(p, str)]
    raw += [c.get("path") for c in doc.get("ask_conflicts") or [] if isinstance(c, dict) and isinstance(c.get("path"), str)]
    process = (home_dir(".", "sizings"), "docs/plans/")
    out = set()
    for entry in raw:
        rel = PurePosixPath(_DOC_TARGET_SUFFIX.sub("", entry.strip()).replace("\\", "/")).as_posix()
        if rel and not any(rel.startswith(PurePosixPath(h).as_posix().rstrip("/") + "/") for h in process):
            out.add(rel)
    return frozenset(out)


def _fold_doc_paths(
    worktree_root: Path, doc_paths: list, plan_path: Optional[str], session_id: Optional[str],
    product_only: bool = False, doc_writes: Optional[list] = None,
) -> tuple:
    """(kept, dropped) of a docs stage's reported writes: a kept path is an existing ``.md``
    under ``docs/`` (or the run's own plan), inside the worktree, and held by no peer's touch
    claim; for an ask run (``product_only``) only a delivery note, wiki page, README, or a
    path the cited sizing names in ``doc_consumers``/``ask_conflicts``, and only when it is also in
    ``doc_writes`` (the docs stage's declared set). In-process only: no spawn."""
    kept: list = []
    dropped: list = []
    targets = _sizing_doc_targets(worktree_root, plan_path) if product_only else frozenset()
    declared = {PurePosixPath(p.replace("\\", "/")).as_posix() for p in doc_writes or ()}
    for raw in doc_paths:
        rel = PurePosixPath(raw.replace("\\", "/")).as_posix()
        admitted = (
            rel in declared and (_is_ask_doc(rel) or rel in targets)
        ) if product_only else (
            rel == plan_path or (rel.startswith(_DOC_PATH_PREFIX) and rel.endswith(_DOC_SUFFIX))
        )
        ok = (
            not PurePosixPath(rel).is_absolute()
            and ".." not in PurePosixPath(rel).parts
            and admitted
            and contained_path(worktree_root / rel, [worktree_root]) is not None
            and (worktree_root / rel).is_file()
        )
        (kept if ok else dropped).append(rel)
    peer = _peer_held(worktree_root, session_id, kept)
    return [p for p in kept if p not in peer], dropped + sorted(peer)


#: The engine's resolution ledger: one row per sizing a run landed whole, never committed.
SIZING_LEDGER_REL = ".coordinator-local/sizings.jsonl"


def _resolving_sizing(worktree_root: Path, plan_path: Optional[str]) -> Optional[dict]:
    """``{rel, id, tshirt, ask_sha256}`` for the run's cited sizing, else None. In-process reads."""
    rel = _cited_sizing(worktree_root, plan_path)
    text = _read_rel(worktree_root, rel) if rel else None
    if text is None:
        return None
    import hashlib

    import yaml

    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    estimate = doc.get("estimate") if isinstance(doc.get("estimate"), dict) else {}
    words = "\n\n".join(
        str(v.get("text")) for v in doc.get("pm_verbatims") or [] if isinstance(v, dict) and v.get("text")
    ) or str(doc.get("intent") or "")
    return {
        "rel": rel,
        "id": doc.get("deliverable_id") or PurePosixPath(rel).stem,
        "tshirt": estimate.get("tshirt") or "?",
        "route": doc.get("route"),
        "ask_sha256": hashlib.sha256(words.encode("utf-8")).hexdigest(),
    }


def _record_resolution(worktree_root: Path, resolves: dict, sha: str) -> dict:
    """Append the sizing's resolution row (``{id, tshirt, route, ask_sha256, resolved_sha, ts,
    sizing}``) to ``SIZING_LEDGER_REL``. Never raises: the product commit stands."""
    from datetime import datetime, timezone

    row = {
        "id": resolves["id"],
        "tshirt": resolves["tshirt"],
        "route": resolves["route"],
        "ask_sha256": resolves["ask_sha256"],
        "resolved_sha": sha,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sizing": resolves["rel"],
    }
    try:
        from coordinator_core.atomic_append import append_line

        sink = worktree_root / SIZING_LEDGER_REL
        sink.parent.mkdir(parents=True, exist_ok=True)
        append_line(sink, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"sizing_ledger_error": repr(exc)}
    return {"sizing_resolved": row}


def _ship_cited_sizing(worktree_root: Path, repo_root: Path, plan_path: Optional[str]) -> dict:
    """Flip the run's cited sizing to ``status: shipped`` through ``sizing.ship`` once its work
    has landed whole. ``{"sizing_shipped": rel}`` on a write, ``{}`` when nothing applies or it
    was already shipped, ``{"sizing_ship_skipped": reason}`` when ``sizing.ship`` refuses (a
    register-bearing or cascade-only sizing closes through the deliverable cascade); never raises -- the
    product commit stands. The sizing schema has no SHA field: the SHA rides the carrying
    commit's ``Sizing-Shipped`` trailer."""
    sizing_rel = _cited_sizing(worktree_root, plan_path)
    if sizing_rel is None:
        return {}
    try:
        reply = reentrant_dispatch("sizing.ship", {"sizing_path": sizing_rel}, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"sizing_ship_skipped": repr(exc)}
    if not isinstance(reply, dict) or reply.get("exit_code") != 0:
        return {"sizing_ship_skipped": str(reply.get("error") if isinstance(reply, dict) else reply)}
    return {"sizing_shipped": sizing_rel} if reply.get("applied") else {}


_RESOLUTION_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_RESOLUTION_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def _write_resolution(worktree_root: Path, sizing_rel: str, sha: str, run_id: str) -> dict:
    """Write ``resolution: {sha, run_id}`` beside the sizing's ``status: shipped``, in place and
    schema-checked. ``{}`` on a write, ``{"resolution_error": reason}`` otherwise; never raises --
    the product commit stands."""
    if not _RESOLUTION_SHA.match(sha or "") or not _RESOLUTION_RUN_ID.match(run_id or ""):
        return {"resolution_error": f"unrecordable resolution sha={sha!r} run_id={run_id!r}"}
    import yaml

    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    schema_path = Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"

    def mutate(old: str) -> str:
        new = write_fm_nested_field(old, "resolution", f'  sha: "{sha}"\n  run_id: "{run_id}"\n')
        try:
            errors = validate_frontmatter(yaml.safe_load(new) or {}, schema_path)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
            raise MutateAbort(f"resolution write refused: {exc}")
        if errors:
            raise MutateAbort(f"resolution write refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(worktree_root / sizing_rel, mutate, repo_root=worktree_root)
    except MutateAbort as exc:
        return {"resolution_error": str(exc.args[0]) if exc.args else "resolution write refused"}
    except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O: refuse, keep the commit
        return {"resolution_error": f"resolution write refused: {exc!r}"}
    return {}


def _stamp_coded_commit(
    commit_v2, worktree_root: Path, repo_root: Path, source_rows: dict,
    sha: str, session_id: Optional[str], also_commit: tuple = (),
    noop_rows: Optional[dict] = None, checkpoint_rows: Optional[dict] = None,
    snapshot: Optional[_Snapshot] = None, flip_plan: Optional[str] = None,
    receipt: Optional[str] = None,
    trailers: tuple = (), commit: bool = True,
) -> dict:
    """Second, plan-only ``commit_v2`` call stamping the product commit's
    rows ``coded`` with ``disposition_ref: <sha>`` -- a SHA only exists once
    the product commit has landed, so this cannot ride in it. One call per
    run, never per row. ``noop_rows`` (same shape as ``source_rows``) are done rows
    with no hunk in the commit: they close ``coded`` against the run's sha with a
    no-change ``disposition_detail``, so none stays ``open`` beneath a met run.
    ``checkpoint_rows`` (``{checkpoint sha: {plan: {row id}}}``) are done rows the run's
    own checkpoint commits landed: they close ``coded`` at that checkpoint's sha. An edit that would make a schema-valid plan invalid
    is refused before any write; on any failure every plan edit is restored.
    ``flip_plan`` names the plan whose ``implemented`` status is written to disk without
    committing (``_stamp_plan_implemented`` reads the rows just written ``coded``), so
    the one commit below carries rows, minted stamp and status together; its reply
    returns under ``_flip``. A commit that does not land restores the flip with the rows.
    ``commit=False`` (an ask run, whose plan is a process record) leaves the stamps on disk and
    lands nothing. ``_committed`` names the paths the commit carried.
    ``receipt`` names the file holding the run's minted ``review_stamp``. When it is gitignored
    (a minted XS spine) the commit cannot carry it and the receipt dies with the scratch dir, so
    the reply adds ``coded_stamp_error`` beside the rest of the commit's result.
    Returns ``{"rows_coded", "coded_sha"}`` or ``{"coded_stamp_error"}``.
    """
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source

    originals: dict = {}
    rows_coded: dict = {}

    def restore() -> None:
        for rel, text in originals.items():
            replace_text(worktree_root / rel, text)

    noop_rows = noop_rows or {}
    checkpoint_rows = checkpoint_rows or {}
    plans = set(source_rows) | set(noop_rows)
    for by_plan in checkpoint_rows.values():
        plans |= set(by_plan)
    for plan_rel in sorted(plans):
        original = _read_rel(worktree_root, plan_rel)
        if original is None:
            continue
        noop_ids = set(noop_rows.get(plan_rel, ())) - set(source_rows.get(plan_rel, ()))
        updated, flipped = _flip_rows_coded(
            original, set(source_rows.get(plan_rel, ())) | noop_ids, sha,
            {rid: _NOOP_DETAIL for rid in noop_ids},
        )
        for csha, by_plan in sorted(checkpoint_rows.items()):
            if plan_rel in by_plan:
                updated, more = _flip_rows_coded(updated, set(by_plan[plan_rel]), csha)
                flipped = sorted(set(flipped) | set(more))
        if not flipped:
            continue
        invalid = check_plan_tasks_source(updated)
        if invalid is not None and check_plan_tasks_source(original) is None:
            restore()
            return {"coded_stamp_error": f"{plan_rel}: stamped spine fails schema: {invalid}"}
        replace_text(worktree_root / plan_rel, updated)
        originals[plan_rel] = original
        rows_coded[plan_rel] = flipped

    if not commit:
        return {"rows_coded": rows_coded, "coded_sha": None}
    # A plan `review_stamp.mint` just wrote rides this commit even when none of
    # its rows flipped (a re-fire over already-coded rows).
    paths = sorted(set(rows_coded) | set(also_commit))
    flip_result: Optional[dict] = None
    if flip_plan is not None and paths:
        before = _read_rel(worktree_root, flip_plan)
        flip_result = _stamp_plan_implemented(worktree_root, flip_plan, sha)
        if flip_result.get("plan_status") == "implemented":
            if before is not None:
                originals.setdefault(flip_plan, before)
            paths = sorted(set(paths) | {flip_plan})
    # A path is stageable unless it is an untracked gitignored plan (a warp run
    # spine), which is never staged; the snapshot answers that without a spawn.
    ignored = set(snapshot.ignored_among(paths)) if snapshot is not None else set()
    paths = [p for p in paths if p not in ignored]
    uncovered = [p for p in paths if snapshot is None or not snapshot.covers(p)]
    if uncovered:
        stageable = _changed_paths(worktree_root, uncovered)
        if stageable is not None:
            paths = [p for p in paths if p not in uncovered or p in stageable]
    lost_receipt = (
        {"coded_stamp_error": f"{receipt}: review receipt is untracked and gitignored, so no commit "
         "can carry it; the run is unreceipted"}
        if receipt is not None and receipt not in paths else {}
    )
    if not paths:
        return {"rows_coded": {}, "coded_sha": None, **lost_receipt}
    n = sum(len(v) for v in rows_coded.values())
    message = f"mark {n} rows coded ({sha[:7]})" if n else f"review stamp ({sha[:7]})"
    if trailers:
        message += "\n\n" + "\n".join(trailers)
    params: dict = {"paths": paths, "message": message}
    if snapshot is not None and not uncovered:
        params["ignored_untracked"] = []
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
    out = {"rows_coded": rows_coded, "coded_sha": reply.get("sha"), "_committed": paths, **lost_receipt}
    if flip_result is not None:
        out["_flip"] = flip_result
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


def _ledger_gate(run_dir: Path, verdict: object) -> Optional[dict]:
    """The refusal when the run's requirements ledger is not met by ``verdict``, else None.
    A run emitted before the ledger existed (no ledger file) is not gated."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    ledger = requirements_ledger.read_ledger(run_dir)
    if ledger is None:
        return None
    if not isinstance(verdict, dict):
        return _error(
            "the run keeps a requirements ledger but params.ledger_verdict is absent: the final "
            "ledger checker's verdict gates this commit. Re-run the run's ledger check and pass "
            "its verdict, or revise the ledger with workflow.revise and re-run the check",
            refused="ledger-unjudged",
        )
    result = requirements_ledger.judge(ledger, verdict)
    if result["pass"]:
        return None
    return _error(
        "the requirements ledger is not met: "
        + "; ".join(f"{f['id']}: {f['reason']}" for f in result["failures"][:10]),
        refused="ledger-unmet",
        ledger_failures=result["failures"],
        ledger_items=len(ledger.items),
        all_of_pm_ask=ledger.all_of_pm_ask,
    )


def _ask_trailers(
    worktree_root: Path, plan_path: Optional[str], record_abs: Optional[Path], receipts: list
) -> list:
    """The trailers an ask run's product commit carries in place of the process records it
    leaves out: ``Falsifier: <pytest node>`` (the plan's falsifier), ``Review: <sha256>`` (the
    review record under ``.coordinator-local/``) and one ``Completion-Receipt: <id>`` each."""
    import hashlib

    from coordinator_core.completion_receipts.model import RECEIPT_TRAILER

    out = []
    plan_text = _read_rel(worktree_root, plan_path) if plan_path else None
    if plan_text is not None:
        from coordinator_core.ops.dispatch_emit.emit import _prime_exit_criterion_falsifier

        falsifier = _prime_exit_criterion_falsifier(plan_text) or {}
        node = _PYTEST_NODE.search(str(falsifier.get("how") or ""))
        if node:
            out.append(f"Falsifier: {node.group(0)}")
    if record_abs is not None:
        try:
            out.append(f"Review: {hashlib.sha256(record_abs.read_bytes()).hexdigest()}")
        except OSError:
            pass
    out.extend(f"{RECEIPT_TRAILER}: {fm['receipt_id']}" for fm, _prose in receipts)
    return out


def _mint_review_stamp(
    worktree_root: Path, plan_rel: Optional[str], sha: str, record_abs: Path, record: dict,
    tree: Optional[str] = None,
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
        mint(
            guarded, worktree_root, build_test_path=None, resolved=(sha, record_abs, record),
            criterion_open_ok=True, terminal_tree_sha=tree,
        )
    except MintRefusal as exc:
        return {"review_stamp": "refused", "review_stamp_refusal": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"review_stamp": "refused", "review_stamp_refusal": repr(exc)}
    return {"review_stamp": "minted"}


def _falsifier_broken_tells(marks: object, plan_path: Optional[str]) -> Optional[list]:
    """The fired tells when the run's pre-dispatch review called ``plan_path``'s
    falsifier BROKEN, else None. ``marks`` is the digest's ``[{plan, tells}]``;
    plans are matched as repo-relative POSIX paths, so a ``./`` or backslash
    spelling on either side still matches."""
    if not plan_path or not isinstance(marks, list):
        return None

    def norm(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    want = norm(plan_path)
    for mark in marks:
        key = mark.get("plan") if isinstance(mark, dict) else None
        tells = mark.get("tells") if isinstance(mark, dict) else None
        if isinstance(key, str) and norm(key) == want:
            return [str(t) for t in tells] if isinstance(tells, list) else []
    return None


def _stamp_plan_implemented(worktree_root: Path, plan_rel: str, sha: str) -> dict:
    """Write the plan's ``implemented`` status to disk, uncommitted, when the
    review stamp it now carries records a ``met`` criterion; the caller's commit
    carries it. The verdict is read off the plan on disk, never a param, so only
    a minted stamp can discharge it. A refusal (open spine rows, the goal gate)
    is reported and writes nothing."""
    import contextlib
    import io

    import yaml

    from coordinator_core.archive_stamp import cs_stamp_plan_implemented
    from coordinator_core.ops.plan_status_transition import _FALSIFIER_OUTPUT_MAX

    plan_abs = worktree_root / plan_rel
    text = _read_rel(worktree_root, plan_rel)
    from coordinator_core.frontmatter.primitives import split_frontmatter

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
            commit=False,
        )
    if rc != 0:
        reason = err.getvalue().strip()[-600:]
        return {"plan_status": "refused", "plan_status_refusal": reason, "plan_status_reason": reason}
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


def _peer_held(worktree_root: Path, session_id: Optional[str], paths: list) -> set:
    """The paths another session's touch claim holds: a peer's in-flight edit sitting in
    one of this run's directories, not this run's residue. No git spawn."""
    if not session_id or not paths:
        return set()
    from coordinator_core.session import claim_index

    try:
        answer = claim_index.classify_paths(session_id, paths, cwd=str(worktree_root))
    except Exception:
        return set()
    return {p for p, o in answer.by_path.items() if o.verdict == claim_index.OWNERSHIP_PEER}


def _in_scope(path: str, request: CommitRequest) -> bool:
    """Whether ``path`` matches one of ``_scope_pathspecs(request)``."""
    parent = path.rpartition("/")[0]
    for chunk in request.chunks:
        if any(p.rpartition("/")[0] == parent for p in chunk.paths):
            return True
        if any(path.startswith(prefix.rstrip("/") + "/") for prefix in chunk.prefixes):
            return True
    return False


def _undeclared_dirty(
    worktree_root: Path, request: CommitRequest,
    snapshot: Optional[_Snapshot] = None, landed: frozenset = frozenset(),
) -> dict:
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
    declared = {p for c in request.chunks for p in c.paths}
    if snapshot is not None:
        # The snapshot predates the commits: what they landed is no longer dirty.
        dirty = sorted(
            {p for p in snapshot.changed if _in_scope(p, request)} - declared - set(landed)
        )
    else:
        result = run_git(
            [
                "-C", str(worktree_root), "--no-optional-locks", "status",
                "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--", *specs,
            ],
            binary=True,
        )
        if not result.ok:
            return {"undeclared_dirty": None}
        dirty = sorted(
            {
                entry[3:].decode("utf-8", "surrogateescape")
                for entry in result.stdout_bytes.split(b"\0")
                if len(entry) > 3
            }
            - declared
        )
    peer_held = _peer_held(worktree_root, request.session_id, dirty)
    dirty = [p for p in dirty if p not in peer_held]
    out: dict = {"undeclared_dirty": dirty[:_UNDECLARED_DIRTY_CAP]}
    if peer_held:
        out["undeclared_dirty_peer_held"] = len(peer_held)
    if len(dirty) > _UNDECLARED_DIRTY_CAP:
        out["undeclared_dirty_total"] = len(dirty)
    return out


def _commit_siblings(
    sibling_paths: dict, subject: str, home_sha: Optional[str], session_id: Optional[str]
) -> list:
    """One ``ceremony.commit_v2`` per sibling repo, scoped to that repo's
    declared paths. A refusal is reported per repo, never raised."""
    out: list = []
    for root, paths in sibling_paths.items():
        message = subject + (f"\n\nCross-Repo-Of: {home_sha}" if home_sha else "")
        params: dict = {"paths": list(paths), "message": message}
        if session_id is not None:
            params["session_id"] = session_id
        try:
            reply = reentrant_dispatch("ceremony.commit_v2", params, repo_root=Path(root) / ".git")
        except OpUnavailableError:
            reply = {"committed": False, "error": "ceremony.commit_v2 is not registered"}
        if not isinstance(reply, dict):
            reply = {"committed": False, "error": f"unexpected commit_v2 reply: {reply!r}"}
        out.append({"repo": Path(root).name, "paths": list(paths), **reply})
    return out


_STATUS_FIELD = r'^[ \t>#-]*"?[*_`]{0,2}status[*_`]{0,2}[ \t]*:[ \t]*[*_`]{0,2}[ \t]*'
_DELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}DONE(?:_WITH_CONCERNS)?[*_`]{0,2}:|<exit-status>DONE</exit-status>'
    rf'|(?i:{_STATUS_FIELD}DONE(?:_WITH_CONCERNS)?\b)',
    re.MULTILINE,
)
_UNDELIVERED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}(?:PARTIAL|BLOCKED)[*_`]{0,2}:|<exit-status>(?:PARTIAL|BLOCKED)</exit-status>'
    rf'|(?i:{_STATUS_FIELD}(?:PARTIAL|BLOCKED)\b)',
    re.MULTILINE,
)
_PARTIAL_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}PARTIAL[*_`]{0,2}:|<exit-status>PARTIAL</exit-status>'
    rf'|(?i:{_STATUS_FIELD}PARTIAL\b)',
    re.MULTILINE,
)
_UNDONE_LINE_RE = re.compile(r"^\s*[-*]?\s*[*_`]{0,2}(?:not done|undone|remaining)", re.IGNORECASE)
_CAUSE_HEADING_RE = re.compile(
    r"^#{1,6}\s+.*\b(?:why|cause|blocker|blocked|reason)\b", re.IGNORECASE
)
_UNDONE_CAP = 400


def _cause_section(lines: list) -> list:
    """The body of the first heading naming a cause/blocker/reason, up to the next heading."""
    for i, ln in enumerate(lines):
        if _CAUSE_HEADING_RE.match(ln.strip()):
            body = []
            for nxt in lines[i + 1:]:
                if nxt.lstrip().startswith("#"):
                    break
                if nxt.strip():
                    body.append(nxt.strip())
            return [ln.strip().lstrip("#").strip() + ":"] + body if body else []
    return []


def _undone_summary(text: str) -> str:
    """The report's named undone work: its ``Not done``/``Remaining`` lines, else its
    cause/blocker section, else its PARTIAL line."""
    lines = text.splitlines()
    picked = [ln.strip() for ln in lines if _UNDONE_LINE_RE.match(ln)]
    if not picked:
        picked = _cause_section(lines)
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


def _reviewed_files_if_passed(inline_review: dict) -> Optional[set]:
    """The files the review wave covered (``prep.slice_files``), or ``None`` when the
    review did not pass: delivery verdict PASS, no unresolved finding, no confinement
    violation. Absent evidence is not a pass."""
    prep = inline_review.get("prep")
    delivery = inline_review.get("delivery")
    integration = inline_review.get("integration")
    if not (isinstance(prep, dict) and isinstance(delivery, dict) and isinstance(integration, dict)):
        return None
    files = prep.get("slice_files")
    if (
        delivery.get("verdict") != "PASS"
        or integration.get("unresolved")
        or integration.get("confinement_violations")
        or not isinstance(files, list)
    ):
        return None
    return {f for f in files if isinstance(f, str)}


def _regradable_chunks(
    worktree_root: Path, request: CommitRequest, incomplete: set, inline_review: dict,
    report_cache: dict,
) -> dict:
    """``{chunk id: gate-blocker text}`` for executor-PARTIAL chunks the run's own review
    clears: the report carries a ``gate-blocker: outside-footprint`` line (the row's build
    gate went red only from errors outside its footprint), every declared and own-prefix
    file sits in ``prep.slice_files``, and the review passed. Prose that merely resembles
    the cause does not qualify; a chunk with no files never does."""
    reviewed = _reviewed_files_if_passed(inline_review)
    if reviewed is None:
        return {}
    out: dict = {}
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is None or not _PARTIAL_REPORT_RE.search(text):
            continue
        blocker = gate_owed.match(text, gate_owed.KIND_OUTSIDE_FOOTPRINT)
        if blocker is None:
            continue
        prefix_files = _own_prefix_files(worktree_root, chunk, report_cache)
        if prefix_files is None:
            continue
        files = set(chunk.paths) | set(prefix_files)
        if files and files <= reviewed:
            out[chunk.id] = f"outside-footprint{blocker[1].rstrip()}"[:_UNDONE_CAP]
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


class _Snapshot:
    """One scoped ``git status`` read of the worktree: ``changed`` (modified, added,
    untracked, deleted or unmerged) and ``ignored`` (untracked and gitignored; an
    ignored directory is one entry with a trailing ``/``). It answers only for paths
    it ``covers``: a requested literal path, or a path at or under a requested root."""

    __slots__ = ("changed", "ignored", "literals", "roots")

    def __init__(self, changed: set, ignored: list, literals: set, roots: list) -> None:
        self.changed = changed
        self.ignored = ignored
        self.literals = literals
        self.roots = roots

    def covers(self, path: str) -> bool:
        return path in self.literals or any(path == r or path.startswith(r + "/") for r in self.roots)

    def note_written(self, paths: list) -> None:
        """Record files this run just wrote (they differ from HEAD by construction)."""
        self.literals.update(paths)
        self.changed.update(paths)

    def changed_among(self, specs: list) -> set:
        bounds = [(s, s.rstrip("/") + "/") for s in specs]
        return {p for p in self.changed if any(p == s or p.startswith(under) for s, under in bounds)}

    def ignored_among(self, paths: list) -> list:
        dirs = [e for e in self.ignored if e.endswith("/")]
        files = set(self.ignored)
        return [p for p in paths if p in files or any(p.startswith(d) for d in dirs)]


def _worktree_snapshot(
    worktree_root: Path, literals: list, roots: list, scope_specs: list
) -> Optional[_Snapshot]:
    """The run's one worktree read; ``None`` when git could not answer or nothing was asked."""
    literals = list(dict.fromkeys(literals))
    roots = [r.rstrip("/") for r in dict.fromkeys(roots)]
    specs = [f":(literal){p}" for p in literals] + [f":(literal){r}/" for r in roots] + list(scope_specs)
    if not specs:
        return None
    result = run_git(
        [
            "-C", str(worktree_root), "--no-optional-locks", "status",
            "--porcelain=v2", "-z", "--no-renames", "--untracked-files=all",
            "--ignored=matching", "--", *specs,
        ],
        binary=True,
    )
    if not result.ok:
        return None
    changed: set = set()
    ignored: list = []
    for raw in result.stdout_bytes.split(b"\0"):
        if len(raw) < 3:
            continue
        kind = raw[:1]
        fields = {b"1": 8, b"u": 10, b"?": 1, b"!": 1}.get(kind)
        if fields is None:
            continue
        path = raw.split(b" ", fields)[fields].decode("utf-8", "surrogateescape")
        if kind == b"!":
            ignored.append(path)
        else:
            changed.add(path)
    return _Snapshot(changed, ignored, set(literals), roots)


def _changed(snapshot: Optional[_Snapshot], worktree_root: Path, paths: list) -> Optional[set]:
    """``_changed_paths`` answered from ``snapshot`` where it covers a path, by a spawn for the rest."""
    if snapshot is None:
        return _changed_paths(worktree_root, paths)
    known = [p for p in paths if snapshot.covers(p)]
    rest = [p for p in paths if not snapshot.covers(p)]
    out = snapshot.changed_among(known)
    if rest:
        extra = _changed_paths(worktree_root, rest)
        if extra is None:
            return None
        out |= extra
    return out


def _entangled_chunks(
    worktree_root: Path, request: CommitRequest, stranded_ids: set, report_cache: dict,
    snapshot: Optional[_Snapshot] = None,
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
    changed = _changed(snapshot, worktree_root, specs)
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


def _regenerate_landing_allowlist(worktree_root: Path, paths: list) -> Optional[str]:
    """Repo-relative ``setup/publish-targets.portable`` when the commit's paths
    add a published-tree top-level name the landing allowlist does not
    classify and this call regenerated it (written to the worktree, to ride the
    commit); else ``None``.

    The pre-filter is `commit_paths`' own: a commit touching neither
    generator-owned dir, or a tree without the declarations yaml, pays no read
    and no spawn. A generator failure leaves the worktree untouched, so
    ``commit_paths`` refuses with its own message.
    """
    from coordinator_core.git import published_tree_classification as ptc

    touched = ptc.touched_published_names(paths)
    if not touched or not (worktree_root / ptc.DECLARATIONS_PATH).is_file():
        return None

    def read_worktree(rel: str) -> Optional[str]:
        try:
            return (worktree_root / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    if not ptc.unclassified(touched, read_worktree):
        return None

    import importlib.util

    generator = worktree_root / "coordinator" / "bin" / "publish-allowlist-generate.py"
    try:
        spec = importlib.util.spec_from_file_location("publish_allowlist_generate", generator)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pending = tuple(p for p in paths if (worktree_root / p).is_file())
        regenerated = module.regenerate_portable(worktree_root, pending)
        portable = worktree_root / ptc.PORTABLE_PATH
        if regenerated == portable.read_text(encoding="utf-8"):
            return None
        portable.write_text(regenerated, encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001 -- commit_paths refuses with its own message
        return None
    return ptc.PORTABLE_PATH


#: Script the close job runs as; ``None`` is ``ops/ceremony/close_background.py``. A budget
#: driver points it at a no-op so the gate measures the launch, not the job's own work.
CLOSE_JOB_SCRIPT: Optional[str] = None
_CLOSE_JOB_WINDOW_MINUTES = 5
_BACKGROUND_LEGS = ("review", "baton-drain", "orientation-refresh", "memo-lifecycle", "post-close")
_PATH_WITHHELD = "detail withheld: names a non-repo-relative path"
_NOT_LANDED = "coded-stamp commit did not land"


def _leg(status: str, detail: str = ""):
    """A ``LegResult``; a detail naming a non-repo-relative path is replaced, never raised."""
    from coordinator_core.ops.dispatch_emit.run_close import LegResult

    try:
        return LegResult(status, detail)
    except ValueError:
        return LegResult(status, _PATH_WITHHELD)


def _plan_source_memo(worktree_root: Path, plan_rel: Optional[str]) -> Optional[str]:
    text = _read_rel(worktree_root, plan_rel) if plan_rel else None
    if text is None:
        return None
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    split = split_frontmatter(text.replace("\r\n", "\n"))
    value = read_fm_field_unquoted(split.fm_text, "source_memo") if split else None
    return None if value in (None, "", "null", "~") else value


def _deferred_rows(worktree_root: Path, plan_rel: str) -> list:
    """Ids of the plan's spine rows marked ``deferred: true`` (harvest is not built here)."""
    text = _read_rel(worktree_root, plan_rel)
    if text is None:
        return []
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return []
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError:
        return []
    return sorted(
        str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("deferred") is True
    ) if isinstance(rows, list) else []


def _pay_inline_legs(
    worktree_root: Path, ctx: dict, work_sha: str, session_id: Optional[str],
    snapshot: Optional[_Snapshot],
) -> list:
    """Run the ``completion-entry`` and ``baton-ship`` legs; returns the repo files they wrote,
    which the coded-stamp commit carries. ``ctx["rollback"]`` keeps what undoing them needs."""
    from coordinator_core.ops.dispatch_emit.completion_receipt import _handoff_claims
    from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg
    from coordinator_core.ops.dispatch_emit.run_close_completion import completion_entry_leg

    run_id, legs = ctx["run_id"], ctx["legs"]
    ctx["session_id"] = session_id
    criterion = ctx["inline_review"].get("criterion")
    criterion = criterion if isinstance(criterion, dict) else None
    try:
        result, entry_paths = completion_entry_leg(worktree_root, run_id, ctx["plan_rel"], criterion)
    except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
        result, entry_paths = _leg("failed", f"completion-entry raised {type(exc).__name__}"), []
    legs["completion-entry"] = result
    if result.status in ("ran", "discharged"):
        ctx["completion_entry"] = (entry_paths or [result.detail.removeprefix("entry ")])[0]

    batons = _handoff_claims(worktree_root, session_id)
    ctx["batons"] = batons
    status = criterion.get("status") if criterion else None
    saved: dict = {}
    written = list(entry_paths)
    if batons and status != "met":
        legs["baton-ship"] = _leg("failed", f"criterion {status or 'absent'}: batons not shipped")
    else:
        saved = {rel: _read_rel(worktree_root, rel) for rel in batons}
        try:
            shipped = baton_ship_leg(worktree_root, run_id, batons, work_sha)
        except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
            shipped = _leg("failed", f"baton-ship raised {type(exc).__name__}")
        legs["baton-ship"] = shipped
        if shipped.status == "ran":
            written += batons
        else:
            _restore_texts(worktree_root, saved)
            saved = {}
    ctx["rollback"] = {"entries": list(entry_paths), "saved": saved}
    if snapshot is not None:
        snapshot.note_written(written)
    return written


def _restore_texts(worktree_root: Path, saved: dict) -> None:
    for rel, text in saved.items():
        if text is not None:
            replace_text(worktree_root / rel, text)


def _launch_close_job(worktree_root: Path, run_id: str, batons: list, memo: Optional[str]) -> bool:
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    script = CLOSE_JOB_SCRIPT or str(engine / "coordinator_core" / "ops" / "ceremony" / "close_background.py")
    params = json.dumps({"claimed_batons": list(batons), "source_memo": memo})
    return spawn_detached(
        str(engine), script, [run_id, "--root", str(worktree_root), "--params", params]
    )


def _background_legs(worktree_root: Path, ctx: dict) -> dict:
    """Write the close-job record, launch the job, and report each background leg."""
    from datetime import datetime, timedelta

    from coordinator_core.ops.dispatch_emit.run_close import read_close_job, write_close_job

    run_id, batons = ctx["run_id"], ctx.get("batons") or []
    memo = _plan_source_memo(worktree_root, ctx["plan_rel"])
    legs: dict = {}
    queue: list = []
    for leg in _BACKGROUND_LEGS:
        if leg == "baton-drain" and not batons:
            legs[leg] = _leg("not-applicable", "run claims no baton")
        elif leg == "review" and not ctx.get("review_receipt"):
            continue
        elif leg in ctx["presets"]:
            legs[leg] = ctx["presets"][leg]
        elif leg == "memo-lifecycle" and not memo:
            legs[leg] = _leg("not-applicable", "plan names no source memo")
        else:
            queue.append(leg)
    deadline = datetime.now().astimezone() + timedelta(minutes=_CLOSE_JOB_WINDOW_MINUTES)
    try:
        write_close_job(
            worktree_root, run_id, queue, deadline,
            session_id=ctx.get("session_id"),
            plan_rel=ctx["plan_rel"],
            source_memo=memo,
            claimed_batons=batons,
            completion_entry=ctx.get("completion_entry"),
            settled={leg: ctx["legs"][leg] for leg in ("completion-entry", "baton-ship") if leg in ctx["legs"]},
            review_receipt=ctx.get("review_receipt"),
            run_base_sha=ctx.get("run_base_sha"),
            terminal_sha=ctx.get("terminal_sha"),
        )
    except (OSError, ValueError) as exc:
        legs.update({leg: _leg("failed", f"close job record not written: {type(exc).__name__}") for leg in queue})
        return legs
    if not _launch_close_job(worktree_root, run_id, batons, memo):
        legs.update({leg: _leg("failed", "close job did not start") for leg in queue})
        return legs
    legs.update(read_close_job(worktree_root, run_id, datetime.now().astimezone(), queue))
    return legs


def _settle_close(worktree_root: Path, ctx: dict, landed: bool) -> None:
    """After the coded-stamp commit: forward lessons and launch the job when it landed, else
    undo the inline legs' files and report every leg that depended on it ``failed``."""
    from coordinator_core.ops.dispatch_emit.completion_receipt import _remove

    legs = ctx["legs"]
    if not landed:
        rollback = ctx.get("rollback") or {}
        _remove(worktree_root, rollback.get("entries", []))
        _restore_texts(worktree_root, rollback.get("saved", {}))
        for leg, result in list(legs.items()):
            if result.status == "ran":
                legs[leg] = _leg("failed", _NOT_LANDED)
        for leg in ("lessons", *_BACKGROUND_LEGS):
            legs[leg] = _leg("failed", _NOT_LANDED)
        ctx["completion_entry"] = None
        return
    from coordinator_core.ops.dispatch_emit.run_close_lessons import lessons_leg

    criterion = ctx["inline_review"].get("criterion")
    try:
        legs["lessons"] = lessons_leg(
            worktree_root, ctx["run_id"], criterion if isinstance(criterion, dict) else None
        )
    except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
        legs["lessons"] = _leg("failed", f"lessons raised {type(exc).__name__}")
    legs.update(_background_legs(worktree_root, ctx))


def _close_block(ctx: dict, reply: dict) -> dict:
    """The reply's ``close`` block: the settled inline and background legs, with ``review`` and
    ``plan-reconciliation`` read off the reply, and every leg nobody settled ``failed`` with why."""
    from coordinator_core.ops.dispatch_emit.run_close import RUN_BOUND_LEGS, build_close_block

    legs = dict(ctx["legs"])
    if ctx["applies"]:
        if reply.get("committed") is not True:
            why = f"terminal commit did not land ({reply['refused']})" if reply.get("refused") else "terminal commit did not land"
        elif "coded_stamp_error" in reply:
            why = _NOT_LANDED
        else:
            why = "leg did not run"
        stamp = reply.get("review_stamp")
        if "review" not in legs:
            if stamp == "refused":
                legs["review"] = _leg("failed", str(reply.get("review_stamp_refusal") or "review stamp refused")[:200])
            else:
                legs["review"] = _leg("failed", f"no review stamp: {why}")
        status = reply.get("plan_status")
        if "plan-reconciliation" not in ctx["presets"] and status == "implemented":
            deferred = (
                [] if ctx["kind"] == "xs-s-run" else _deferred_rows(ctx["root"], ctx["plan_rel"])
            )
            legs["plan-reconciliation"] = (
                _leg("failed", "deferred rows unharvested: " + ", ".join(deferred))
                if deferred
                else _leg("ran", "plan implemented")
            )
        elif "plan-reconciliation" in ctx["presets"]:
            pass  # the preset is applied below
        elif status:
            legs["plan-reconciliation"] = _leg("failed", str(reply.get("plan_status_reason") or status)[:200])
        else:
            legs["plan-reconciliation"] = _leg("failed", f"plan not reconciled: {why}")
        for leg in RUN_BOUND_LEGS:
            legs.setdefault(leg, _leg("failed", why))
        legs.update(ctx["presets"])
    return build_close_block(ctx["run_id"], ctx["applies"], legs, ctx["completion_entry"], kind=ctx["kind"])


@register_op("dispatch.terminal_commit")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.terminal_commit" handler -- mutating, sync.

    Params:
        script_path (str, required)      -- the emitted Workflow script to
                                             parse the terminal-commit-request
                                             marker (D2) out of. Guarded under
                                             the caller's own worktree before
                                             any read.
        incomplete_chunks (list[str], required unless task_output_path supplies it; may be empty) -- chunk ids the
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
        ledger_verdict (dict, optional)  -- an ask run's final ledger-checker
                                             verdict ``{items: [{id, status,
                                             essential?, superseded_by?}]}``;
                                             required when the run dir holds a
                                             requirements ledger, re-judged
                                             against it (``refused`` is
                                             ``ledger-unjudged`` / ``ledger-unmet``).

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
    ``review_receipt`` (a mise run only) is the inventory record carrying the minted receipt;
    a refused receipt reports ``review_stamp: refused`` instead.
    ``review_stamp`` (``minted``/``refused``) reports the stamp, and on a
    minted stamp with no incomplete chunk ``plan_status`` reports the
    ``implemented`` flip (``implemented``/``refused``/``not-stamped``; every
    non-``implemented`` value carries ``plan_status_reason``, beside the top-level
    ``criterion_status`` the digest's criterion leg reported), which
    ``plan_status_transition`` commits on its own. A
    script with no marker returns ``{"committed": False, "nothing_to_commit":
    True}`` without error. ``commit_v2``'s own ``nothing_to_commit: True``
    (a peer already landed the bytes) passes through unmodified, as a
    non-error. Every reply also carries ``stranded`` -- ``{chunk id: [declared
    paths]}`` for each ``incomplete_chunks`` id the request marker names, the
    work the commit left uncommitted; ``{}`` when none, or when no marker was read. The key is
    omitted on a ``missing-run-outcome`` or ``unreviewed`` refusal: the incomplete set was not
    known, so an empty map would falsely read as "nothing stranded".
    ``entangled`` (present only when non-empty) maps each chunk held back from the commit to
    the stranded chunk ids whose uncommitted ``.py`` files it imports (transitively): landing it
    would leave an unresolvable import at that sha. It is stranded with them, its reason
    ``entangled: ...``; land the pair by hand once the definer is finished.
    ``blockers`` (present only when non-empty) maps each executor-PARTIAL row, whose
    files stay uncommitted, to the undone work its report names.
    ``regraded`` (present only when non-empty) maps each executor-PARTIAL row, re-graded
    complete, to its blocker: the report's ``gate-blocker: outside-footprint`` line, every
    file of the row in the passed review's ``prep.slice_files``. It commits with the run,
    leaves ``incomplete_chunks``, and is coded like a DONE row.
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
    regraded: dict = {}
    close_ctx: dict = {}
    reply = _terminal_commit(
        params, repo_root, stranded, scope, gated_ids, blockers, regraded, close_ctx=close_ctx
    )
    if close_ctx:
        reply["close"] = _close_block(close_ctx, reply)
    if reply.get("refused") not in _STRANDED_UNKNOWN_REFUSALS:
        reply["stranded"] = stranded
    if blockers:
        reply["blockers"] = blockers
    if regraded:
        reply["regraded"] = regraded
    if scope:
        reply.update(
            _undeclared_dirty(
                scope["root"], scope["request"], scope.get("snapshot"), frozenset(scope.get("landed", ()))
            )
        )
    incomplete = params.get("incomplete_chunks") if isinstance(params, dict) else None
    landed = (params.get("landed_chunks") if isinstance(params, dict) else None) or []
    held = params.get("held") if isinstance(params, dict) else None
    held = {
        h["chunk"]: h.get("reason") or ""
        for h in (held if isinstance(held, list) else [])
        if isinstance(h, dict) and isinstance(h.get("chunk"), str)
    }
    if (landed or regraded) and isinstance(incomplete, list) and isinstance(landed, list):
        reply["incomplete_chunks"] = sorted(set(incomplete) - set(landed) - set(regraded))
    if isinstance(incomplete, list) and isinstance(landed, list):
        entangled = reply.get("entangled") or {}
        open_ids = (set(incomplete) - set(landed) - set(regraded)) | set(entangled) | set(held)
        reasons = {}
        for i in sorted(open_ids):
            if i in held:
                reasons[i] = f"held: {held[i]}"
            elif i in gated_ids:
                reasons[i] = "external_gate"
            elif i in entangled:
                reasons[i] = "entangled: imports stranded " + ", ".join(entangled[i])
            elif i in blockers:
                reasons[i] = f"executor_partial: {blockers[i]}" if blockers[i] else "executor_partial"
            else:
                reasons[i] = "incomplete_unreported"
        if reasons:
            reply["incomplete_reasons"] = reasons
    if stranded or reply.get("incomplete_reasons"):
        reply["next"] = _resume_hint(stranded, scope, params)
    return reply


def _resume_hint(stranded: dict, scope: dict, params: dict) -> str:
    request = scope.get("request")
    plan = getattr(request, "plan_path", None) or (
        params.get("plan_path") if isinstance(params, dict) else None
    ) or "<plan>"
    run_text = (params.get("task_output_path") if isinstance(params, dict) else None) or "<run-output-file>"
    command = f"emit-dispatch-workflow --plan {plan} --only-incomplete {run_text}"
    if stranded:
        return (
            f"stranded files for {', '.join(sorted(stranded))}: commit or discard them, "
            f"then re-emit with {command}"
        )
    return f"re-emit the incomplete rows with {command}"


def _held_ids(params: dict) -> set:
    held = params.get("held")
    return {
        h["chunk"] for h in (held if isinstance(held, list) else [])
        if isinstance(h, dict) and isinstance(h.get("chunk"), str)
    }


def _held_write_paths(worktree_root: Path, request: CommitRequest, held_ids: set) -> dict:
    """``{path: [held chunk ids]}`` of every ``writes:`` path a held chunk declares: the marker's
    chunk paths plus the plan spine row of the same id (a held chunk may be absent from the marker)."""
    out: dict = {}
    for chunk in request.chunks:
        if chunk.id in held_ids:
            for p in chunk.paths:
                out.setdefault(p, []).append(chunk.id)
    text = _read_rel(worktree_root, request.plan_path) if request.plan_path else None
    if text is not None:
        import yaml

        from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

        located = locate_fenced_block(text.replace("\r\n", "\n"))
        try:
            rows = yaml.safe_load(located.body) or [] if located.status == LocateStatus.LOCATED else []
        except yaml.YAMLError:
            rows = []
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and str(row.get("id")) in held_ids:
                for p in row.get("writes") or ():
                    if isinstance(p, str):
                        ids = out.setdefault(PurePosixPath(p.replace("\\", "/")).as_posix(), [])
                        if str(row["id"]) not in ids:
                            ids.append(str(row["id"]))
    return out


def _terminal_commit(
    params: dict, repo_root: Optional[Path], stranded: dict, scope: dict,
    gated_ids: Optional[set] = None, blockers: Optional[dict] = None,
    regraded: Optional[dict] = None,
    close_ctx: Optional[dict] = None,
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

    missing = _missing_run_outcome(params)
    if missing is not None:
        return missing

    refusal = validate_params("dispatch.terminal_commit", params, _PARAM_FIELDS)
    if refusal is not None:
        return _error(refusal["error"])

    script_path_raw = params["script_path"]
    incomplete_chunks = set(params["incomplete_chunks"]) - set(params.get("landed_chunks") or [])
    inline_review = params.get("inline_review")

    from coordinator_core.git.commit_trailers import _UUID_RE

    session_id = params.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id)
    ):
        session_id = None

    from coordinator_core.ops.fleet._common import main_worktree_root

    worktree_root = main_worktree_root(repo_root)

    if params.get("run_token") is not None:
        from coordinator_core.ops.dispatch_emit import run_token

        if not run_token.admits(worktree_root, params.get("run_id"), params["run_token"], script_path_raw):
            return _error(
                "params.run_token does not match a live token minted for params.run_id and this script",
                refused="run-token",
            )

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
    ask_run_dir: Optional[Path] = None
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
            ask_run_dir = manifest_abs.parent
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
    # An ask run commits product only; its process records stay on disk, named by trailers.
    product_only = ask_run_dir is not None
    # A review-only run closes no row: a row reopened since its emit must stay open.
    rows_untouched = anchor_only or request.review_only
    if close_ctx is not None:
        applies = bool(request.plan_path) and not rows_untouched
        kind, plan_governs = (
            _close_kind(_read_rel(worktree_root, request.plan_path)) if applies else ("plan-run", True)
        )
        from coordinator_core.ops.dispatch_emit.run_close import kind_presets

        presets = kind_presets(kind, plan_governs=plan_governs)
        close_ctx.update(
            root=worktree_root,
            run_id=PurePosixPath(script_path_raw.replace("\\", "/")).stem,
            applies=applies,
            plan_rel=request.plan_path,
            inline_review=inline_review if isinstance(inline_review, dict) else {},
            legs=dict(presets),
            completion_entry=None,
            kind=kind,
            presets=presets,
        )

    # Review at close is deterministic (PM ruling 2026-10-01): a run whose
    # result carries no review-stage output cannot land code, however the
    # script was composed or stripped.
    if inline_review is None:
        return _error(
            "params.inline_review is absent -- the run carries no review-stage "
            "output. Run the digest's `terminal_commit_cli` line verbatim (or pass "
            "task_output_path); else re-emit with a review stage and re-fire",
            refused="unreviewed",
        )
    if not inline_review.get("integration_stem") or inline_review.get("slices") is None:
        return _error(
            "params.inline_review is missing integration_stem and/or slices -- "
            f"no review-stage output to land against (got: {inline_review!r}). "
            "Use the digest's `terminal_commit_cli` line verbatim",
            refused="unreviewed",
        )
    if ask_run_dir is not None:
        unmet = _ledger_gate(ask_run_dir, params.get("ledger_verdict"))
        if unmet is not None:
            return unmet

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
    register_failure = (
        None if anchor_only else _register_gate(worktree_root, request.plan_path, inline_review)
    )
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
        from coordinator_core.ops.review_mint.wave_bookkeeping import bookkeep_wave

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

    companions: list = []
    if not anchor_only:
        cited_sizing = _cited_sizing(worktree_root, request.plan_path)
        if cited_sizing is not None:
            companions.append(cited_sizing)
        if request.plan_path:
            from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path

            evidence_rel = str(
                PurePosixPath(request.plan_path).with_name(
                    evidence_sidecar_path(Path(request.plan_path)).name
                )
            )
            if (worktree_root / evidence_rel).is_file():
                companions.append(evidence_rel)
    plan_seam = (
        str(PurePosixPath(request.plan_path).with_name(PurePosixPath(request.plan_path).stem + _SEAM_SIDECAR_SUFFIX))
        if request.plan_path else None
    )
    snapshot = _worktree_snapshot(
        worktree_root,
        [p for c in request.chunks for p in c.paths]
        + [p for p in (request.plan_path, plan_seam, bookkeeping_record_path) if p]
        + companions,
        [prefix for c in request.chunks for prefix in c.prefixes],
        _scope_pathspecs(request),
    )

    known_ids = {chunk.id for chunk in request.chunks}
    unsent_memos = _memo_rows_without_receipt(worktree_root, request)
    report_cache: dict = {}
    regraded_here = _regradable_chunks(
        worktree_root, request, incomplete_chunks, inline_review, report_cache
    )
    if regraded_here:
        incomplete_chunks = incomplete_chunks - set(regraded_here)
        if regraded is not None:
            regraded.update(regraded_here)
    # The wake digest lists every unfinished row, including rows that never
    # started and so never reached the marker; those carry no paths to hold
    # back, so they are reported, not refused.
    # A held chunk's work is not the run's to land, whatever its report or the review's slice say.
    held_ids = _held_ids(params)
    incomplete_chunks = incomplete_chunks | (held_ids & known_ids)
    unmarked_incomplete = sorted(incomplete_chunks - known_ids)
    stranded.update(
        {c.id: list(c.paths) for c in request.chunks if c.id in incomplete_chunks}
    )

    withheld_partial: dict = {}
    partial_chunks = _partial_chunks(worktree_root, request, incomplete_chunks, withheld_partial)
    partial_ids = {c.id for c in partial_chunks}
    entangled = _entangled_chunks(
        worktree_root, request, incomplete_chunks - partial_ids, report_cache, snapshot
    )
    if entangled:
        incomplete_chunks = incomplete_chunks | set(entangled)
        partial_chunks = [c for c in partial_chunks if c.id not in entangled]
        stranded.update({c.id: list(c.paths) for c in request.chunks if c.id in entangled})
    partial_chunks = [c for c in partial_chunks if c.id not in held_ids]
    done_chunks = [c for c in request.chunks if c.id not in incomplete_chunks]

    held_paths = _held_write_paths(worktree_root, request, held_ids) if held_ids else {}
    if held_paths:
        overlap: dict = {}
        for chunk in done_chunks + partial_chunks:
            for p in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
                if p in held_paths:
                    entry = overlap.setdefault(p, {"held": held_paths[p], "landed": []})
                    if chunk.id not in entry["landed"]:
                        entry["landed"].append(chunk.id)
        if overlap:
            shown = "; ".join(
                f"{p} (held {', '.join(e['held'])}; landed {', '.join(e['landed'])})"
                for p, e in sorted(overlap.items())
            )
            return _error(
                f"path(s) declared by both a held chunk and a landed chunk cannot be committed "
                f"consistently: {shown}. Re-fire with the landed chunk listed in params.held, or "
                "release the held chunk first",
                refused="held-landed-overlap",
                overlap=overlap,
            )

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

    if bookkeeping_record_path is not None and not product_only and bookkeeping_record_path not in all_paths:
        all_paths.append(bookkeeping_record_path)

    # A sibling-checkout path never reaches the home commit (it would drop as
    # absent-at-HEAD); each sibling repo lands its own commit after the home one.
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths

    all_paths, sibling_paths = split_sibling_paths(all_paths, worktree_root)

    def _with_siblings(reply: dict) -> dict:
        if sibling_paths:
            reply["sibling_commits"] = _commit_siblings(
                sibling_paths, _subject(done_chunks, "review trail"), reply.get("sha"), session_id
            )
        return reply

    if not all_paths:
        if close_ctx is not None:
            close_ctx.clear()
        return _with_siblings({"committed": False, "nothing_to_commit": not sibling_paths})

    if not anchor_only and not product_only:
        for companion in companions:
            if companion not in all_paths:
                all_paths.append(companion)
        for seam_rel in _seam_sidecars(
            worktree_root, request.plan_path, [c.id for c in done_chunks + partial_chunks]
        ):
            if seam_rel not in all_paths:
                all_paths.append(seam_rel)

    doc_dropped: list = []
    doc_kept: list = []
    if not anchor_only and params.get("doc_paths"):
        doc_kept, doc_dropped = _fold_doc_paths(
            worktree_root, params["doc_paths"], request.plan_path, session_id, product_only,
            params.get("doc_writes"),
        )
        all_paths.extend(p for p in doc_kept if p not in all_paths)

    if held_paths:
        all_paths = [p for p in all_paths if p not in held_paths]

    declared_writes = {p for c in done_chunks + partial_chunks for p in c.paths}
    absent = [p for p in all_paths if not (worktree_root / p).exists()]
    dropped_absent: list = []
    deleted_paths: list = []
    if absent:
        from coordinator_core.git.commit import partition_declared_deletions

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
        if close_ctx is not None:
            close_ctx.clear()
        return _with_siblings({"committed": False, "nothing_to_commit": not sibling_paths})

    scope_request = request
    if product_only and params.get("doc_writes"):
        scope_request = CommitRequest(
            chunks=request.chunks + (ChunkCommit(id="docs", title="docs", paths=tuple(params["doc_writes"])),),
            deliverable_id=request.deliverable_id, plan_path=request.plan_path,
            session_id=request.session_id,
        )
    scope.update(root=worktree_root, request=scope_request, snapshot=snapshot)
    # Unlike a hand commit, a run's product commit never absorbs a live peer's in-flight edit.
    from coordinator_core.ops.ceremony import commit_v2 as commit_v2_mod

    peer_holds: list = []
    commit_v2_mod._peer_claim_warnings(
        worktree_root, all_paths + deleted_paths, peer_holds, own_sid=session_id
    )
    if peer_holds:
        sids = sorted({sid for _p, sid in peer_holds})
        return _error(
            "live peer session(s) " + ", ".join(sids) + " hold write claims on path(s) this "
            "commit would carry: " + ", ".join(sorted({p for p, _s in peer_holds})) + ". "
            "Wait for the peer to finish, or have it commit them, then re-fire",
            refused="peer-claim",
            peer_claims=[{"path": p, "session": s} for p, s in peer_holds],
        )
    final_paths_set = set(all_paths)
    changed = _changed(snapshot, worktree_root, all_paths)

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
            local=product_only,
        )
        receipt_paths = write_run_receipts(worktree_root, receipts_built) if receipts_built else []
    except Exception as exc:  # noqa: BLE001 -- write_run_receipts already removed its partial writes
        return _error(f"completion receipt write failed: {exc}", refused="receipt-write-failed")
    if not product_only:
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
    resolves = None if anchor_only or incomplete_chunks else _resolving_sizing(worktree_root, request.plan_path)
    if resolves is not None:
        trailers.append(f"Resolves-Sizing: {resolves['id']} {resolves['tshirt']}")
    trailers.append(
        f"Inline-Review: applies {inline_review['integration_stem']} -- execute-review: "
        f"{inline_review['slices']} slices, {inline_review.get('fixes')} fixes"
    )
    if product_only:
        trailers.extend(_ask_trailers(worktree_root, request.plan_path, record_abs, receipts_built))
    paragraphs.append("\n".join(trailers))
    message = "\n\n".join(paragraphs)

    def commit_v2(params: dict, root: Path):
        return reentrant_dispatch("ceremony.commit_v2", params, repo_root=root)

    allowlist_path = _regenerate_landing_allowlist(worktree_root, all_paths)
    if allowlist_path is not None and allowlist_path not in all_paths:
        all_paths.append(allowlist_path)

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
    if snapshot is not None:
        # Receipts and the regenerated allowlist are new tracked-by-design files the
        # snapshot predates; every other path must be covered or commit_v2 asks git itself.
        fresh = {*receipt_paths, allowlist_path}
        unread = [p for p in all_paths if p not in fresh and not snapshot.covers(p)]
        if not unread:
            commit_params["ignored_untracked"] = snapshot.ignored_among(
                [p for p in all_paths if p not in fresh]
            )

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
        scope.setdefault("landed", set()).update(all_paths)
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
        noop_chunks = [c for c in done_chunks if c not in coded_chunks]
        # A no-hunk row whose every declared write is absent delivered nothing to
        # close against: coding it drops it from every later emit.
        absent = set(dropped_absent)
        unbuilt = [c for c in noop_chunks if c.paths and set(c.paths) <= absent]
        if unbuilt:
            reply["uncoded_absent"] = [c.id for c in unbuilt]
            noop_chunks = [c for c in noop_chunks if c not in unbuilt]
        checkpoint_of = (
            {} if rows_untouched or not noop_chunks
            else _checkpoint_attribution(worktree_root, str(reply["sha"]), request.plan_path, noop_chunks)
        )
        checkpoint_rows: dict = {}
        for chunk_id, csha in checkpoint_of.items():
            for plan_rel, ids in _source_rows_by_plan(worktree_root, request.plan_path, [chunk_id]).items():
                checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(ids)
        if request.review_only and not anchor_only:
            review_coded, review_no_delta = _review_only_coding(
                worktree_root, str(reply["sha"]), request, inline_review
            )
            for csha, by_plan in review_coded.items():
                for plan_rel, ids in by_plan.items():
                    checkpoint_rows.setdefault(csha, {}).setdefault(plan_rel, set()).update(ids)
            if review_no_delta:
                reply["review_uncoded_no_delta"] = review_no_delta
        noop_rows = (
            {} if rows_untouched
            else _source_rows_by_plan(
                worktree_root, request.plan_path,
                [c.id for c in noop_chunks if c.id not in checkpoint_of],
            )
        )
        source_rows = (
            {request.plan_path: set()}
            if rows_untouched
            else _source_rows_by_plan(
                worktree_root, request.plan_path, [c.id for c in coded_chunks]
            )
        )
        partial_rows = _source_rows_by_plan(
            worktree_root, request.plan_path, [c.id for c in contributing_partial]
        )
        # Before the coded stamp: that commit carries the plan, so a stamp
        # minted here lands in it and costs no commit of its own.
        if record_abs is not None and record is not None and (
            request.plan_path in source_rows or request.plan_path in noop_rows
            or request.plan_path in partial_rows
            or any(request.plan_path in by_plan for by_plan in checkpoint_rows.values())
        ):
            reply.update(
                _mint_review_stamp(
                    worktree_root, request.plan_path, str(reply["sha"]), record_abs, record,
                    tree=reply.get("tree"),
                )
            )
        writeback = {} if anchor_only else _register_writeback(
            worktree_root, request.plan_path, inline_review, str(reply["sha"])
        )
        register_sizing = writeback.pop("register_sizing", None)
        reply.update(writeback)
        if (
            record_abs is not None and record is not None and not anchor_only
            and source_rows and request.plan_path not in source_rows
            and request.plan_path.endswith(_SPINE_SUFFIX)
        ):
            reply.update(
                _mint_mise_receipt(
                    worktree_root, request.plan_path, str(reply["sha"]), record_abs, record,
                    tree=reply.get("tree"),
                )
            )
        shipped_sizing = None
        if not anchor_only and not incomplete_chunks and not register_sizing:
            shipped = _ship_cited_sizing(worktree_root, repo_root, request.plan_path)
            shipped_sizing = shipped.pop("sizing_shipped", None)
            reply.update(shipped)
            if shipped_sizing:
                run_id = params.get("run_id") or (
                    ask_run_dir.name if ask_run_dir is not None else PurePosixPath(script_path_raw).stem
                )
                reply.update(_write_resolution(worktree_root, shipped_sizing, str(reply["sha"]), str(run_id)))
        also_commit = tuple(
            p for p in (
                request.plan_path if reply.get("review_stamp") == "minted" else None,
                reply.get("review_receipt"),
                register_sizing,
                shipped_sizing,
            ) if p
        )
        paying_close = close_ctx is not None and close_ctx["applies"]
        if close_ctx is not None:
            prep = inline_review.get("prep")
            base = prep.get("run_base_sha") if isinstance(prep, dict) else None
            close_ctx.update(
                review_receipt=reply.get("review_receipt")
                or (request.plan_path if reply.get("review_stamp") == "minted" else None),
                run_base_sha=base if isinstance(base, str) else None,
                terminal_sha=str(reply["sha"]),
            )
        if paying_close:
            also_commit += tuple(
                _pay_inline_legs(worktree_root, close_ctx, str(reply["sha"]), session_id, snapshot)
            )
        broken_tells = _falsifier_broken_tells(params.get("falsifier_broken"), request.plan_path)
        flips = bool(
            request.plan_path
            and reply.get("review_stamp") == "minted"
            and broken_tells is None
            and not register_failure
            and not incomplete_chunks
            and not unsent_memos
        )
        reply.update(
            _stamp_coded_commit(
                commit_v2, worktree_root, repo_root, source_rows,
                str(reply["sha"]), session_id,
                also_commit=also_commit,
                noop_rows=noop_rows, checkpoint_rows=checkpoint_rows,
                snapshot=snapshot,
                flip_plan=request.plan_path if flips else None,
                receipt=request.plan_path if reply.get("review_stamp") == "minted" else None,
                trailers=(f"Sizing-Shipped: {shipped_sizing} {reply['sha']}",) if shipped_sizing else (),
                commit=not product_only,
            )
        )
        flipped = reply.pop("_flip", None)
        scope["landed"].update(reply.pop("_committed", ()))
        if paying_close:
            _settle_close(worktree_root, close_ctx, "coded_stamp_error" not in reply)
        if shipped_sizing:
            reply["sizing_shipped"] = {"sizing": shipped_sizing, "sha": str(reply["sha"])}
        if resolves is not None:
            reply.update(_record_resolution(worktree_root, resolves, str(reply["sha"])))
        coded_stale = reply.pop("coded_index_stale", [])
        if coded_stale:
            reply["index_stale"] = sorted(set(reply.get("index_stale") or []) | set(coded_stale))
        if request.plan_path and reply.get("review_stamp") == "minted" and broken_tells is not None:
            reply["plan_status"] = "not-stamped"
            reply["plan_status_reason"] = (
                "falsifier integrity BROKEN at pre-dispatch ("
                + (", ".join(broken_tells) or "no tell named")
                + "): a met judge on this falsifier is not self-stamped; close it through the EM "
                "re-judge route (terminal-judge.md § Closing a plan whose run judged not_met)"
            )
        elif register_failure and reply.get("review_stamp") == "minted":
            reply["plan_status"] = "not-stamped"
            reply["plan_status_reason"] = register_failure
        elif flips and reply.get("coded_sha") and flipped is not None:
            reply.update(flipped)
        elif flips and product_only and "coded_stamp_error" not in reply:
            reply.update(_stamp_plan_implemented(worktree_root, request.plan_path, str(reply["sha"])))
        elif request.plan_path and reply.get("review_stamp") == "minted":
            reply["plan_status"] = "not-stamped"
            reply["plan_status_reason"] = (
                f"incomplete chunks remain: {sorted(incomplete_chunks)}"
                if incomplete_chunks
                else "memo not sent (no sender-side receipt): "
                + ", ".join(f"{rid} ({slug})" for rid, slug in sorted(unsent_memos.items()))
                if unsent_memos
                else "coded-stamp commit did not land"
            )
        criterion = inline_review.get("criterion")
        if isinstance(criterion, dict) and criterion.get("status"):
            reply["criterion_status"] = criterion["status"]
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
    if unsent_memos:
        reply["memo_unsent"] = dict(sorted(unsent_memos.items()))
    reply["prefix_files"] = prefix_files
    if doc_dropped:
        reply["doc_paths_dropped"] = doc_dropped
    if reply.get("committed") or reply.get("nothing_to_commit"):
        _with_siblings(reply)
    elif sibling_paths:
        reply["sibling_commits"] = [
            {"repo": root.name, "paths": paths, "committed": False,
             "error": "home commit did not land; sibling commit withheld"}
            for root, paths in sibling_paths.items()
        ]
    return reply
