"""Close-out records for dispatch.terminal_commit: cited sizing/baton, memo rows, resolution, usage and the close-job legs."""

from __future__ import annotations

import glob as _glob
import json
import re
from pathlib import Path, PurePosixPath
from typing import Optional
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.claimed_write import replace_text
from coordinator_core.session.record_homes import home_dir
from coordinator_core.warm.entry_seam import reentrant_dispatch
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest
from coordinator_core.ops.dispatch_emit.plan_row_coding import (
    yaml_load as _yaml_load,
    MINTED_SPINE_ORIGIN as _MINTED_SPINE_ORIGIN,
    inventory_record_rel as _inventory_record_rel,
    source_rows_by_plan as _source_rows_by_plan,
)
from coordinator_core.ops.dispatch_emit.register_gate import sizing_file_rel as _sizing_file_rel
from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot as _Snapshot, read_rel as _read_rel


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


def _cited_baton(worktree_root: Path, sizing_rel: Optional[str]) -> Optional[str]:
    """Worktree-relative path of the handoff the cited sizing names as its top-level ``baton:``,
    when it is an existing file under the handoffs home. In-process read: no spawn."""
    text = _read_rel(worktree_root, sizing_rel) if sizing_rel else None
    if text is None:
        return None
    import yaml

    try:
        doc = _yaml_load(text)
    except yaml.YAMLError:
        return None
    cited = doc.get("baton") if isinstance(doc, dict) else None
    if not isinstance(cited, str) or not cited:
        return None
    guarded = contained_path(worktree_root / cited, [Path(home_dir(str(worktree_root), "handoffs"))])
    if guarded is None or not guarded.is_file():
        return None
    return guarded.relative_to(worktree_root.resolve()).as_posix()


def _derive_judge_status(inline_review: object) -> Optional[str]:
    """Re-derive ``inline_review.criterion.status`` from its structured ``clauses``, in place.

    Any ``not_met`` clause gives ``not_met``; else any non-``met`` clause gives ``indeterminate``;
    a ``met`` reply whose ``clauses`` or ``observed`` list is present and empty is
    ``indeterminate`` (a reply that omits the key is a digest shape that never carried it, left
    alone). A derived status
    that differs from the reply's is written over it and the disagreement appended to
    ``criterion["deviations"]``; a derived status never upgrades a non-``met`` reply. Returns the
    deviation text, or ``None`` when the reply agrees or carries no judged criterion.
    """
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None
    clauses = criterion.get("clauses")
    if not isinstance(clauses, list):
        return None
    statuses = [c.get("status") if isinstance(c, dict) else None for c in clauses]
    if "not_met" in statuses:
        derived, why = "not_met", "a clause is not_met"
    elif any(s != "met" for s in statuses):
        derived, why = "indeterminate", "a clause is not met or indeterminate"
    elif not statuses:
        derived, why = "indeterminate", "met reply carries no clauses"
    elif "observed" in criterion and not criterion["observed"]:
        derived, why = "indeterminate", "met reply observed nothing"
    else:
        return None
    note = f"judge reply status 'met' disagrees with its clauses: derived {derived} ({why})"
    criterion["status"] = derived
    criterion["reason"] = note
    criterion["deviations"] = [*(criterion.get("deviations") or []), note]
    return note


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


#: Per-plan seam-check sidecar suffix; `plan.seam_record` writes `<plan-stem>` + this beside the plan
#: (prep phase) or `<plan-stem>.wave-<N>` + this (wave-boundary phase).
_SEAM_SIDECAR_SUFFIX = ".seam.yaml"

#: Mirrors `plan_tasks_mutate.EVIDENCE_SIDECAR_SUFFIX` (pinned by test): importing that module costs ~25ms.
_EVIDENCE_SIDECAR_SUFFIX = ".evidence.yaml"


def _seam_sidecars(worktree_root: Path, plan_path: Optional[str], chunk_ids: list) -> list:
    """Worktree-relative `.seam.yaml` sidecars on disk beside the run's plan and each
    source plan its committed rows came from, in sorted order."""
    if not plan_path:
        return []
    plans = {plan_path, *_source_rows_by_plan(worktree_root, plan_path, chunk_ids)}
    found = []
    for plan in sorted(plans):
        pure = PurePosixPath(plan)
        prep = str(pure.with_name(pure.stem + _SEAM_SIDECAR_SUFFIX))
        if (worktree_root / prep).is_file():
            found.append(prep)
        fire = str(pure.with_name(pure.stem + ".fire" + _SEAM_SIDECAR_SUFFIX))
        if (worktree_root / fire).is_file():
            found.append(fire)
        try:
            waves = sorted(
                p.name for p in (worktree_root / pure.parent).glob(f"{_glob.escape(pure.stem)}.wave-*{_SEAM_SIDECAR_SUFFIX}")
                if p.is_file()
            )
        except OSError:
            waves = []
        found.extend(str(pure.with_name(name)) for name in waves)
    return found


_MEMO_NO_SLUG = "no slug: declare receipt:"


def _memo_rows_without_receipt(worktree_root: Path, request: CommitRequest) -> dict:
    """``{row id: slug}`` of the plan's open memo-send rows whose sender-side receipt is absent.

    A memo row may be dispatched to an executor; it closes on
    ``memo-outbox/sent/<slug>.md`` existing, when ``_stamp_coded_commit`` resolves the row
    ``delivered_elsewhere`` with ``disposition_ref`` naming that receipt (a non-open row is not reported).
    An open row naming no slug maps to ``"no slug: declare receipt:"``, never dropped.
    Unreadable plan or spine: empty.
    """
    if not request.plan_path:
        return {}
    text = _read_rel(worktree_root, request.plan_path)
    if text is None:
        return {}
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.memo_rows import (
        _is_memo_send_row,
        memo_receipt_exists,
        memo_row_slug,
    )

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return {}
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return {}
    out: dict = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("performer") == "em":
            continue
        if (row.get("disposition") or "open") != "open" or not _is_memo_send_row(row):
            continue
        slug = memo_row_slug(row)
        if slug is None:
            out[str(row["id"])] = _MEMO_NO_SLUG
        elif not memo_receipt_exists(worktree_root, slug):
            out[str(row["id"])] = slug
    return out


def _memo_rows_to_close(worktree_root: Path, request: CommitRequest) -> dict:
    """``{plan: {row id: (receipt ref, receiver)}}`` of the run's chunk rows that are open
    memo-send rows with a sender-side receipt on disk. One stat per memo row, no spawn."""
    if not request.plan_path:
        return {}
    text = _read_rel(worktree_root, request.plan_path)
    if text is None:
        return {}
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.memo_rows import (
        _is_memo_send_row,
        memo_receipt_exists,
        memo_row_slug,
    )
    from coordinator_core.session.machinery_paths import MEMO_OUTBOX_RELDIR

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return {}
    try:
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return {}
    ids = {c.id for c in request.chunks}
    out: dict = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or str(row.get("id")) not in ids:
            continue
        if (row.get("disposition") or "open") != "open" or not _is_memo_send_row(row):
            continue
        slug = memo_row_slug(row)
        if slug is not None and memo_receipt_exists(worktree_root, slug):
            to = row.get("memo_to")
            out.setdefault(request.plan_path, {})[str(row["id"])] = (
                f"{MEMO_OUTBOX_RELDIR}/sent/{slug}.md",
                to.strip() if isinstance(to, str) and to.strip() else "its receiver",
            )
    return out


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
        doc = _yaml_load(text) or {}
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
    was already shipped, ``{"sizing_ship_skipped": reason}`` when ``sizing.ship`` refuses (a register
    not fully met, or a cascade-only sizing: it stays live and the deliverable cascade is the backstop);
    never raises -- the product commit stands. A register-bearing sizing ships here too: ``sizing.ship``
    runs ``requirement_register.ship_refusal`` itself over the rows ``_register_writeback`` just wrote.
    The sizing schema has no SHA field: the SHA rides the carrying commit's ``Sizing-Shipped`` trailer."""
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


def _dropped_part_rows(worktree_root: Path, sizing_rel: str, note_text: str) -> dict:
    """Mint an open ``relayed`` register row on ``sizing_rel`` for each part ``note_text`` records as
    not done, not investigated or out of scope with no ruling; the open row keeps ``sizing.ship``
    refusing. A part is ruled when its ``(label)`` row (or its row id) is ``waived`` or deferred by a
    ``workflow.revise`` ruling. ``{"closed_as": "partial", "dropped_parts": [{label, text, row_id}]}``
    when any part was found, ``{}`` otherwise; ``dropped_parts_error`` and no ``row_id`` when the
    write failed. Never raises."""
    from coordinator_core.ops import requirement_register as rr
    from coordinator_core.ops.dispatch_emit import requirements_ledger, stated_parts

    sizing_text = _read_rel(worktree_root, sizing_rel)
    register = rr.read_register(sizing_text) if sizing_text else None
    rows = register.rows if register is not None else []
    deferred = set(requirements_ledger.ruled_out_ids(rows))
    ruled: set = set()
    for row in rows:
        if row.get(rr.ROW_STATUS) != rr.STATUS_WAIVED and str(row.get(rr.ROW_ID)) not in deferred:
            continue
        ruled.add(str(row.get(rr.ROW_ID)))
        lead = re.match(r"\(([a-z])\)\s", str(row.get(rr.ROW_TEXT) or ""), re.IGNORECASE)
        if lead:
            ruled.add(lead.group(1).lower())
    dropped = stated_parts.dropped_parts(note_text, ruled=ruled)
    if not dropped:
        return {}
    texts = [
        f"({p.label}) {p.text}" if len(p.label) == 1 else f"{p.label}: {p.text}" for p in dropped
    ]
    error: Optional[str] = None
    on_disk: list = []
    try:
        on_disk = requirements_ledger._append_rows(
            worktree_root, sizing_rel, [(t, requirements_ledger.ORIGIN_RELAYED) for t in texts]
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced; the product commit stands
        error = str(exc)
    by_text = {str(r.get(rr.ROW_TEXT) or "").strip(): str(r.get(rr.ROW_ID)) for r in on_disk}
    out: dict = {
        "closed_as": "partial",
        "dropped_parts": [
            {"label": p.label, "text": p.text, "row_id": by_text.get(t.strip())}
            for p, t in zip(dropped, texts)
        ],
    }
    if error is not None:
        out["dropped_parts_error"] = error
    return out


USAGE_FILE = "usage.json"


def _write_usage(worktree_root: Path, session_id: Optional[str], run_id: str) -> dict:
    """Write ``state/warp/<run_id>/usage.json`` (``scan_run`` shape, ``as_of: terminal-commit``,
    ``unsettled`` agent ids) and return ``{total_tokens, usd}``, else ``{usage_total: None, usage_reason}``.

    Never raises: a run dir that does not resolve uniquely, or any I/O failure, leaves no file."""
    if not session_id:
        return {"usage_total": None, "usage_reason": "no session_id"}
    try:
        from coordinator_core.ops.dispatch_emit.work_label import parse_work_label
        from coordinator_core.usage import agent_usage, run_locate

        session_dir = run_locate.session_dir_for(session_id)
        wf = run_locate.locate_run_dir(session_dir, run_id) if session_dir is not None else None
        if wf is None:
            return {"usage_total": None, "usage_reason": "run dir not located"}
        scan = agent_usage.scan_run(str(wf))
        # Scan labels are `work:<row id>[#cont<n>]`, not bare row ids: parse back to the row id and
        # sum continuations. manifest.json stays untouched (its sha256 is pinned by manifest_handle).
        rows: dict = {}
        for agent in scan["agents"]:
            row_id = parse_work_label(agent.get("label") or "")
            if row_id is not None:
                entry = rows.setdefault(row_id, {"rag_calls": 0})
                entry["rag_calls"] += int(agent.get("rag_calls") or 0)
        body = {
            **scan,
            "rows": rows,
            "as_of": "terminal-commit",
            "unsettled": sorted(run_locate._unsettled_agent_ids(wf / "journal.jsonl")),
        }
        target = worktree_root / RECORD_DIR_ROOT / run_id / USAGE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_text(target, json.dumps(body, indent=2, sort_keys=True) + "\n")
        total = scan["run_total"]
        return {"usage_total": {"total_tokens": total["total_tokens"], "usd": total["usd"]}}
    except Exception as exc:  # noqa: BLE001 -- usage is a report, never a commit gate
        return {"usage_total": None, "usage_reason": f"usage scan failed: {exc!r}"}


def _run_id_of(params: dict, ask_run_dir: Optional[Path], script_path_raw: str) -> str:
    """Which run this commit is for: ``params.run_id``, else the ask run dir's name, else the
    script stem (a non-ask script's stem is its run id; an ask script's is ``<run_id>.workflow``)."""
    rid = params.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    if ask_run_dir is not None:
        return ask_run_dir.name
    return PurePosixPath(script_path_raw.replace("\\", "/")).stem


#: Script the close job runs as; ``None`` is ``ops/ceremony/close_background.py``. A budget
#: driver points it at a no-op so the gate measures the launch, not the job's own work.
CLOSE_JOB_SCRIPT: Optional[str] = None

_CLOSE_JOB_WINDOW_MINUTES = 5

_BACKGROUND_LEGS = (
    "review", "baton-drain", "orientation-refresh", "memo-lifecycle", "post-close", "symbol-knowledge",
)

_NOT_LANDED = "coded-stamp commit did not land"


def _leg(status: str, detail: str = "", root=None):
    """A ``LegResult`` whose detail is path-normalised: a path under *root* reads repo-relative,
    any other path-shaped token reads ``<path>``, and the rest of the text survives."""
    from coordinator_core.ops.dispatch_emit.run_close import LegResult, scrub_detail

    return LegResult(status, scrub_detail(detail, root))


def _plan_source_memo(worktree_root: Path, plan_rel: Optional[str]) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit.source_memo_ref import resolve_source_memo

    return resolve_source_memo(worktree_root, plan_rel)


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
        rows = _yaml_load(located.body) or []
    except yaml.YAMLError:
        return []
    return sorted(
        str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("deferred") is True
    ) if isinstance(rows, list) else []


def _run_own_baton(worktree_root: Path, run_id: str) -> Optional[str]:
    """The baton the run's gate minted and recorded, when it is a file inside the handoffs home."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return None
    home = [Path(home_dir(str(worktree_root), "handoffs"))]
    target = contained_path(worktree_root / rel, home)
    return rel if target is not None and target.is_file() else None


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
        result, entry_paths = completion_entry_leg(worktree_root, run_id, ctx["plan_rel"], criterion, ledger_met=ctx.get("ledger_met"))
    except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
        result, entry_paths = _leg("failed", f"completion-entry raised {type(exc).__name__}"), []
    legs["completion-entry"] = result
    if result.status in ("ran", "discharged"):
        ctx["completion_entry"] = (entry_paths or [result.detail.removeprefix("entry ")])[0]

    batons = _handoff_claims(worktree_root, session_id)
    status = criterion.get("status") if criterion else None
    saved: dict = {}
    written = list(entry_paths)
    discharged = status == "met" or (bool(ctx.get("ledger_met")) and status != "not_met")
    if discharged:
        own = _run_own_baton(worktree_root, run_id)
        if own and own not in batons:
            batons = [*batons, own]
    ctx["batons"] = batons
    if batons and not discharged:
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
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    script = _tc.CLOSE_JOB_SCRIPT or CLOSE_JOB_SCRIPT or str(engine / "coordinator_core" / "ops" / "ceremony" / "close_background.py")
    params = json.dumps({"claimed_batons": list(batons), "source_memo": memo})
    return spawn_detached(
        str(engine), script, [run_id, "--root", str(worktree_root), "--params", params],
        site_free=True,
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
            criterion=_judged if isinstance(_judged := ctx["inline_review"].get("criterion"), dict) else None,
            falsifier_broken=ctx.get("falsifier_broken"),
        )
    except (OSError, ValueError) as exc:
        legs.update({leg: _leg("failed", f"close job record not written: {type(exc).__name__}") for leg in queue})
        return legs
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    if not _tc._launch_close_job(worktree_root, run_id, batons, memo):
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


def _inline_paid_legs(reply: dict, root) -> dict:
    """The land-run-only legs a landed terminal commit paid inline, read off its reply:
    ``ran`` for a minted review stamp, a coded-stamp commit and a shipped sizing; ``failed``
    for a skipped sizing ship. A leg the reply is silent on is absent."""
    paid: dict = {}
    if reply.get("review_stamp") == "minted":
        paid["review-stamp"] = _leg("ran", "review stamp minted")
    if reply.get("coded_sha"):
        paid["coded-stamp"] = _leg("ran", "coded-stamp commit landed")
    if reply.get("sizing_shipped"):
        paid["sizing-ship"] = _leg("ran", "sizing shipped")
    elif reply.get("sizing_ship_skipped"):
        paid["sizing-ship"] = _leg("failed", f"sizing ship skipped: {reply['sizing_ship_skipped']}", root)
    return paid


def _close_block(ctx: dict, reply: dict) -> dict:
    """The reply's ``close`` block: the settled inline and background legs, with ``review`` and
    ``plan-reconciliation`` read off the reply, and every leg nobody settled ``failed`` with why."""
    from coordinator_core.ops.dispatch_emit.run_close import LAND_RUN_ONLY_LEGS, RUN_BOUND_LEGS, build_close_block

    legs = dict(ctx["legs"])
    if ctx["applies"]:
        already_landed = (
            reply.get("committed") is not True
            and bool(reply.get("nothing_to_commit"))
            and not reply.get("refused")
        )
        if already_landed:
            na = _leg("not-applicable", "already landed: no fresh diff to commit")
            if "review" not in legs and reply.get("review_stamp") != "refused":
                legs["review"] = na
            if (
                "plan-reconciliation" not in ctx["presets"]
                and "plan-reconciliation" not in legs
                and not reply.get("plan_status")
            ):
                legs["plan-reconciliation"] = na
            for leg in RUN_BOUND_LEGS:
                legs.setdefault(leg, na)
        if reply.get("committed") is not True:
            why = f"terminal commit did not land ({reply['refused']})" if reply.get("refused") else "terminal commit did not land"
        elif "coded_stamp_error" in reply:
            why = _NOT_LANDED
        else:
            why = "leg did not run"
        stamp = reply.get("review_stamp")
        if "review" not in legs:
            if stamp == "refused":
                legs["review"] = _leg(
                    "failed", str(reply.get("review_stamp_refusal") or "review stamp refused"), ctx["root"]
                )
            else:
                legs["review"] = _leg("failed", f"no review stamp minted: {why}" if why != "leg did not run" else "no review stamp minted")
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
        elif "plan-reconciliation" in ctx["presets"] or (
            already_landed and "plan-reconciliation" in legs
        ):
            pass  # the preset is applied below / the already-landed leg stands
        elif status:
            legs["plan-reconciliation"] = _leg(
                "failed", str(reply.get("plan_status_reason") or status), ctx["root"]
            )
        else:
            legs["plan-reconciliation"] = _leg("failed", f"plan not reconciled: {why}")
        landed_whole = reply.get("committed") is True and "coded_stamp_error" not in reply
        if landed_whole:
            legs.update({leg: result for leg, result in _inline_paid_legs(reply, ctx["root"]).items() if leg not in legs})
        for leg in RUN_BOUND_LEGS:
            if landed_whole and leg in LAND_RUN_ONLY_LEGS:
                continue  # unpaid on a landed run: build_close_block reads it not-applicable
            legs.setdefault(leg, _leg("failed", why))
        legs.update(ctx["presets"])
    from coordinator_core.ops.dispatch_emit.run_close import receipt_inputs
    from coordinator_core.ops.dispatch_emit.run_close_parts import close_from_sizing_text

    root = Path(ctx["root"])
    sizing_rel = _cited_sizing(root, ctx.get("plan_rel"))
    sizing_text = _read_rel(root, sizing_rel) if sizing_rel else None
    arm, tshirt = receipt_inputs(sizing_text)
    block = build_close_block(
        ctx["run_id"], ctx["applies"], legs, ctx["completion_entry"], kind=ctx["kind"],
        parts=close_from_sizing_text(sizing_text, ctx.get("ledger_met")),
        run_dir=Path(home_dir(str(root), "warp")) / (ctx.get("sibling_run_id") or ctx["run_id"]),
        arm=arm, tshirt=tshirt,
    )
    sha = reply.get("sha")
    if ctx["applies"]:
        from coordinator_core.ops.dispatch_emit.run_close import sibling_release

        landed = reply.get("committed") is True and isinstance(sha, str) and bool(sha)
        siblings = sibling_release(
            Path(ctx["root"]), ctx.get("sibling_run_id") or ctx["run_id"], sha if landed else None
        )
        if siblings is not None:
            block["siblings"] = siblings
    return block


def _release_siblings_at_landing(close_ctx: Optional[dict], sha: str) -> None:
    """Mark the run landed in its sibling-status file the moment the product commit lands.

    Runs before every post-commit leg so a worker death after the commit cannot strand the run's
    ``after`` siblings; ``_close_block``'s later call rewrites landed as landed. Only when the close
    applies (the predicate ``_close_block`` uses). A failure here never fails the landed commit.
    """
    if not close_ctx or not close_ctx.get("applies"):
        return
    from coordinator_core.ops.dispatch_emit.run_close import sibling_release

    try:
        sibling_release(Path(close_ctx["root"]), close_ctx.get("sibling_run_id") or close_ctx["run_id"], sha)
    except Exception:  # noqa: BLE001 -- _close_block repeats the release; the commit stands
        pass
