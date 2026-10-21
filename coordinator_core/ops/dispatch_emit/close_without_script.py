"""The no-script close: pay a landed run's close legs from the plan and the close-job record.

``dispatch.terminal_commit`` routes here when it is given ``plan_path`` and ``run_id`` and no
``script_path``. Nothing here reads the emitted script. The judge's verdict comes from three
stores in a fixed order (close-job record, the run's review record, a supplied task output) and
is never inferred; every leg reads the one ``Verdict`` ``read_verdict`` returns. The module
writes no file itself: the close-job record goes through ``run_close.amend_close_job``, the plan,
sizing and completion entry through the ``terminal_commit`` helpers it reuses.
"""

from __future__ import annotations

import copy
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple, Optional

_JUDGED = ("met", "not_met", "indeterminate")
_PAID = ("ran", "discharged")
_FIELD = "\x1f"
_MULTI = "\x1e"


class Verdict(NamedTuple):
    criterion: dict
    source: str
    rejudged_by: Optional[str]
    falsifier_known: bool
    falsifier: Optional[list]


class CloseRefusal(Exception):
    """A named refusal; the caller turns it into ``_error(message, refused=reason)``."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _norm(path: Any) -> str:
    text = str(path or "").replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _run_key(stem: str) -> str:
    from coordinator_core.ops.dispatch_emit.run_close import bare_run_id

    return bare_run_id(stem)


def _bare_stem(text: Any) -> bool:
    return isinstance(text, str) and bool(text) and text != ".." and "/" not in text and "\\" not in text


def _digest(task_output_path: Optional[str], run_id: str) -> Optional[dict]:
    """The supplied task output's ``next_action.params``, bound to this run by its script stem,
    or by ``run_id`` when it names no script (a ``close_job``-kind action)."""
    if not task_output_path:
        return None
    from coordinator_core.ops.dispatch_emit.terminal_commit import _params_from_task_output

    try:
        found = _params_from_task_output(task_output_path)
    except ValueError as exc:
        raise CloseRefusal(
            "task-output-run-mismatch",
            f"{exc}. Pass the run's own task-output file, or omit task_output_path.",
        ) from exc
    script = found.get("script_path")
    stem = PurePosixPath(script.replace("\\", "/")).stem if isinstance(script, str) and script else None
    if stem is None:
        bound = found.get("run_id")
        if not isinstance(bound, str) or not bound:
            raise CloseRefusal(
                "task-output-run-mismatch",
                f"{task_output_path!r} names no script_path or run_id, so it is not bound to run {run_id!r}. "
                "Pass the run's own task-output file, or omit task_output_path.",
            )
        stem = bound
    if _run_key(stem) != _run_key(run_id):
        raise CloseRefusal(
            "task-output-run-mismatch",
            f"{task_output_path!r} is run {stem!r}'s output, not {run_id!r}. "
            "Pass the run's own task-output file, or omit task_output_path.",
        )
    return {**_digest_extras(task_output_path), **found}


def _digest_extras(path_raw: str) -> dict:
    """``inline_review``, ``falsifier_broken`` and ``run_base_sha`` the digest's result carries beside
    ``next_action``: a ``close_job`` action's own params hold none of them."""
    import json

    try:
        data = json.loads(Path(path_raw).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("inline_review", "falsifier_broken", "run_base_sha") if data.get(k) is not None}


def _rejudge(repo_root: Path, plan_id: str, review: Optional[dict]) -> Optional[tuple]:
    """``mint``'s re-judge rule: the newest recorded criterion after the review record, if ``met``."""
    if review is None:
        return None
    from datetime import datetime, timezone

    from coordinator_core.ops.dispatch_emit.verdict_supersession import _instant, latest_criterion_record

    floor = _instant(review.get("recorded_at")) or datetime.min
    found = latest_criterion_record(repo_root, plan_id, floor.replace(tzinfo=floor.tzinfo or timezone.utc))
    verdict = found["criterion"] if found else None
    if verdict and verdict.get("status") == "met" and str(verdict.get("observation") or "").strip():
        return {**verdict, "sidecar": found["record_path"]}, found["record_path"]
    return None


def read_verdict(
    repo_root: Path, record: dict, review: Optional[dict], task_output_path: Optional[str],
    run_id: str, plan_path: str, plan_id: str,
) -> Verdict:
    """The recorded judge verdict that governs the run, or a ``CloseRefusal``.

    The first source with a judged status wins: the close-job record, the review record (only
    when it belongs to this plan), the supplied task output. An unmet verdict is replaced by a
    recorded ``met`` re-judge. A record with no ``falsifier_broken`` key (written before the key
    existed) reads as never marked only when such a re-judge governs the plan; otherwise the
    mark stays unknown.
    """
    digest = _digest(task_output_path, run_id)
    inline = digest.get("inline_review") if digest else None
    sources = (
        ("close-job", record.get("criterion")),
        (
            "review-record",
            review.get("criterion") if review is not None and str(review.get("plan_id") or "") == plan_id else None,
        ),
        ("task-output", inline.get("criterion") if isinstance(inline, dict) else None),
    )
    for source, criterion in sources:
        if isinstance(criterion, dict) and criterion.get("status") in _JUDGED:
            break
    else:
        raise CloseRefusal(
            "no-judge-verdict",
            f"no judge verdict in the close-job record, the review record or the task output for {run_id!r}. "
            "Pass task_output_path=<the run's task-output file>.",
        )
    rejudged_by = None
    replaced = _rejudge(repo_root, plan_id, review)
    if criterion["status"] != "met" and replaced is not None:
        criterion, rejudged_by = replaced
    if "falsifier_broken" in record:
        marked = record["falsifier_broken"]
        known, falsifier = True, (list(marked) if isinstance(marked, list) else None)
    elif digest is not None:
        from coordinator_core.ops.dispatch_emit.terminal_commit import _falsifier_broken_tells

        known, falsifier = True, _falsifier_broken_tells(digest.get("falsifier_broken"), plan_path)
    elif replaced is not None:
        known, falsifier = True, None
    else:
        known, falsifier = False, None
    return Verdict(criterion, source, rejudged_by, known, falsifier)


def _is_landed(worktree_root: Path, sha: Any) -> bool:
    from coordinator_core.git.run import run_git

    if not isinstance(sha, str) or len(sha) != 40:
        return False
    return run_git(["-C", str(worktree_root), "merge-base", "--is-ancestor", sha, "HEAD"]).returncode == 0


def _review_record(worktree_root: Path, sha: str) -> tuple[Optional[Path], Optional[dict]]:
    """The review record the terminal commit's ``Inline-Review`` trailer names, read from that one sha."""
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.review_stamp import (
        _APPLIES_RE, _BODY_INLINE_REVIEW_RE, _find_sidecar_by_stem, _load_sidecar,
    )

    fmt = (
        "--format=%(trailers:key=Session-Id,valueonly=true,unfold=true,separator=%x1e)%x1f"
        "%(trailers:key=Inline-Review,valueonly=true,unfold=true,separator=%x1e)%x1f%B"
    )
    shown = run_git(["-C", str(worktree_root), "log", "-1", fmt, sha])
    if shown.returncode != 0:
        return None, None
    session, _, rest = shown.stdout.partition(_FIELD)
    trailer, _, body = rest.partition(_FIELD)
    anchors = [t for t in trailer.split(_MULTI) if t]
    anchors += [m.group(1) for m in map(_BODY_INLINE_REVIEW_RE.match, body.splitlines()) if m]
    stem = next((m.group(1) for m in (_APPLIES_RE.match(a.strip()) for a in anchors) if m), None)
    session = session.strip()
    if not _bare_stem(stem):
        return None, None
    path = None
    if _bare_stem(session):
        own = worktree_root / ".coordinator-local" / "subagent-share" / session / f"{stem}.md"
        path = own if own.is_file() else None
    if path is None:
        path = _find_sidecar_by_stem(worktree_root, stem, session or None)
    return (path, _load_sidecar(path)) if path is not None else (None, None)


def _plan_frontmatter(text: Optional[str]) -> dict:
    import yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(text.replace("\r\n", "\n")) if text is not None else None
    if split is None:
        return {}
    try:
        data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _commit_v2(params: dict, root: Path):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    return terminal_commit.reentrant_dispatch("ceremony.commit_v2", params, repo_root=root)


def _holds_valid_stamp(
    worktree_root: Path, plan_rel: str, plan_fm: dict, terminal_sha: str, judged: str,
) -> bool:
    """Whether the plan's own ``review_stamp`` is for this run's terminal commit, records the
    verdict being closed on, and ``check`` accepts it."""
    from coordinator_core.ops.review_stamp import check

    stamp = plan_fm.get("review_stamp")
    if not isinstance(stamp, dict) or stamp.get("terminal_commit_sha") != terminal_sha:
        return False
    criterion = stamp.get("criterion")
    if not isinstance(criterion, dict) or criterion.get("status") != judged:
        return False
    return check(worktree_root / plan_rel, worktree_root) is None


def _stamp_refusal(
    worktree_root: Path, review_path: Optional[Path], review: Optional[dict], plan_id: str,
    plan_rel: str, judged: str,
) -> Optional[str]:
    """Why the review stamp must not be minted over this verdict, else ``None``."""
    if review is None or review_path is None:
        return "no review record is readable for the terminal commit. review-stamp mint --repair rebuilds one"
    if str(review.get("plan_id") or "") != plan_id:
        return f"review record plan_id {review.get('plan_id')!r} is not this plan's {plan_id!r}"
    from coordinator_core.ops.dispatch_emit.verdict_supersession import latest_delivery_supersession

    try:
        record_rel = review_path.relative_to(worktree_root).as_posix()
    except ValueError:
        record_rel = None
    if record_rel is not None and latest_delivery_supersession(worktree_root, record_rel) is not None:
        return "the review record has a re-verified delivery; mint it at the re-verified head with review-stamp mint"
    criterion = review.get("criterion")
    held = criterion.get("status") if isinstance(criterion, dict) else None
    if held != "met":
        replaced = _rejudge(worktree_root, plan_id, review)
        if replaced is not None:
            held = "met"
    if held != judged:
        return (
            f"review record criterion is {held}, the resolved verdict is {judged}. "
            f"Record a re-judge: emit-dispatch-workflow --plan {plan_rel} --rejudge --out <x>.workflow.mjs"
        )
    return None


def close_landed_run(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    """Pay the close legs of a run whose terminal commit already landed.

    Reads ``plan_path``, ``run_id``, ``task_output_path`` and ``session_id`` from *params*.
    Every refusal returns before any write.
    """
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit import run_close
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _read_rel

    plan_rel, run_id = params.get("plan_path"), params.get("run_id")
    session_id = params.get("session_id")
    session_id = session_id if isinstance(session_id, str) and _UUID_RE.fullmatch(session_id) else None
    task_output_path = params.get("task_output_path") or None
    try:
        if not _bare_stem(run_id):
            raise CloseRefusal(
                "no-close-job",
                f"run_id {run_id!r} is not a bare file stem. Pass the close-job record's stem "
                "(the emitted script's file name without .mjs).",
            )
        stem = run_close.resolve_close_job_stem(worktree_root, run_id)
        if stem is None:
            raise CloseRefusal(
                "no-close-job",
                f"no close-job record at {run_close.close_job_path(run_id)} or "
                f"{run_close.close_job_path(run_id + '.workflow')}. "
                "A run that has not landed closes through dispatch.terminal_commit with script_path.",
            )
        run_id = stem
        record = run_close._load(worktree_root, run_id)
        if not isinstance(plan_rel, str) or _norm(record.get("plan_rel")) != _norm(plan_rel):
            raise CloseRefusal(
                "close-job-plan-mismatch",
                f"{run_close.close_job_path(run_id)} belongs to plan {record.get('plan_rel')!r}, not {plan_rel!r}. "
                "Pass that plan's path.",
            )
        terminal_sha = record.get("terminal_sha")
        if not _is_landed(worktree_root, terminal_sha):
            raise CloseRefusal(
                "terminal-commit-not-landed",
                f"terminal_sha {terminal_sha!r} is absent or not an ancestor of HEAD. "
                "Land the run's terminal commit first.",
            )
        plan_text = _read_rel(worktree_root, plan_rel)
        if plan_text is None:
            raise CloseRefusal("close-job-plan-mismatch", f"plan {plan_rel!r} is not readable. Pass a tracked plan path.")
        plan_fm = _plan_frontmatter(plan_text)
        plan_id = str(plan_fm.get("plan_id") or "")
        review_path, review = _review_record(worktree_root, terminal_sha)
        verdict = read_verdict(worktree_root, record, review, task_output_path, run_id, plan_rel, plan_id)
        run_base = (_digest(task_output_path, run_id) or {}).get("run_base_sha")
    except CloseRefusal as exc:
        return _error(exc.message, refused=exc.reason)
    return _pay(
        worktree_root, repo_root, session_id, plan_rel, run_id, record, plan_text, plan_fm, plan_id,
        review_path, review, verdict, run_base=run_base if isinstance(run_base, str) else None,
    )


_GATE_REASON_PREFIXES = ("register row ", "PM turn ")


def _ledger_judged(worktree_root: Path, run_id: str, record: dict) -> list:
    """Register row ids the run's requirements ledger judged: the record's stored inputs, else the
    ledger read off the run's record directory."""
    inputs = record.get("inputs") if isinstance(record.get("inputs"), dict) else {}
    stored = inputs.get("ledger_judged")
    if isinstance(stored, list):
        return [str(i) for i in stored]
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    try:
        ledger = requirements_ledger.read_ledger(
            worktree_root, requirements_ledger.run_dir(worktree_root, _run_key(run_id))
        )
    except (requirements_ledger.LedgerRefused, OSError):
        return []
    return [str(i.id) for i in ledger.items] if ledger is not None else []


def _regated(worktree_root: Path, plan_rel: str, run_id: str, record: dict, criterion: dict) -> tuple:
    """``(criterion, register_failure, status before the gate)``: a ``not_met`` the register gate itself wrote (reason equal to
    observation, a gate reason prefix, the judge's ``register_rows`` still on the criterion) is
    re-judged from those rows through today's gate with the ledger's judged ids, never read back as
    the stored status. Any other criterion is gated as it stands."""
    from coordinator_core.ops import requirement_register as rr
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    gated = copy.deepcopy(criterion)
    reason = gated.get("reason")
    from_gate = (
        gated.get("status") == "not_met"
        and isinstance(reason, str) and reason == gated.get("observation")
        and reason.startswith(_GATE_REASON_PREFIXES)
        and isinstance(gated.get(rr.JUDGE_ROWS_KEY), list)
    )
    if from_gate:
        gated["status"] = "met"
        gated.pop("reason", None)
        gated.pop("observation", None)
    ahead = gated["status"]
    failure = tc._register_gate(
        worktree_root, plan_rel, {"criterion": gated}, _ledger_judged(worktree_root, run_id, record)
    )
    return gated, failure, ahead


def _pay(
    worktree_root: Path, repo_root: Path, session_id: Optional[str], plan_rel: str, run_id: str,
    record: dict, plan_text: str, plan_fm: dict, plan_id: str, review_path: Optional[Path],
    review: Optional[dict], verdict: Verdict, run_base: Optional[str] = None,
) -> dict:
    import re
    from datetime import datetime, timedelta

    from coordinator_core.ops.ceremony import close_background
    from coordinator_core.ops.dispatch_emit import run_close, terminal_commit as tc
    from coordinator_core.ops.dispatch_emit.completion_receipt import _remove
    from coordinator_core.ops.dispatch_emit.run_close_completion import completion_entry_leg
    from coordinator_core.ops.dispatch_emit.run_close_lessons import lessons_leg

    terminal_sha = record["terminal_sha"]
    gated, register_failure, judged = _regated(worktree_root, plan_rel, run_id, record, verdict.criterion)
    now = datetime.now().astimezone()
    batons = _claimed_batons(worktree_root, run_id, record)
    try:
        held = run_close._parse_close(record, run_id, now)
    except ValueError:
        held = run_close.RunClose(run_id)
    held_legs = dict(held.legs or {})
    entry_state = held_legs.get("completion-entry")
    entry_paid = entry_state is not None and entry_state.status in _PAID
    review_leg = held_legs.get("review")
    stamp_present = isinstance(plan_fm.get("review_stamp"), dict)
    already = stamp_present and plan_fm.get("status") == "implemented"
    base = {"criterion_status": judged, "verdict_source": verdict.source}
    if verdict.rejudged_by:
        base["rejudged_by"] = verdict.rejudged_by
    owes_nothing = (
        already
        and bool(record.get("review_receipt"))
        and review_leg is not None and review_leg.status != "failed"
        and (entry_paid or judged != "met")
    )
    if owes_nothing:
        return {
            **base, "committed": False, "nothing_to_commit": True,
            "review_stamp": "minted", "plan_status": "implemented",
        }

    amend: dict = {}
    if gated != record.get("criterion"):
        amend["criterion"] = gated
    if "falsifier_broken" not in record and verdict.falsifier_known:
        amend["falsifier_broken"] = verdict.falsifier
    if amend:
        run_close.amend_close_job(worktree_root, run_id, **amend)

    reply: dict = {**base}
    reply["criterion_status"] = gated["status"]
    minted = already or _holds_valid_stamp(worktree_root, plan_rel, plan_fm, terminal_sha, gated["status"])
    if minted:
        reply["review_stamp"] = "minted"
    else:
        refusal = _stamp_refusal(worktree_root, review_path, review, plan_id, plan_rel, judged)
        if refusal is None:
            reply.update(tc._mint_review_stamp(worktree_root, plan_rel, terminal_sha, review_path, review))
            minted = reply.get("review_stamp") == "minted"
        else:
            reply.update(review_stamp="refused", review_stamp_refusal=refusal)
    # A refused close writes no register row or entry: nothing is left behind for a commit to miss.
    writeback: dict = {}
    if minted:
        writeback = tc._register_writeback(worktree_root, plan_rel, {"criterion": gated}, terminal_sha)
    reply.update(writeback)
    fresh = minted and not already
    if fresh:
        stamped = (_plan_frontmatter(tc._read_rel(worktree_root, plan_rel)).get("review_stamp") or {}).get("criterion")
        stamp_status = stamped.get("status") if isinstance(stamped, dict) else None
        if stamp_status and stamp_status != judged:
            reply["stamp_criterion_status"] = stamp_status

    entry_paths: list = []
    entry_result = entry_state
    if not entry_paid and not minted:
        entry_result = tc._leg("failed", "review stamp refused; no completion entry written")
    elif not entry_paid:
        try:
            entry_result, entry_paths = completion_entry_leg(
                worktree_root, run_id, plan_rel, gated, ledger_met=(record.get("inputs") or {}).get("ledger_met"),
            )
        except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
            entry_result, entry_paths = tc._leg("failed", f"completion-entry raised {type(exc).__name__}"), []

    ship_state = held_legs.get("baton-ship")
    ship_paid = ship_state is not None and ship_state.status in _PAID
    ship_paths: list = []
    ship_saved: dict = {}
    ship_result = None
    if batons and not ship_paid and minted and judged == "met":
        from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg

        ship_saved = {rel: tc._read_rel(worktree_root, rel) for rel in batons}
        try:
            ship_result = baton_ship_leg(worktree_root, run_id, batons, terminal_sha)
        except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
            ship_result = tc._leg("failed", f"baton-ship raised {type(exc).__name__}")
        if ship_result.status == "ran":
            ship_paths = list(batons)
        else:
            tc._restore_texts(worktree_root, ship_saved)

    also_commit = tuple(
        p for p in (
            plan_rel if fresh else None,
            writeback.get("register_sizing"),
            *entry_paths,
            *ship_paths,
        ) if p
    )
    withheld = None
    if fresh:
        if verdict.falsifier is not None:
            withheld = (
                "falsifier integrity BROKEN at pre-dispatch ("
                + (", ".join(verdict.falsifier) or "no tell named")
                + "): a met judge on this falsifier is not self-stamped; close it through the EM "
                "re-judge route (terminal-judge.md § Closing a plan whose run judged not_met)"
            )
        elif register_failure:
            withheld = register_failure
        elif not verdict.falsifier_known:
            withheld = (
                "the falsifier mark is unknown for this run; pass task_output_path=<the run's "
                "task-output file> to supply it"
            )
    flip = fresh and withheld is None
    stamped_result = tc._stamp_coded_commit(
        _commit_v2, worktree_root, repo_root, {plan_rel: set()}, terminal_sha, session_id,
        also_commit=also_commit, flip_plan=plan_rel if flip else None, receipt=None,
    ) if also_commit else {"rows_coded": {}, "coded_sha": None}
    flipped = stamped_result.pop("_flip", None)
    stamped_result.pop("_committed", None)
    failed_commit = "coded_stamp_error" in stamped_result
    written_entries = list(entry_paths)
    if failed_commit:
        _remove(worktree_root, entry_paths)
        entry_paths = []
        if ship_paths:
            tc._restore_texts(worktree_root, ship_saved)
    coded_sha = stamped_result.get("coded_sha")
    reply["committed"] = bool(coded_sha)
    reply["sha"] = coded_sha
    if failed_commit:
        reply["coded_stamp_error"] = stamped_result["coded_stamp_error"]
    elif not coded_sha:
        reply["nothing_to_commit"] = True

    on_disk = _plan_frontmatter(tc._read_rel(worktree_root, plan_rel))
    if withheld is not None:
        reply.update(plan_status="not-stamped", plan_status_reason=withheld)
    elif flipped is not None:
        reply.update(flipped)
    elif already or (minted and not failed_commit):
        reply["plan_status"] = str(on_disk.get("status") or "not-stamped")
        if reply["plan_status"] != "implemented":
            reply["plan_status_reason"] = "no path was stageable for the flip"
    elif minted:
        reply.update(plan_status="not-stamped", plan_status_reason="coded-stamp commit did not land")
    else:
        reply.update(plan_status="not-stamped", plan_status_reason=reply.get("review_stamp_refusal", ""))
    if reply.get("plan_status") == "not-stamped" and minted and not withheld:
        from coordinator_core.ops.review_stamp import criterion_refusal

        from coordinator_core.frontmatter.primitives import split_frontmatter

        split = split_frontmatter((tc._read_rel(worktree_root, plan_rel) or "").replace("\r\n", "\n"))
        unmet = criterion_refusal(worktree_root / plan_rel, split.fm_text) if split is not None else None
        if unmet:
            reply["plan_status_reason"] = unmet

    legs: dict = {}
    if not entry_paid:
        legs["completion-entry"] = tc._leg("failed", tc._NOT_LANDED) if written_entries and failed_commit else entry_result
    if ship_result is not None:
        legs["baton-ship"] = tc._leg("failed", tc._NOT_LANDED) if ship_paths and failed_commit else ship_result
    if failed_commit:
        legs["lessons"] = tc._leg("failed", tc._NOT_LANDED)
    else:
        try:
            legs["lessons"] = lessons_leg(worktree_root, run_id, gated)
        except Exception as exc:  # noqa: BLE001 -- a leg failure is reported, never raised
            legs["lessons"] = tc._leg("failed", f"lessons raised {type(exc).__name__}")

    receipted = already or minted
    write: dict = {}
    if receipted and not record.get("review_receipt"):
        write["review_receipt"] = plan_rel
    paid_here = legs.get("completion-entry")
    if paid_here is not None and not entry_paid and paid_here.status in _PAID and not failed_commit:
        write["settled"] = {"completion-entry": paid_here}
    shipped_here = legs.get("baton-ship")
    if shipped_here is not None and shipped_here.status in _PAID and not failed_commit:
        write["settled"] = {**write.get("settled", {}), "baton-ship": shipped_here}
    queue_review = receipted and (review_leg is None or review_leg.status == "failed")
    if queue_review and not record.get("run_base_sha"):
        if run_base and re.fullmatch(r"[0-9a-f]{40}", run_base):
            write["run_base_sha"] = run_base
        else:
            queue_review = False
            why = "close-job record and digest carry no run_base_sha; review range not computed"
            if review_leg is None:
                write["settled"] = {**write.get("settled", {}), "review": tc._leg("not-applicable", why)}
            else:
                run_close.record_leg_outcome(worktree_root, run_id, "review", "not-applicable", why)
    if queue_review:
        write["queue"] = ["review"]
        write["deadline"] = now + timedelta(minutes=tc._CLOSE_JOB_WINDOW_MINUTES)
    if write:
        run_close.amend_close_job(worktree_root, run_id, **write)
    if queue_review:
        launch = close_background._context(
            worktree_root, Path(repo_root),
            {"run_id": run_id, "claimed_batons": batons, "source_memo": record.get("source_memo")},
        )
        if not close_background._launch_close_review(launch):
            run_close.record_leg_outcome(worktree_root, run_id, "review", "failed", "close.review did not start")

    reply["close"] = _close(worktree_root, run_id, plan_rel, plan_text, reply, legs, entry_paths, now, batons)
    return reply


def _claimed_batons(worktree_root: Path, run_id: str, record: dict) -> list:
    """The close-job record's claimed batons, else the one baton the run's gate recorded at emit."""
    recorded = [str(b) for b in record.get("claimed_batons") or []]
    if recorded:
        return recorded
    from coordinator_core.ops.dispatch_emit.terminal_commit import _run_own_baton

    own = _run_own_baton(worktree_root, run_id)
    return [own] if own else []


def _close(
    worktree_root: Path, run_id: str, plan_rel: str, plan_text: str, reply: dict, paid: dict,
    entry_paths: list, now: Any, batons: list,
) -> dict:
    from coordinator_core.ops.dispatch_emit import run_close, terminal_commit as tc

    record = run_close._load(worktree_root, run_id) or {}
    try:
        legs = dict(run_close._parse_close(record, run_id, now).legs or {})
    except ValueError:
        legs = {}
    legs.update(paid)
    if not batons:
        legs["baton-drain"] = tc._leg("not-applicable", "run claims no baton")
    if not record.get("source_memo"):
        legs["memo-lifecycle"] = tc._leg("not-applicable", "plan names no source memo")
    kind, plan_governs = tc._close_kind(plan_text)
    presets = run_close.kind_presets(kind, plan_governs=plan_governs)
    ctx = {
        "root": worktree_root, "run_id": run_id, "applies": True, "plan_rel": plan_rel,
        "legs": {**presets, **legs}, "kind": kind, "presets": presets,
        "completion_entry": (entry_paths or [record.get("completion_entry")])[0],
    }
    # A kept stamp whose plan flip leaves the tree equal to HEAD has nothing to commit: the close is
    # already landed there, not "the terminal commit did not land".
    settled = reply.get("nothing_to_commit") and reply.get("review_stamp") == "minted" and "coded_stamp_error" not in reply
    return tc._close_block(ctx, {**reply, "committed": True} if settled else reply)
