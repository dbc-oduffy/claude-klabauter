"""Requirements-ledger gate and regrading for dispatch.terminal_commit: withheld work, report grading, residue and entanglement."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path, PurePosixPath
from typing import Sequence, Optional
from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit import (
    bug_close,
    halt_baseline,
    gate_owed,
    review_floor,
    credit_gate,
    pre_halt_credit,
)
from coordinator_core.ops.dispatch_emit.land_run import (
    _unclosed_halt_gaps,
    _halt_gap_lines,
    _em_discharged_rows,
    held_revision_refusal,
)
from coordinator_core.ops.dispatch_emit.halt_landing import (
    landed_before_halt as _landed_before_halt,
    since_halt_close as _since_halt_close,
)
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import (
    cited_at_or_before,
    CITED_DETAIL_KEY,
    verified_post_commit_credit,
)
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_UNLANDED_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
    RUN_DIR_ROOT,
    RECORD_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.commit_request import PREFIX_CLAIM_LABEL, CommitRequest
from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot as _Snapshot, read_rel as _read_rel

from coordinator_core.ops.dispatch_emit.terminal_commit_closeout import _EVIDENCE_SIDECAR_SUFFIX
from coordinator_core.ops.dispatch_emit.terminal_commit_checkpoints import _FULL_SHA_RE
from coordinator_core.ops.dispatch_emit.terminal_commit_review import (
    _clean_review_files,
    _reviewed_files_if_passed,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import (
    _error,
    _measured_rev_drift_refusal,
    _pre_existing_dirt_refusal,
    _readable_manifest,
    _recovery_withheld_reasons,
)


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


def _expanded_prefix_files(
    worktree_root: Path, chunk, snapshot: Optional["_Snapshot"], session_id: Optional[str],
    touched: frozenset, *, left_out: Optional[dict] = None,
) -> list:
    """Existing changed files under ``chunk``'s own ``writes_under`` prefixes that ``touched``
    (this run's own touched set) names and no live peer contests, read from the pre-commit
    ``snapshot`` (no git spawn). Empty with no snapshot, no session id or no prefixes. Admission
    is the run's touched set, never a session claim: a sibling run's file under a shared prefix
    is not staged and surfaces in ``undeclared_dirty``. ``session_id`` only keys the live-peer
    contest. ``left_out``, when given, receives the ``{path: [sid]}`` it dropped as contested."""
    if snapshot is None or not session_id or not chunk.prefixes:
        return []
    found = sorted(
        p
        for p in snapshot.changed_among(list(chunk.prefixes))
        if p in touched and (worktree_root / p).is_file()
    )
    if not found:
        return []
    from coordinator_core.session import scope as session_scope

    root_str = str(worktree_root)
    contested = session_scope.contested_by_live_peers(found, session_id, cwd=root_str)
    if contested is None:
        return []
    if left_out is not None:
        left_out.update({p: list(contested[p]) for p in found if p in contested})
    return [p for p in found if p not in contested]


def _with_expansion(claimed: Optional[list], expanded: list) -> Optional[list]:
    """The report's claimed prefix files joined with the snapshot expansion; ``None`` (the
    no-claim-list refusal) only when the report has no claim list and the expansion is empty."""
    if claimed is None:
        return list(expanded) or None
    return claimed + [p for p in expanded if p not in claimed]


def _routed_from_manifest(manifest_path: Optional[Path]) -> dict:
    """``{id: engine root}`` the stage manifest routed; ``{}`` when unreadable (fail-open to the
    cwd-only gate)."""
    if manifest_path is None:
        return {}
    from coordinator_core.ops.dispatch_emit.engine_owned_rows import routed_roots

    try:
        return routed_roots(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


# Closed list: a corpus the run machinery writes must be named here, or a declared write into it counts as product.
_PROCESS_STATE_CORPORA = frozenset({"sizings", "warp", "handoffs", "apm-rulings", "queue-grind"})


def _is_process_state(rel: str) -> bool:
    """True for a ``state/`` path the run machinery itself writes: a file directly under
    ``state/`` or anything under a process corpus. Any other ``state/<corpus>/...`` record is
    product the chunk produced."""
    if not rel.startswith("state/"):
        return False
    parts = rel.split("/")
    return len(parts) < 3 or parts[1] in _PROCESS_STATE_CORPORA


def _manifest_declared(manifest_path: Optional[Path]) -> tuple:
    """``(declared product paths, manifest head_sha)`` from one read of the manifest: the `writes`
    of its rows minus run-record paths and the run's own process state (``_is_process_state``), and
    the staging HEAD (None when absent or not a full sha). A write into a state corpus such as
    ``state/bug-backlog/`` is product. ``([], None)`` when the manifest is unreadable."""
    if manifest_path is None:
        return [], None
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        rows = data.get("rows")
    except (OSError, ValueError, AttributeError):
        return [], None
    skip = (RECORD_DIR_ROOT.rstrip("/") + "/", RUN_DIR_ROOT.rstrip("/") + "/")
    paths: list = []
    for row in rows if isinstance(rows, list) else ():
        writes = row.get("writes") if isinstance(row, dict) else None
        for w in writes if isinstance(writes, list) else ():
            if isinstance(w, str) and w and not w.startswith(skip) and not _is_process_state(w) and w not in paths:
                paths.append(w)
    head = data.get("head_sha")
    return paths, head if isinstance(head, str) and _FULL_SHA_RE.fullmatch(head) else None


def verified_only_rows(resume: object) -> dict:
    """``{row_id: {command, observed}}`` from a ledger resume's ``verified_only``: rows whose only
    remainder was a check, discharged by one that ran. An entry missing either string is no
    exemption -- a bare claim is what the unchanged-since-halt guard exists to refuse."""
    raw = resume.get("verified_only") if isinstance(resume, dict) else None
    out: dict = {}
    for row_id, ev in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(ev, dict):
            continue
        command, observed = ev.get("command"), ev.get("observed")
        if isinstance(command, str) and command.strip() and isinstance(observed, str) and observed.strip():
            out[str(row_id)] = {"command": command.strip(), "observed": observed.strip()}
    return out


def _every_staged_row_landed(resume: object) -> bool:
    """True when a ledger resume carries ``unlanded`` as an empty list: every open plan row was
    landed before the halt (checkpoint or halt commit, landed-writes gate) or credited, so no
    declared path owes a diff. An absent or non-list ``unlanded`` is no evidence."""
    raw = resume.get(RESUME_LEDGER_UNLANDED_FIELD) if isinstance(resume, dict) else None
    return isinstance(raw, list) and not raw


def _withheld_of(worktree_root: Path, resume: object) -> dict:
    """The paths the halt withheld: ``run_artifacts.withheld`` of the resume, else the halted run's
    ``halted.json``; ``{}`` when neither is readable."""
    if not isinstance(resume, dict):
        return {}
    arts = resume.get("run_artifacts")
    withheld = arts.get("withheld") if isinstance(arts, dict) else None
    if isinstance(withheld, dict):
        return withheld
    run_id = resume.get("run_id")
    from coordinator_core.ops._path_guard import safe_id

    if not (isinstance(run_id, str) and safe_id(run_id)):
        return {}
    from coordinator_core.ops.dispatch_emit.halted_run import halted_withheld

    return halted_withheld(worktree_root / RECORD_DIR_ROOT / run_id)


def _worktree_differs_from_head(worktree_root: Path, resume: object) -> bool:
    """True when a path of the run's write set (``run_write_set``, no own session so every live
    claim is foreign) differs from HEAD in the worktree or is untracked. Two git spawns at most."""
    run_id = resume.get("run_id") if isinstance(resume, dict) else None
    from coordinator_core.ops._path_guard import safe_id

    if not (isinstance(run_id, str) and safe_id(run_id)):
        return False
    from coordinator_core.ops.dispatch_emit.run_write_set import run_write_set

    paths = list(run_write_set(worktree_root, run_id, ""))
    if not paths:
        return False
    cwd = str(worktree_root)
    if run_git(["--literal-pathspecs", "diff", "--quiet", "HEAD", "--", *paths], cwd=cwd).returncode == 1:
        return True
    created = run_git(["--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "--", *paths], cwd=cwd)
    return created.returncode == 0 and bool(created.stdout.strip())


def _withheld_work_pending(worktree_root: Path, resume: object, declared: Sequence[str]) -> bool:
    """True when a declared path the halt withheld (``_withheld_of``) is dirty or untracked: the
    halt left that product in the tree with the halt state's own bytes, so ``git diff <halt_sha>``
    reads it as unchanged while it is exactly the work this refire has to land."""
    withheld = _withheld_of(worktree_root, resume)
    pending = [p for p in declared if p in withheld]
    if not pending:
        return False
    status = run_git(
        ["--literal-pathspecs", "status", "--porcelain", "--untracked-files=all", "--", *pending],
        cwd=str(worktree_root),
    )
    return status.returncode == 0 and bool(status.stdout.strip())


def _unchanged_since_halt_failures(
    worktree_root: Path, resume: object, verdict: object, manifest_path: Optional[Path],
    plan_rel: Optional[str] = None, detail: Optional[dict] = None,
) -> list:
    """``[{id, reason}]`` for each blocking row of a ledger resume that ``verdict`` judges met or
    post_commit while no declared product path differs from the diff base. One ``git diff
    --quiet`` against the worktree, run only when such a row exists. The base is the first that
    resolves per ``halt_baseline.since_halt_base``: ``since_sha`` (``param``); the run's first
    ledger-check halt (``resolved``); the 40-hex ``halt_sha`` (``param``); the manifest's
    ``head_sha``, the HEAD the refire's staging stamped (``stage-head``). No base, an empty
    declared set or any git exit other than 0 or 1 refuses the same rows. A resume whose
    ``unlanded`` is an empty list owes no diff and refuses nothing (``_every_staged_row_landed``).
    When ``detail`` is given and the rows are refused it receives ``{base, base_source, outcome}``,
    ``outcome`` one of ``unchanged``, ``no-base``, ``no-declared-paths``, ``git-exit-<n>``.
    On the unchanged refusal, rows whose met_by names a commit at or before the base are recorded
    in ``detail["cited"]`` and, when they cover every refused row, stand as the landing."""
    blocking = resume.get("blocking") if isinstance(resume, dict) else None
    items = verdict.get("items") if isinstance(verdict, dict) else None
    if not isinstance(blocking, list) or not isinstance(items, list):
        return []
    judged = [
        str(i["id"]) for i in items
        if isinstance(i, dict) and i.get("id") in blocking and i.get("status") in ("met", "post_commit")
    ]
    exempt = verified_only_rows(resume)
    credited = pre_halt_credit.credited_rows(resume, verdict)
    discharged = _em_discharged_rows(resume, items) if judged else frozenset()
    post_commit = {
        str(i["id"]) for i in items if isinstance(i, dict) and i.get("status") == "post_commit"
    }
    unverified = credited & post_commit
    judged = [
        i for i in judged
        if i not in exempt and (i not in credited or i in unverified) and i not in discharged
    ]
    if not judged or _every_staged_row_landed(resume):
        return []
    paths, stage_head = _manifest_declared(manifest_path)
    base, source = halt_baseline.since_halt_base(worktree_root, resume, plan_rel, stage_head)
    if unverified and base:
        verified = verified_post_commit_credit(worktree_root, base, items, unverified)
        judged = [i for i in judged if i not in verified]
        if not judged:
            return []
    outcome = "no-base" if base is None else "no-declared-paths" if not paths else "unchanged"
    if outcome == "unchanged":
        result = run_git(
            ["--literal-pathspecs", "diff", "--quiet", base, "--", *paths], cwd=str(worktree_root)
        )
        if result.returncode == 1:
            return []
        if result.returncode == 0:
            # `git diff` cannot see a file the remaining-work leg created and left unstaged.
            created = run_git(
                ["--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "--", *paths],
                cwd=str(worktree_root),
            )
            if created.returncode == 0 and created.stdout.strip():
                return []
            if created.returncode == 0 and _withheld_work_pending(worktree_root, resume, paths):
                return []
            if created.returncode == 0 and (
                (credited & set(judged)) or _withheld_of(worktree_root, resume)
            ) and _worktree_differs_from_head(worktree_root, resume):
                return []
            if created.returncode != 0:
                outcome = f"git-exit-{created.returncode}"
            elif detail is not None:
                owners = [
                    x for x in (plan_rel, resume.get("plan"), resume.get("run_id")) if isinstance(x, str)
                ]
                cited = cited_at_or_before(worktree_root, base, items, judged)
                if cited and all(i in cited for i in judged):
                    shas = set(cited.values())
                    detail[CITED_DETAIL_KEY] = cited
                    detail["landed_sha"] = next(iter(shas)) if len(shas) == 1 else base
                    detail["landed_paths"] = list(paths)
                else:
                    landed = _landed_before_halt(worktree_root, base, paths, owners)
                    if landed:
                        detail["landed_sha"] = landed
                        detail["landed_paths"] = list(paths)
        else:
            outcome = f"git-exit-{result.returncode}"
    if detail is not None:
        detail.update(base=base, base_source=source, outcome=outcome)
    return [{"id": i, "reason": UNCHANGED_SINCE_HALT_REASON} for i in judged]


_VERDICTS_HEADING = "## Verdicts"


def _verdicts_section(ledger_verdict: object, inline_review: dict, sidecar_root: Path) -> str:
    """The delivery note's ``## Verdicts`` section: the ledger checker's per-item status and the
    review, test and criterion results, rendered from the params the commit already holds.
    ``sidecar_root`` (required) anchors relative review sidecar paths, never the process cwd."""
    def cell(value: object) -> str:
        return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")

    lines = [_VERDICTS_HEADING, "", "Written by dispatch.terminal_commit from the run's verdict params.", ""]
    items = ledger_verdict.get("items") if isinstance(ledger_verdict, dict) else None
    if isinstance(items, list) and items:
        lines += ["### Requirements ledger", "", "| item | status | essential | wired | remaining |", "|---|---|---|---|---|"]
        for item in items:
            if not isinstance(item, dict):
                continue
            remaining = item.get("remaining")
            lines.append(
                f"| {cell(item.get('id'))} | {cell(item.get('status'))} | {cell(item.get('essential'))} "
                f"| {cell(item.get('wired'))} | {cell('; '.join(map(str, remaining)) if isinstance(remaining, list) else None)} |"
            )
        lines.append("")
    prep = inline_review.get("prep") if isinstance(inline_review.get("prep"), dict) else {}
    delivery = inline_review.get("delivery") if isinstance(inline_review.get("delivery"), dict) else {}
    lines += [
        "### Review", "",
        f"- delivery verdict: {cell(delivery.get('verdict'))}; unbacked claims: {cell(delivery.get('claims_unbacked'))}",
        f"- product files: {cell(prep.get('product_files'))}; slices reviewed: {cell(inline_review.get('slices'))}; "
        f"fixes applied: {cell(inline_review.get('fixes'))}",
        f"- ledger receipt: {review_floor.receipt(review_floor.verdict_count(inline_review, sidecar_root))}",
        "",
    ]
    tests = inline_review.get("tests") if isinstance(inline_review.get("tests"), dict) else {}
    lines += [
        "### Tests", "",
        f"- status: {cell(tests.get('status'))}; run: {cell(tests.get('run'))}; failed: {cell(tests.get('failed'))}",
        "",
    ]
    criterion = inline_review.get("criterion") if isinstance(inline_review.get("criterion"), dict) else {}
    lines += [
        "### Criterion", "",
        f"- status: {cell(criterion.get('status'))}",
        f"- observation: {cell(criterion.get('observation'))}",
        *([f"- reason: {cell(criterion.get('reason'))}"] if criterion.get("reason") else []),
        "",
    ]
    return "\n".join(lines)


def _write_note_verdicts(
    note_abs: Path, ledger_verdict: object, inline_review: object, sidecar_root: Path,
) -> None:
    """Append ``## Verdicts`` to the docs stage's delivery note, once. Never raises: the note is
    a record, and a failed append must not refuse the product commit."""
    if not isinstance(inline_review, dict):
        return
    try:
        text = note_abs.read_text(encoding="utf-8")
        if _VERDICTS_HEADING in text:
            return
        note_abs.write_text(
            text.rstrip("\n") + "\n\n" + _verdicts_section(ledger_verdict, inline_review, sidecar_root),
            encoding="utf-8", newline="\n",
        )
    except (OSError, ValueError):
        return


def _awaiting_operator(open_rows: Sequence[dict]) -> list:
    """``[{id, row, command}]`` for each open ledger row classified ``awaiting_operator``."""
    return [{"id": r["id"], "row": r["row"], "command": r["command"]} for r in open_rows if "command" in r]


def _ledger_gate(
    worktree_root: Path, run_dir: Path, verdict: object, manifest_path: Optional[Path] = None,
    gated: Optional[Sequence[str]] = None, operator: Optional[dict] = None,
    plan_text: Optional[str] = None, inline_review: Optional[dict] = None,
    resume_ledger: object = None, plan_rel: Optional[str] = None,
    recovery_withheld: Optional[Sequence[str]] = None, code_paths: Sequence[str] = (),
    sidecar_root: Optional[Path] = None,
) -> tuple[Optional[dict], object, list]:
    """(refusal or None, ledger, open) for a ledger-bearing ask run: the refusal when ``verdict``
    does not pass ``requirements_ledger.judge`` against the live register rows, an engine-routed
    item (``manifest_path``'s ``engine_rows``) included when it names another root than the one
    owed. ``gated`` is the manifest's gated chunk ids; a ``not_met`` row whose ``withheld_by`` names
    one passes and comes back in ``open`` (``[{id, reason}]``), as does a non-essential one.
    A run emitted without a ledger is not gated (``(None, None, [])``). The pass is one gate among
    the review stage's; it discharges none of them. ``plan_rel`` is the request's plan, the
    since-halt check's halt-commit lookup when ``resume_ledger`` names none; a since-halt refusal
    carries ``since_halt: {base, base_source, outcome}``. A since-halt refusal closes as already
    landed when every refused row is in ``since_halt["cited"]`` (its met_by names a pre-halt commit)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    recovery_reasons: dict = {}
    if recovery_withheld:
        recovery_reasons, unbacked = _recovery_withheld_reasons(run_dir, list(recovery_withheld))
        if unbacked:
            return _error(
                f"params.recovery_withheld names chunks the run's recovery.json does not record as "
                f"exhausted, blocked or held: {', '.join(unbacked)}. Re-run the ledger recovery or "
                "drop them from the param",
                refused="recovery-withheld-unbacked",
                unbacked=unbacked,
            ), None, []
    try:
        ledger = requirements_ledger.read_ledger(worktree_root, run_dir)
    except requirements_ledger.LedgerRefused as exc:
        return _error(
            f"the run's bound sizing is unreadable: {exc}. Restore the sizing and re-fire",
            refused="ledger-unreadable",
        ), None, []
    if ledger is None:
        return None, None, []
    held_refusal = held_revision_refusal(worktree_root, ledger, run_dir)
    if held_refusal is not None:
        return held_refusal, ledger, []
    gated = [*(gated or ()), *recovery_reasons]
    if not isinstance(verdict, dict):
        return _error(
            "the run is ledger-bearing but params.ledger_verdict is absent: the final ledger "
            "checker's verdict gates this commit. Re-run the run's ledger check and pass its "
            "verdict, or revise with workflow.revise and re-run the check",
            refused="ledger-unjudged",
        ), ledger, []
    result = requirements_ledger.judge(
        ledger, verdict, _routed_from_manifest(manifest_path), gated=gated, operator=operator,
        repo_root=worktree_root, plan_text=plan_text,
        wave_reviews=[w for w in (inline_review or {}).get("wave_reviews") or [] if isinstance(w, dict)],
        noted_paths=requirements_ledger.noted_paths(worktree_root, run_dir),
        code_paths=code_paths,
        review_verdicts=review_floor.verdict_count(inline_review, sidecar_root or worktree_root),
    )
    since_halt: dict = {}
    if resume_ledger is not None:
        resume_ledger = pre_halt_credit.with_notes(resume_ledger, requirements_ledger.read_notes(run_dir))
        from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

        stale = _tc._unchanged_since_halt_failures(
            worktree_root, resume_ledger, verdict, manifest_path, plan_rel, since_halt
        )
        stale = [*stale, *_unclosed_halt_gaps(resume_ledger, verdict, _halt_gap_lines(worktree_root, resume_ledger))]
        if stale:
            result = {**result, "pass": False, "failures": [*result["failures"], *stale]}
    if result["pass"]:
        dirt = _pre_existing_dirt_refusal(
            worktree_root, _readable_manifest(manifest_path), verdict, ledger, plan_rel, run_dir
        )
        if dirt is not None:
            return dirt, ledger, []
        drift = _measured_rev_drift_refusal(run_dir, worktree_root, verdict, ledger)
        if drift is not None:
            return drift, ledger, []
        return None, ledger, [
            {**r, "reason": recovery_reasons.get(str(r["reason"])[len("withheld_by "):], r["reason"])}
            if str(r.get("reason", "")).startswith("withheld_by ") else r
            for r in result.get("open") or []
        ]
    foreign = [
        str(f["id"]) for f in result["failures"] if f["reason"] == requirements_ledger.NOT_A_ROW_REASON
    ]
    message = "the requirements are not met: " + "; ".join(
        (f"{f['id']}: {f['reason']}" if f["id"] is not None else f["reason"]) for f in result["failures"][:10]
    )
    if foreign:
        message += f". Verdict ids that are not register rows: {', '.join(foreign)}"
    landed = _since_halt_close(since_halt, result["failures"], verdict)
    if landed:
        return {
            "committed": True, "sha": landed, "already_landed": True,
            "paths": since_halt["landed_paths"], "since_halt": since_halt,
        }, ledger, []
    return _error(
        message,
        refused="ledger-unmet",
        ledger_failures=result["failures"],
        ledger_items=len(ledger.items),
        all_of_pm_ask=ledger.all_of_pm_ask,
        awaiting_operator=_awaiting_operator(result["open"]),
        **({"since_halt": since_halt} if since_halt else {}),
    ), ledger, []


def _ledger_writeback(worktree_root: Path, ledger, verdict: object, run_id: str, sha: str) -> dict:
    """Write the passed verdict onto the bound sizing's register rows; never raises -- the
    product commit stands. ``{}`` or ``{"requirements_writeback_error": reason}``, plus
    ``post_commit_owed`` (``[{id, acts, sha}]``) when a ``post_commit`` item still owes a sha memo."""
    if ledger is None or not ledger.sizing_rel or not isinstance(verdict, dict):
        return {}
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    out: dict = {}
    try:
        requirements_ledger.apply_verdict(worktree_root, ledger.sizing_rel, verdict, run_id=run_id, sha=sha)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        out["requirements_writeback_error"] = str(exc)
    owed = requirements_ledger.post_commit_owed(verdict, sha)
    if owed:
        out["post_commit_owed"] = owed
    return out


from coordinator_core.ops.dispatch_emit.bug_close import _PARTIAL_REPORT_RE, _STATUS_FIELD  # noqa: E402 -- the one definition both readers share

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
        picked = [
            ln.strip() for ln in lines
            if re.match(r'\s*"?[*_`]{0,2}PARTIAL', ln) or re.match(_STATUS_FIELD + "PARTIAL", ln, re.I)
        ]
    return " | ".join(picked)[:_UNDONE_CAP]


def _partial_chunks(
    worktree_root: Path, request: CommitRequest, incomplete: set, withheld: Optional[dict] = None
) -> list:
    """Incomplete chunks whose executor itself reported DONE/DONE_WITH_CONCERNS: the row
    is incomplete only because the delivery verdict failed (an AC needing a later runtime
    step), and its files were reviewed with the wave. A PARTIAL report undone only by
    ``post-commit: bug_close <id>`` markers (``bug_close.delivered_with_post_commit``) lands
    too: the landing closes those rows. Any other executor PARTIAL or BLOCKED, an
    unreadable report, or one carrying no DONE status stays stranded; a PARTIAL row is
    recorded in ``withheld`` as ``{id: undone AC text}``."""
    out: list = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is None:
            continue
        if (_DELIVERED_REPORT_RE.search(text) and not _UNDELIVERED_REPORT_RE.search(text)) or (
            bug_close.delivered_with_post_commit(text)
        ):
            out.append(chunk)
        elif withheld is not None and _PARTIAL_REPORT_RE.search(text):
            withheld[chunk.id] = _undone_summary(text)
    return out


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


_BLOCKED_REPORT_RE = re.compile(
    r'^\s*"?[*_`]{0,2}BLOCKED[*_`]{0,2}:|<exit-status>BLOCKED</exit-status>'
    rf'|(?i:{_STATUS_FIELD}BLOCKED\b)',
    re.MULTILINE,
)


def _is_delivered_report(text: str) -> bool:
    """True for a DONE/DONE_WITH_CONCERNS report: ``_partial_chunks`` lands that row, not a regrade."""
    return bool(_DELIVERED_REPORT_RE.search(text)) and not _UNDELIVERED_REPORT_RE.search(text)


def _ledger_met_ids(ledger_verdict: object) -> list:
    """Ids of the verdict items whose status is ``met`` or whose only owed acts the landing itself
    performs (``requirements_ledger.close_dischargeable``), in verdict order."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    items = ledger_verdict.get("items") if isinstance(ledger_verdict, dict) else None
    return [
        str(i["id"]) for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("id") is not None
        and (i.get("status") == "met" or requirements_ledger.close_dischargeable(i))
    ]


def _withheld_product_files(
    worktree_root: Path, snapshot: Optional[_Snapshot], declared: set, committed: set,
) -> list:
    """Declared writes still changed in the tree that the commit did not carry, sorted.

    A run whose commit omits a file it changed cannot report its ledger rows met: the claim is
    checked against the tree, not against the verdict. Unknown change state reads as none.
    """
    changed = _changed(snapshot, worktree_root, sorted(declared))
    return sorted(changed - committed) if changed else []


def _lifecycle_owed(
    verdict: dict, sha: str, reply: dict, consumed_baton: Optional[str], close_ctx: Optional[dict],
) -> list:
    """``post_commit_owed`` entries once the close stamp ran: a lifecycle act counts as performed
    when the plan reads implemented, or the consumed baton was archived or shipped inline;
    ``bug_close`` counts when the landing closed its marked rows without a refusal."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    done: set = set()
    if reply.get("plan_status") == "implemented":
        done.add("plan_implemented")
    legs = close_ctx.get("legs") if isinstance(close_ctx, dict) else None
    shipped = legs.get("baton-ship") if isinstance(legs, dict) else None
    if (consumed_baton and consumed_baton in (reply.get("archived") or {})) or (
        getattr(shipped, "status", None) in ("ran", "discharged")
    ):
        done.add("baton_archive")
    if reply.get("bugs_closed") and not reply.get("bug_close_refused"):
        done.add(bug_close.ACT)
    return requirements_ledger.post_commit_owed(verdict, sha, discharged=done)


def _blocking_open_rows(ledger: object, verdict: object, open_rows: object) -> list:
    """The open rows that keep ``_ledger_met_observation`` from stamping: every open row except an
    awaiting-operator row whose verdict item is explicitly ``essential: false``, under a ledger that
    is not ``all_of_pm_ask`` and whose row is not a design-conformance row (always essential)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger

    items = verdict.get("items") if isinstance(verdict, dict) else None
    design = {
        str(i.id) for i in (getattr(ledger, "items", None) or ())
        if requirements_ledger._is_design(getattr(i, "anchor", None))
    }
    optional = {
        str(i.get("id")) for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and i.get("essential") is False and str(i.get("id")) not in design
    } if not getattr(ledger, "all_of_pm_ask", False) else set()
    return [r for r in (open_rows or []) if not (isinstance(r, dict) and "command" in r and str(r.get("id")) in optional)]


def _ledger_met_observation(ledger: object, verdict: object, open_rows: object) -> Optional[str]:
    """``"ledger-met: <ids>"`` when a ledger-bearing run's passed gate left no blocking open row
    (an awaiting-operator row the verdict marks non-essential does not block) and the verdict names
    a ``met`` item; else None. Call only after ``_ledger_gate`` passed."""
    if ledger is None or _blocking_open_rows(ledger, verdict, open_rows):
        return None
    met = _ledger_met_ids(verdict)
    return ("ledger-met: " + ", ".join(met))[:_UNDONE_CAP] if met else None


def _ledger_not_stamped_reason(ledger: object, verdict: object, open_rows: object) -> Optional[str]:
    """Why a passed ledger gate yielded no ``ledger_met`` observation, for the close reply; None when
    the run is not ledger-bearing or an observation exists."""
    if ledger is None or _ledger_met_observation(ledger, verdict, open_rows) is not None:
        return None
    blocking = _blocking_open_rows(ledger, verdict, open_rows)
    if blocking:
        why = "; ".join(f"{r.get('id')}: {r.get('reason')}" for r in blocking)
        return f"exit_criterion_met not stamped: open ledger row(s) not met ({why})"[:_UNDONE_CAP]
    return "exit_criterion_met not stamped: the ledger verdict names no met row"


def _ledger_met_regrades(
    worktree_root: Path, request: CommitRequest, incomplete: set, inline_review: dict,
    report_cache: dict, ledger_verdict: object, held: set,
) -> dict:
    """``{chunk id: "ledger-met: <met item ids>"}`` for incomplete chunks a passed requirements
    ledger plus a clean review cover: the report is readable and not BLOCKED, the chunk is not held,
    and every declared and own-prefix file sits in ``prep.slice_files``. Call only once the ledger
    gate passed. The ledger is run-wide, not mapped to chunks: a PARTIAL report's undone text is not
    weighed."""
    met = _ledger_met_ids(ledger_verdict)
    blocked = set(held)
    files: dict = {}
    candidates = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or not chunk.report:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is not None and _is_delivered_report(text):
            continue
        candidates.append(chunk)
        prefix_files = _own_prefix_files(worktree_root, chunk, report_cache)
        if text is None or _BLOCKED_REPORT_RE.search(text) or prefix_files is None:
            blocked.add(chunk.id)
            continue
        files[chunk.id] = set(chunk.paths) | set(prefix_files)
    ids = credit_gate.ledger_regradable(
        [c.id for c in candidates], blocked, files, _clean_review_files(inline_review)
    )
    reason = ("ledger-met: " + ", ".join(met))[:_UNDONE_CAP]
    return {cid: reason for cid in ids}


def _credit_regrades(
    worktree_root: Path, artifacts, record_dir_rel: Optional[str], request: CommitRequest,
    incomplete: set, held: set, run_dir: Optional[Path] = None, sizing_rel: Optional[str] = None,
) -> dict:
    """``{chunk id: "credited: <evidence>"}`` for incomplete chunks the EM credited through
    ``workflow.revise credit_chunks`` (this run's record dir, its superseded predecessors', and any
    earlier run of ``sizing_rel``, committed or not; this run's own entries win). A credit overrides a
    BLOCKED report; a hold still bars it."""
    from coordinator_core.ops.dispatch_emit import run_credit

    dirs = list(artifacts.record_dirs) if artifacts is not None else []
    if record_dir_rel and record_dir_rel not in dirs:
        dirs.insert(0, record_dir_rel)
    paths = [worktree_root / d for d in reversed(dirs)]
    if run_dir is not None:
        paths.append(run_dir)
    credits = run_credit.read(paths)
    if sizing_rel:
        credits = {**run_credit.credits_for_sizing(worktree_root, sizing_rel), **credits}
    if not credits:
        return {}
    known = []
    for chunk in request.chunks:
        if chunk.id not in incomplete or chunk.id not in credits:
            continue
        text = _read_rel(worktree_root, chunk.report) if chunk.report else None
        if text is None or not _is_delivered_report(text):
            known.append(chunk.id)
    got = credit_gate.credit_regradable(known, credits, held)
    return {cid: f"credited: {evidence}"[:_UNDONE_CAP] for cid, evidence in got.items()}


def _is_closeout_path(rel: str, evidence_rel: Optional[str]) -> bool:
    """True for a plan evidence sidecar, an evidence index or a run process-state path
    (baton, sizing, run record): what a closeout row writes in place of product."""
    return rel == evidence_rel or rel.endswith(".evidence.yaml") or _is_process_state(rel)


def _xs_closeout_regrades(
    worktree_root: Path, request: CommitRequest, incomplete: set, snapshot: Optional[_Snapshot],
    report_cache: dict,
) -> dict:
    """``{chunk id: evidence}`` for incomplete chunks of an xs-arm run whose report is DONE and whose
    changed declared paths are all evidence/state paths: the closeout's own writes are its credited
    chunk. A chunk with no changed declared path, or any product path, never qualifies."""
    candidates = [c for c in request.chunks if c.id in incomplete and c.report and c.paths]
    if not candidates:
        return {}
    plan_pp = PurePosixPath(request.plan_path) if request.plan_path else None
    evidence_rel = str(plan_pp.with_name(plan_pp.stem + _EVIDENCE_SIDECAR_SUFFIX)) if plan_pp else None
    out: dict = {}
    for chunk in candidates:
        text = _read_rel(worktree_root, chunk.report)
        if text is None or not _is_delivered_report(text):
            continue
        prefix = _own_prefix_files(worktree_root, chunk, report_cache)
        if prefix is None:
            continue
        declared = sorted(set(chunk.paths) | set(prefix))
        changed = _changed(snapshot, worktree_root, declared)
        if changed and all(_is_closeout_path(p, evidence_rel) for p in changed):
            out[chunk.id] = "credited: xs closeout evidence/state writes"[:_UNDONE_CAP]
    return out


def _own_evidence_append_chunks(
    worktree_root: Path, request: CommitRequest, stranded_ids: set, carried: set,
    changed: Optional[set],
) -> set:
    """Stranded chunk ids whose own deliverable is the plan evidence append this commit carries.

    A chunk qualifies when its report is DONE, none of its declared paths differs from HEAD (the
    work its footprint forbids is the append itself) and the plan or its evidence sidecar is changed
    and in the commit. Unanswerable change state (``changed`` None) credits nothing."""
    if not request.plan_path or changed is None:
        return set()
    plan_pp = PurePosixPath(request.plan_path)
    evidence_rel = str(plan_pp.with_name(plan_pp.stem + _EVIDENCE_SIDECAR_SUFFIX))
    if not ({request.plan_path, evidence_rel} & carried & changed):
        return set()
    out: set = set()
    for chunk in request.chunks:
        if chunk.id not in stranded_ids or not chunk.report or set(chunk.paths) & changed:
            continue
        text = _read_rel(worktree_root, chunk.report)
        if text is not None and _is_delivered_report(text):
            out.add(chunk.id)
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


def _commit_residue(
    worktree_root: Path, request: CommitRequest, snapshot: Optional[_Snapshot],
    report_cache: dict, session_id: Optional[str], *, excluded: set,
    unfinished: set, commit_v2_mod,
) -> dict:
    """``{path: reason}`` for every path a chunk declares (or claims by prefix) that the
    pre-commit read saw changed and the commit did not carry; ``excluded`` holds the paths
    the commit carried or legitimately dropped. A path the snapshot does not cover costs
    one scoped status spawn. The reason is the unfinished
    chunk ids declaring it, ``peer-held``, or ``unexplained``."""
    owners: dict = {}
    for chunk in request.chunks:
        for p in list(chunk.paths) + (_own_prefix_files(worktree_root, chunk, report_cache) or []):
            owners.setdefault(p, []).append(chunk.id)
    candidates = [p for p in owners if p not in excluded]
    if not candidates:
        return {}
    dirty = _changed(snapshot, worktree_root, candidates)
    if not dirty:
        return {}
    peer: list = []
    commit_v2_mod._peer_claim_warnings(worktree_root, sorted(dirty), peer, own_sid=session_id)
    peer_paths = {p for p, _sid in peer}
    out: dict = {}
    for p in sorted(dirty):
        ids = sorted(set(owners[p]) & unfinished)
        out[p] = (", ".join(ids) if ids else "peer-held" if p in peer_paths else "unexplained")
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
    A stranded path a committable chunk also declares lands with that chunk, so it blocks no importer.
    """
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
    done_declared = {p for c in request.chunks if c.id not in stranded_ids for p in c.paths}
    specs = [p for p in declared if p.endswith(".py")] + [root for root, _ in prefix_roots]
    changed = _changed(snapshot, worktree_root, specs)
    blocked_files: dict = {}
    if changed is None:
        blocked_files = {p: c for p, c in declared.items() if p.endswith(".py")}
    else:
        for path in changed:
            if not path.endswith(".py"):
                continue
            if path in done_declared:
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

    from coordinator_core.authoring_leaks.import_closure import _absolute_base, _needed_paths
    from coordinator_core.pyimports import collect_imports

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
            for rec in records:
                rec_needed = _needed_paths([(rel, rec)])
                base = _absolute_base(rec, rel)
                if base and "." in base:
                    # `top/__init__.py` is only the gate's first-party probe here; the import binds it
                    # only when it names the top package itself.
                    rec_needed.discard(f"{base.split('.')[0]}/__init__.py")
                needed |= rec_needed
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
