"""
coordinator_core.roadmap.blitz_land — land a plan-blitz wave result without an EM.

Purpose: a plan-blitz wave runs unattended from fire to verdict — but until this
module existed, everything on either side of it was hand-work: stamp each `ready`
plan `approved`, repair the plan→baton link when the planner did not write one,
mint a baton for each `replan`, then re-read the gate and build the next wave's
arguments. Every one of those is derivable from the wave result and the gate; none
needs judgment. Leaving them to the operator made the WAVE autonomous and the LOOP
manual, which is the half that matters: the loop is what turns a batch tool into a
pipeline.

The one step that must not be left to a human is the link repair. A plan stamped
`approved` that no FK ties to its baton is a silent no-op — the baton reads
`needs_plan: true` forever, every later sweep re-plans it, and its approval opens
the planning gate of nothing. Measured twice on DoE-claude: once on a plan this
pipeline authored, and once on a 457-line plan authored months earlier by hand
(`pcli-06`), which the gate reported as unplanned for exactly this reason. Both
symptoms read as "not planned yet", which is indistinguishable from the honest
state of untouched work. `approve_ready` therefore verifies the link and repairs it
BEFORE the stamp, and REFUSES the stamp when it cannot — an unlinked approval is
worse than a missing one, because it looks done.

Domain vocabulary: wave result, landing, ready/pulled/replan verdict, link repair,
replan baton.

Consumed by ``coordinator_core.ops.roadmap_blitz_land`` (the ``roadmap.blitz_land``
op). Pure-ish: reads the corpus through ``plan_gate``, writes only the records the
wave result names, each under ``locked_rmw``.

Spec backlink: DoE-claude coordinator/skills/plan-blitz/SKILL.md § The flow, step 4.

Negative-spec:
  - Does NOT decide anything. `ready`/`pulled`/`replan` are the EM's verdicts,
    already made at the readiness gate; this module executes them. A verdict it
    does not recognise is refused, never guessed at.
  - Does NOT stamp a plan it cannot link. See above — refusal is the whole point.
  - Does NOT touch a `pulled` plan. `pulled` means the EM left it at its current
    status deliberately; advancing or reverting it would overwrite that judgment.
  - Does NOT re-queue `surfacedToPm`. Those need a PM answer first, and a loop
    that silently retried them would be answering on the PM's behalf.
  - Does NOT commit. Landing writes records; committing them is the caller's act,
    so a caller can inspect the diff before it becomes history.
  - Does NOT fire the next wave. It reports what the next wave WOULD be
    (`next_wave`); firing is the driver's move, which keeps a runaway loop one
    explicit call away rather than implicit in a landing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.roadmap.plan_gate import (
    BATON_CODED_STATES,
    PLAN_APPROVED_STATUSES,
    assemble_plan_gate,
    link_plans,
)

#: The status a `ready` verdict advances a plan to. Deliberately a constant rather
#: than a parameter: this is the single seam the whole two-gate design keys on
#: (`PLAN_APPROVED_STATUSES`'s lower bound), and a caller-chosen status would let a
#: landing quietly write `implemented` — which asserts the work landed and would
#: open dependents' EXECUTION gates on a plan nobody has executed.
APPROVED_STATUS = "approved"

#: Review verdicts that mean "this direction cannot proceed" rather than "this plan
#: is wrong until you fix it". A `ready` verdict carrying one of these is refused: the
#: plan-blitz workflow already reconciles a pivoted plan to `replan` before it gets
#: here, and this is the SECOND, independent check — the first lives in a `.mjs` an
#: agent edits, which is the one that goes missing.
#:
#: `REJECTED` is in the set for a fail-closed reason, not because it is a synonym. It
#: is the fleet-wide reviewer enum's strongest severity and plan-blitz resolves it by
#: evidence upstream, so an unresolved one arriving here is a workflow that did not
#: resolve it. Refusing costs one EM look; stamping `approved` on a plan a reviewer
#: pivoted opens every dependent baton's PLANNING gate on a premise someone rejected.
PIVOT_VERDICTS = frozenset({"PIVOT", "REJECTED"})

_FM_BOUNDS = re.compile(r"\A(?:\s*<!--.*?-->\s*)*---\s*\n(?P<fm>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_STATUS_LINE = re.compile(r"^status:.*$", re.MULTILINE)


class LandingRefused(Exception):
    """A landing step refused, loudly, rather than writing something misleading."""


# ---------------------------------------------------------------------------
# Frontmatter editing — narrow, and only on records the wave result names
# ---------------------------------------------------------------------------


def _frontmatter_span(text: str):
    """`(start, end)` of the frontmatter BODY, or None when there is no block.

    Spans are returned rather than a parsed mapping because every edit below is a
    surgical line replacement or insertion: re-emitting a parsed record would
    reformat fields this module never looked at, and a landing has no business
    rewriting bytes it was not asked to change.
    """
    match = _FM_BOUNDS.match(text)
    if not match:
        return None
    return match.start("fm"), match.end("fm")


def _set_field(text: str, field: str, value: str) -> str:
    """Set `field` in the frontmatter, replacing it in place or appending it.

    Raises LandingRefused when there is no frontmatter block — a record without
    one is not a record this module should be inventing structure for.
    """
    span = _frontmatter_span(text)
    if span is None:
        raise LandingRefused("record has no frontmatter block")
    start, end = span
    head, body, tail = text[:start], text[start:end], text[end:]

    line_re = re.compile(rf"^{re.escape(field)}:.*$", re.MULTILINE)
    if line_re.search(body):
        body = line_re.sub(f"{field}: {value}", body, count=1)
    else:
        body = body.rstrip("\n") + f"\n{field}: {value}"
    return head + body + tail


def _read_field(text: str, field: str) -> Optional[str]:
    span = _frontmatter_span(text)
    if span is None:
        return None
    body = text[span[0] : span[1]]
    match = re.search(rf"^{re.escape(field)}:\s*(.*)$", body, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value or None


def pivoting_reviewers(entry: Dict[str, Any]) -> List[str]:
    """Name the reviewers whose verdict pivots this entry, or an empty list.

    Reads `reviewVerdicts` — the per-reviewer verdict list the wave result carries
    alongside each EM verdict. A wave result predating that field carries none, and
    this returns empty: the guard admits what it cannot see rather than refusing every
    older payload, because a check that fails closed on ABSENCE would refuse the whole
    corpus of existing wave results without any pivot being present in them.
    """
    out: List[str] = []
    for review in entry.get("reviewVerdicts") or []:
        if not isinstance(review, dict):
            continue
        if str(review.get("verdict") or "").strip().upper() in PIVOT_VERDICTS:
            out.append(str(review.get("reviewer") or "(unnamed reviewer)"))
    return out


# ---------------------------------------------------------------------------
# The three verdict handlers
# ---------------------------------------------------------------------------


def approve_ready(
    worktree_root: Path,
    baton_path: str,
    plan_path: str,
    report: Dict[str, Any],
) -> Dict[str, Any]:
    """Stamp one `ready` plan `approved`, repairing the baton link first if needed.

    Order is load-bearing and is the whole reason this function exists: LINK, then
    verify, then stamp. Stamping first and repairing after leaves a window in which
    an interrupted landing has written an approval that resolves to nothing — the
    exact silent state this module was built to make impossible.

    The repair writes `governing_plan` onto the BATON rather than a `deliverable_id`
    onto the plan. Both would link, but `governing_plan` is the stronger basis
    (`plan_gate._PLAN_LINK_ORDER` tries it first): it is stamped against this
    specific baton and cannot be a coincidence of two records citing a third, and it
    leaves the plan's own frontmatter untouched, which matters when the plan is one
    a human authored months earlier and this landing is only adopting it.
    """
    baton_abs = worktree_root / baton_path
    plan_abs = worktree_root / plan_path
    if not plan_abs.is_file():
        raise LandingRefused(f"plan does not exist on disk: {plan_path}")
    if not baton_abs.is_file():
        raise LandingRefused(f"baton does not exist on disk: {baton_path}")

    baton = next((b for b in report["batons"] if b["path"] == baton_path), None)
    linked = bool(baton and baton.get("plan") and baton["plan"]["path"] == plan_path)

    repaired = False
    if not linked:
        rel = plan_abs.relative_to(worktree_root).as_posix()

        def _link(old: str) -> str:
            existing = _read_field(old, "governing_plan")
            if existing and existing not in ("null", "~", rel):
                raise MutateAbort(
                    f"baton already names a different governing_plan ({existing}); "
                    f"refusing to repoint it at {rel} — a landing adopts an unlinked "
                    f"plan, it never re-owns a linked one"
                )
            return _set_field(old, "governing_plan", rel)

        locked_rmw(baton_abs, _link, repo_root=worktree_root)
        repaired = True

    def _stamp(old: str) -> str:
        current = (_read_field(old, "status") or "").lower()
        if current in PLAN_APPROVED_STATUSES:
            # Idempotent: a re-landed wave must not walk a plan backwards from
            # `implemented` to `approved`, which would re-open a gate that closed.
            raise MutateAbort(f"already at status: {current}")
        return _set_field(old, "status", APPROVED_STATUS)

    stamped = True
    note = None
    try:
        locked_rmw(plan_abs, _stamp, repo_root=worktree_root)
    except MutateAbort as exc:
        stamped = False
        note = str(exc)

    return {
        "baton": baton_path,
        "plan": plan_path,
        "link_repaired": repaired,
        "stamped": stamped,
        "note": note,
    }


def close_dispatched(
    worktree_root: Path,
    baton_path: str,
    shipped_in: str,
) -> Dict[str, Any]:
    """Close an XS baton whose work the wave's Dispatch phase already did.

    The third lane's landing. XS routes to `dispatch` and has no plan, so there is
    nothing to approve — what closes it is the baton itself reaching a terminal
    `deployment_state` with a resolvable `shipped_in`. Until that stamp lands the
    work is done and every surface that counts open batons still reads it as
    unstarted, which is the same silent-success shape as an unlinked approval,
    reached from the other direction.

    `shipped_in` is REQUIRED and is the caller's to supply, because this module
    does not commit: the executor's changes have to be in history before a SHA can
    name them, and a stamp citing a commit that does not exist yet is a citation to
    nothing. A caller that has not committed gets a refusal telling it to.
    """
    if not shipped_in or not re.fullmatch(r"[0-9a-fA-F]{7,64}", shipped_in):
        raise LandingRefused(
            "closing a dispatched baton needs a resolvable `shipped_in` SHA — commit "
            "the wave's work first, then land with shipped_in=<sha>. This module does "
            "not commit, so a stamp written before the commit would cite nothing."
        )
    baton_abs = worktree_root / baton_path
    if not baton_abs.is_file():
        raise LandingRefused(f"baton does not exist on disk: {baton_path}")

    def _close(old: str) -> str:
        state = _read_field(old, "deployment_state")
        if state in BATON_CODED_STATES:
            raise MutateAbort(f"baton is already terminal (deployment_state: {state})")
        text = _set_field(old, "deployment_state", "shipped")
        text = _set_field(text, "shipped_in", shipped_in)
        return text

    stamped = True
    detail = None
    try:
        locked_rmw(baton_abs, _close, repo_root=worktree_root)
    except MutateAbort as exc:
        stamped = False
        detail = str(exc)

    return {
        "baton": baton_path,
        "closed": stamped,
        "shipped_in": shipped_in,
        "note": detail,
    }


def authorize_execution(
    worktree_root: Path,
    baton_path: str,
    plan_path: str,
    authorized_by: str,
    note: str,
) -> Dict[str, Any]:
    """Park an S-lane plan onto its baton and stamp it execution-ready.

    The S lane (`route: spec-dispatch`) produces a light four-part spec, not a
    decision-weight plan. Sending it round a full review/integrate cycle and then
    handing back an un-actioned baton is what made the EM's own sizing fight the
    rest of the skill: if calling something S condemned it to the queue, the honest
    S got inflated to M. Stamping it execution-ready instead means `/execute-plan`
    resolves it as a straight dispatch — mise-en-place tier — which is what an S
    was always supposed to mean.

    **This writes a MARK, never work.** The blitz-em decides `ready`; this function
    performs the stamp. That separation is the point: an EM that could stamp its own
    records could also edit them, and "park the spec" would quietly become "do the
    work". The EM returns a verdict and touches nothing.

    The stamp is the four-field byte-identical contract H-CROSS-EXEC-1 requires when
    `handoff_phase: execution` — by/at/sha/note, all present and non-empty, or the
    record fails validation. `sha` is the git blob hash of the PLAN BODY, computed
    in-process (no spawn): it is the content-binding witness `/pickup` recomputes,
    so a plan edited after authorization no longer matches its own stamp and the
    authorization is visibly stale rather than silently carried.

    `note` is attributed to the WAVE, never phrased as a PM utterance. The field
    exists to be self-attesting about who named execution, and a session writing a
    sentence that reads like the PM's is the one way this stamp could lie.
    """
    baton_abs = worktree_root / baton_path
    plan_abs = worktree_root / plan_path
    if not plan_abs.is_file():
        raise LandingRefused(f"plan does not exist on disk: {plan_path}")
    if not baton_abs.is_file():
        raise LandingRefused(f"baton does not exist on disk: {baton_path}")

    rel_plan = plan_abs.relative_to(worktree_root).as_posix()
    body_sha = _git_blob_sha(plan_abs.read_bytes())
    stamped_at = _now_iso()

    def _stamp(old: str) -> str:
        existing = _read_field(old, "handoff_phase")
        if existing == "execution":
            raise MutateAbort("baton is already stamped handoff_phase: execution")
        text = _set_field(old, "governing_plan", rel_plan)
        text = _set_field(text, "handoff_phase", "execution")
        text = _set_field(text, "execution_authorized_by", authorized_by)
        text = _set_field(text, "execution_authorized_at", stamped_at)
        text = _set_field(text, "execution_authorized_sha", body_sha)
        # Single-quoted with YAML's own doubling escape: the note is prose and the
        # one field guaranteed to contain apostrophes. Double-quoting would need
        # backslash escaping that `_scan_fields` would then have to mirror exactly.
        text = _set_field(
            text, "execution_authorized_note", "'" + note.replace("'", "''") + "'"
        )
        return text

    stamped = True
    detail = None
    try:
        locked_rmw(baton_abs, _stamp, repo_root=worktree_root)
    except MutateAbort as exc:
        stamped = False
        detail = str(exc)

    return {
        "baton": baton_path,
        "plan": rel_plan,
        "execution_ready": stamped,
        "authorized_sha": body_sha,
        "note": detail,
    }


def _git_blob_sha(data: bytes) -> str:
    """`git hash-object` of `data`, computed in-process.

    Spawning git here would cost ~25ms of process creation to reproduce four lines
    of hashing, on a box where the load norm is fifty peers. The format is stable
    and specified: `blob <len>\0<data>`, sha1.
    """
    import hashlib

    header = f"blob {len(data)}\0".encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()


def _now_iso() -> str:
    """Authorization timestamp. Isolated for the same reason as `_today`."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint_replan_baton(
    worktree_root: Path,
    source_baton_path: str,
    brief: str,
    handoff_id: str,
    deliverable_id: str,
    title: str,
    branch: str,
    summary: str,
) -> Dict[str, Any]:
    """Write one replan baton carrying the gate's brief verbatim.

    The brief is written UNEDITED. It was authored by the readiness gate for a
    session that will not have this context, and a landing that summarised it would
    be compressing the one artifact whose whole purpose is to survive the context
    boundary.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    if not stem.startswith("replan-"):
        stem = f"replan-{stem}"
    out = worktree_root / "state" / "handoffs" / f"{_today()}-{stem}.md"
    if out.exists():
        raise LandingRefused(f"replan baton already exists: {out.name}")

    body = (
        "---\n"
        f'title: "{title}"\n'
        f"created: {_today()}\n"
        f'branch: "{branch}"\n'
        "predecessor: none\n"
        "status: open\n"
        "kind: spinoff\n"
        "deployment_state: ready_to_fire\n"
        "category: infra\n"
        f'summary: "{summary}"\n'
        "pickup_ready: true\n"
        f'deliverable_id: "{deliverable_id}"\n'
        f'handoff_id: "{handoff_id}"\n'
        "initiative: null\n"
        f'forked_from: "{source_baton_path}"\n'
        "---\n\n"
        f"# {title}\n\n"
        "Minted by a plan-blitz readiness gate. The brief below is the gate's own, verbatim.\n\n"
        "---\n\n"
        f"{brief}\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return {"path": out.relative_to(worktree_root).as_posix(), "handoff_id": handoff_id}


def _today() -> str:
    """Today's date, read from the clock the caller's records are dated against.

    Isolated in one function so a test can monkeypatch it, and so the import stays
    visible: this is the module's only non-deterministic input.
    """
    from datetime import date

    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def land_wave(
    worktree_root: Path,
    wave_result: Dict[str, Any],
    branch: str = "main",
    limit: int = 8,
    authorized_by: str = "plan-blitz",
    shipped_in: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a wave result's verdicts and report what the next wave would be.

    Returns `{approved, refused, minted, pulled, surfaced_to_pm, next_wave}`.
    Every list is per-item and names its subject, because a landing that reports
    only counts cannot be reconciled against the trail it landed.

    `next_wave` is computed from a FRESH gate read taken after the writes, not from
    the pre-landing report — the approvals just written are precisely what changes
    it, and reusing the stale read would hand the driver the same wave twice.
    """
    wave_result = _unwrap_wave_result(wave_result)
    report = assemble_plan_gate(worktree_root)
    # Path resolution walks EVERY record, not just the candidates the report
    # carries: closing a baton makes it terminal and therefore a non-candidate, so
    # resolving against the report alone made an idempotent re-land fail with "no
    # baton carries this id" — which reads as a missing record rather than as work
    # already done.
    from coordinator_core.roadmap.plan_gate import scan_batons

    _, all_records = scan_batons(worktree_root, include_archived=False)
    report = dict(report, _all_records=all_records)

    approved: List[Dict[str, Any]] = []
    closed: List[Dict[str, Any]] = []
    execution_ready: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for entry in wave_result.get("ready") or []:
        try:
            pivoted = pivoting_reviewers(entry)
            if pivoted:
                # Refused, not downgraded to a replan: minting a baton here would act
                # on a brief nobody wrote, since an EM that returned `ready` wrote none.
                # The baton keeps no approved plan, so the next sweep replans it — the
                # self-healing path — and the refusal names why in the landing report.
                raise LandingRefused(
                    f"ready verdict contradicted by review: "
                    f"{', '.join(pivoted)} returned a pivot; refusing to stamp "
                    f"{APPROVED_STATUS}"
                )
            baton_path = _baton_path_for(entry, report)
            # An XS carries no plan, so planPath is resolved only for the lanes
            # that have one — resolving it first would refuse the dispatch lane on
            # a field it is correct for that lane not to have.
            plan_path = (
                None
                if entry.get("route") == "dispatch"
                else _rel(entry.get("planPath"), worktree_root)
            )
            # The S lane parks its spec onto the baton and marks it execution-ready,
            # so `/execute-plan` resolves it as a straight dispatch instead of
            # handing back an un-actioned baton. Everything else takes the ordinary
            # approval, which is what opens the NEXT wave's planning gates.
            if entry.get("route") == "dispatch":
                # XS: the Dispatch phase already did the work. What is owed is the
                # terminal stamp, not an approval — there is no plan to approve.
                closed.append(
                    close_dispatched(worktree_root, baton_path, shipped_in or "")
                )
            elif entry.get("route") == "spec-dispatch":
                execution_ready.append(
                    authorize_execution(
                        worktree_root,
                        baton_path,
                        plan_path,
                        authorized_by=authorized_by,
                        note=(
                            f"plan-blitz wave {wave_result.get('waveIndex')} readiness gate: "
                            f"S-lane spec parked, execution-ready"
                        ),
                    )
                )
            else:
                approved.append(
                    approve_ready(worktree_root, baton_path, plan_path, report)
                )
        except (LandingRefused, MutateAbort, OSError) as exc:
            refused.append({"baton": entry.get("batonId"), "reason": str(exc)})

    minted: List[Dict[str, Any]] = []
    for entry in wave_result.get("replan") or []:
        try:
            from coordinator_core.ops.mint_deliverable_id import mint

            slug = f"replan-{entry['batonId']}"
            deliverable_id, _ = mint(slug=slug)
            # handoff.schema.json pins ^hnd-<slug>-[0-9a-f]{6}$. Reuse the suffix the
            # minter just generated rather than inventing a second one: the two ids
            # then share a visible provenance, and there is only one random draw to
            # reason about.
            suffix = deliverable_id.rsplit("-", 1)[-1]
            minted.append(
                mint_replan_baton(
                    worktree_root,
                    source_baton_path=_baton_path_for(entry, report),
                    brief=entry.get("replanBrief") or entry.get("reason") or "",
                    handoff_id=f"hnd-{slug[:40]}-{suffix}",
                    deliverable_id=deliverable_id,
                    title=f"Replan — {entry['batonId']}",
                    branch=branch,
                    summary="Replan minted by a plan-blitz readiness gate; brief carries the reviewers' rationale.",
                )
            )
        except (LandingRefused, OSError, KeyError) as exc:
            refused.append({"baton": entry.get("batonId"), "reason": str(exc)})

    after = assemble_plan_gate(worktree_root)
    wave = after["waves"][0] if after["waves"] else []
    by_id = {b["id"]: b for b in after["batons"]}

    return {
        "approved": approved,
        # XS batons the wave executed and this landing stamped terminal. Separate
        # from `approved` because they mean the opposite thing downstream: an
        # approval opens a dependent's PLANNING gate, a terminal stamp opens its
        # EXECUTION gate too.
        "closed": closed,
        # S-lane batons carrying a parked spec and a four-field execution stamp.
        # Reported separately from `approved` because they mean a different thing
        # downstream: an approval opens a dependent's PLANNING gate, an execution
        # stamp hands this baton to /execute-plan as a straight dispatch.
        "execution_ready": execution_ready,
        "refused": refused,
        "minted": minted,
        "pulled": [e.get("batonId") for e in wave_result.get("pulled") or []],
        "surfaced_to_pm": [e.get("batonId") for e in wave_result.get("surfacedToPm") or []],
        "next_wave": {
            "waveIndex": (wave_result.get("waveIndex") or 0) + 1,
            "batons": [_fire_arg(by_id[i], worktree_root) for i in wave[:limit] if i in by_id],
            "remaining": max(0, len(wave) - limit),
            "counts": after["counts"],
        },
    }


#: The verdict keys a real wave result carries. A payload with none of them is not
#: an empty wave — it is the wrong object.
_VERDICT_KEYS = ("ready", "pulled", "replan", "surfacedToPm", "dispatched", "routedElsewhere")


def _unwrap_wave_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Accept a wave result, or the task-output envelope that wraps one. Refuse anything else.

    The harness writes a completed workflow's return value to a file as
    ``{"summary": ..., "agentCount": ..., "result": {<the wave result>}, ...}``, and
    that file is the artifact its own notification points a caller at. Handing it
    over whole is the natural mistake, not an exotic one — I made it on the first
    live landing.

    Unwrapped it is harmless. Unrefused it is not: the envelope carries none of the
    verdict keys, so every loop below iterates nothing and the landing returns
    ``approved: [], minted: [], refused: []`` — a clean, complete-looking result for
    work that was never done. That is the silent-success shape this whole module
    exists to refuse elsewhere, and it would be indistinguishable from a wave where
    the EM genuinely pulled everything.
    """
    if not isinstance(payload, dict):
        raise LandingRefused("wave_result must be an object")
    if any(key in payload for key in _VERDICT_KEYS):
        return payload
    inner = payload.get("result")
    if isinstance(inner, dict) and any(key in inner for key in _VERDICT_KEYS):
        return inner
    raise LandingRefused(
        "wave_result carries none of "
        + ", ".join(_VERDICT_KEYS)
        + " — this is not a plan-blitz wave result. Pass the workflow's own return "
        "value, or the task-output file's `result` field."
    )


def _rel(path: Optional[str], worktree_root: Path) -> str:
    if not path:
        raise LandingRefused("verdict carries no planPath")
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(worktree_root).as_posix()
        except ValueError:
            raise LandingRefused(f"planPath is outside the worktree: {path}")
    return candidate.as_posix()


def _baton_path_for(entry: Dict[str, Any], report: Dict[str, Any]) -> str:
    ident = entry.get("batonId")
    for baton in report["batons"]:
        if ident in (baton["id"], baton["stub_id"], baton["path"]):
            return baton["path"]
    for record in report.get("_all_records") or []:
        if ident in record["ids"] or ident == record["path"]:
            return record["path"]
    raise LandingRefused(f"no baton on disk carries id {ident!r}")


def _fire_arg(baton: Dict[str, Any], worktree_root: Path) -> Dict[str, Any]:
    """One `batons[]` entry, shaped exactly as the workflow's args contract wants.

    Emitted here rather than assembled by the caller so the fire arguments and the
    gate that produced them cannot drift — a hand-built array is where `planPath`
    silently becomes null for a baton that already has a plan.
    """
    return {
        "id": baton["id"],
        "path": (worktree_root / baton["path"]).as_posix(),
        "title": baton["title"],
        "sized": baton["sized"],
        "planPath": (
            (worktree_root / baton["plan"]["path"]).as_posix() if baton.get("plan") else None
        ),
        # The EXECUTION gate, carried so the wave can finish XS work in-band. It is
        # deliberately a separate field from anything planning-related: an XS whose
        # blockers are planned-but-not-coded may be PLANNED here and must not be
        # EXECUTED here, and a wave that read one gate for both questions would run
        # code against blockers that do not exist yet.
        "executionOpen": baton["execution_gate"]["open"],
    }
