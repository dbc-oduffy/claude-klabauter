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
  - Does NOT touch a `pulled` plan's STATUS. `pulled` means the EM left it there
    deliberately; advancing or reverting it would overwrite that judgment. It DOES
    repair a missing baton→plan link for a `pulled` verdict, same as `ready` —
    linking is not approving, so the plan's status is untouched either way, and an
    unlinked `pulled` plan is refused from adoption identically to an unlinked
    `ready` one when the baton already names a different plan.
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
from typing import Any, Dict, List, Optional, Set, Tuple

from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.artifact_id_slug import id_slug
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.roadmap.plan_gate import (
    BATON_CODED_STATES,
    PLAN_APPROVED_STATUSES,
    assemble_plan_gate,
)

#: Kinds `handoff_phase` is legal on (mirrors `schema_validate.py`'s own
#: `_HANDOFF_PHASE_KINDS`, H-CROSS-EXEC-2). Sourced the same way that module
#: sources it — through `kind_values_for_canonical('roadmap-baton')`, never a
#: bare `kind == 'roadmap-baton'` literal, for the identical reason: that
#: canonical resolves to `{roadmap-baton, spinoff-roadmap}`, and a literal
#: gate would silently never admit a real roadmap baton written under the
#: retired spelling. `kind: spinoff` is deliberately absent — see
#: `authorize_execution`'s own docstring for why a spinoff baton refuses
#: this stamp instead of receiving a narrowed one.
_EXECUTION_PHASE_KINDS = frozenset({"session-handoff"}) | frozenset(
    kind_values_for_canonical("roadmap-baton")
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


def _link_baton_to_plan(
    worktree_root: Path,
    baton_path: str,
    plan_path: str,
    report: Dict[str, Any],
) -> bool:
    """Verify/repair the baton→plan link. Returns True iff a repair was written.

    Shared by every landing path that must guarantee a link without ever touching
    a plan's `status` — `approve_ready` (link, then stamp `approved`) and the
    `pulled` handler in `land_wave` (link only, status untouched) both call this
    rather than each carrying its own linker, so the refusal when a baton already
    names a different plan is one rule, not two that could drift apart.

    Writes `governing_plan` onto the BATON rather than a `deliverable_id` onto the
    plan. Both would link, but `governing_plan` is the stronger basis
    (`plan_gate._PLAN_LINK_ORDER` tries it first): it is stamped against this
    specific baton and cannot be a coincidence of two records citing a third, and it
    leaves the plan's own frontmatter untouched, which matters when the plan is one
    a human authored months earlier and this landing is only adopting it.
    """
    baton_abs = worktree_root / baton_path
    plan_abs = worktree_root / plan_path
    baton = next((b for b in report["batons"] if b["path"] == baton_path), None)
    linked = bool(baton and baton.get("plan") and baton["plan"]["path"] == plan_path)
    if linked:
        return False

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
    return True


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
    """
    baton_abs = worktree_root / baton_path
    plan_abs = worktree_root / plan_path
    if not plan_abs.is_file():
        raise LandingRefused(f"plan does not exist on disk: {plan_path}")
    if not baton_abs.is_file():
        raise LandingRefused(f"baton does not exist on disk: {baton_path}")

    repaired = _link_baton_to_plan(worktree_root, baton_path, plan_path, report)

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

    One terminal state is still closable: `shipped` with no `shipped_in`. An
    executor that hand-stamps `shipped` during the Dispatch phase leaves exactly
    that, and the shipped-handoff sweep fails closed on it — the record can never
    archive, and this landing is the one caller holding the SHA that would repair it.
    So it stamps the SHA and leaves `deployment_state` alone. Every other terminal
    state, and a `shipped` baton that already cites a commit, stays refused: a
    re-landing must never overwrite a citation it did not write.

    `shipped_in_kind: ship-commit` is written in lockstep with every `shipped_in`
    (DR-096; `handoff.schema.json` requires it once a `shipped` baton carries a
    `shipped_in`). A confirm-and-close's prior SHA is a ship commit too — it names
    where the work landed.
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
        cited = _read_field(old, "shipped_in") not in (None, "null", "~")
        if state in BATON_CODED_STATES and (state != "shipped" or cited):
            raise MutateAbort(f"baton is already terminal (deployment_state: {state})")
        text = _set_field(old, "deployment_state", "shipped")
        text = _set_field(text, "shipped_in", shipped_in)
        return _set_field(text, "shipped_in_kind", "ship-commit")

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


def _verified_prior_sha(
    worktree_root: Path, prior: Any
) -> Tuple[Optional[str], Optional[str]]:
    """`(sha, None)` for a usable per-baton prior SHA, `(None, reason)` otherwise.

    A confirm-and-close XS verifies work that shipped long before this wave, in
    a commit the executor names. Stamping the landing's own `shipped_in` there
    cites the closure commit and loses the one SHA that says where the work is.
    The prior SHA comes from an agent, so it is honoured only as a full 40-hex
    COMMIT present in this repo's object store (read in process, no spawn); an
    abbreviated or absent one falls back to the landing's SHA and is named.
    `(None, None)` means none was reported.
    """
    if prior in (None, ""):
        return None, None
    if not isinstance(prior, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", prior):
        return None, f"not a full 40-hex SHA: {prior!r}"
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import _read_object

    try:
        found = _read_object(resolve_git_common_dir(worktree_root), prior)
    except Exception as exc:  # noqa: BLE001 - any read failure is "cannot verify"
        return None, f"object store unreadable: {type(exc).__name__}: {exc}"
    if found is None or found[0] != "commit":
        return None, f"no commit {prior} in this repo"
    return prior.lower(), None


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

    **`handoff_phase` is legal only on `kind: session-handoff` or a canonical
    `roadmap-baton` (H-CROSS-EXEC-2, `schema_validate.py::_cf_handoff_phase_kind_gate`).**
    A spinoff baton (the S lane's usual carrier — example-store-repo-em, landing
    fc61535f) does not carry either kind, so stamping `handoff_phase: execution`
    onto one writes a shape `pickup-assemble apply` refuses on claim: the write
    here would "succeed" and the baton would die at pickup instead. This function
    refuses the stamp instead — MutateAbort, same as the already-stamped case
    below, surfaced through the ordinary `execution_ready: False` / `note` result
    rather than a silent re-kind. The S lane is not expressible on a spinoff
    baton today; widening `_EXECUTION_PHASE_KINDS` (and its schema-side twin) to
    admit `spinoff` is a schema-owning decision, not this landing step's to make.
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
        kind = _read_field(old, "kind")
        if kind not in _EXECUTION_PHASE_KINDS:
            raise MutateAbort(
                f"cannot stamp handoff_phase: execution — kind is {kind!r}, "
                "which handoff_phase requires to be session-handoff or "
                "roadmap-baton (H-CROSS-EXEC-2); the S lane cannot be "
                "expressed on this baton's kind"
            )
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
    stem = id_slug(re.sub(r"[^a-z0-9]+", "-", title.lower()), 60)
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


#: Where an archived baton can be. Two shapes, because the archive move is done by
#: whoever finishes the work and the repos do not agree: claude-klabauter's own convention is
#: a dated `archive/handoffs/<YYYY-MM>/`, while example-game-workbench-repo archives in
#: place under `state/handoffs/archive/`. Only the first was searched, so a
#: example-game-repo XS baton whose own remit WAS the archive move came back from the landing
#: as "no baton on disk carries id ..." — a record-missing refusal for a record
#: sitting one directory away, at the end of a wave that had done everything right
#: (measured 2026-09-11, blitz-2026-09-11 wave 0, `hnd-example-game-repo-control-mcp-proce…`).
#: Both are walked lazily and only when an id is otherwise unresolved, so the added
#: directory costs nothing on the normal path.
_ARCHIVE_SUBDIRS = (("archive", "handoffs"), ("state", "handoffs", "archive"))


def _archived_records_this_wave_names(
    worktree_root: Path,
    wave_result: Dict[str, Any],
    live_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Archived baton records for the ids this wave names and the live scan missed.

    The XS lane's own correct close-out ARCHIVES its baton: an executor that
    finishes an XS remit `git mv`s the record into ``archive/handoffs/<YYYY-MM>/``,
    which is exactly where the live scan does not look. So a well-behaved dispatch
    produced a landing refusal — "no baton on disk carries id ..." — reading as a
    missing record when the record is right there and terminal. Measured 2026-09-10,
    wave 0 of run 20260910T000000Z: two of eight dispatched batons archived
    themselves and both refused at the landing, in a report whose other entries
    landed fine, so the refusal looked like data loss rather than success.

    Lazy for the reason ``plan_gate.scan_batons`` is: claude-klabauter's archive holds ~3x
    the live tree and parsing all of it costs more than the rest of the landing,
    for an answer that is normally "nothing was missing". Nothing is read unless
    this wave names an id the live scan could not resolve, and then only the
    archive is walked, once.

    Resolving is all this does. It stamps nothing: ``close_dispatched`` already
    reads an archived record's terminal ``deployment_state`` and reports
    ``stamped: False`` with the reason, which is the honest answer for work that
    closed itself — and is a ``closed`` entry rather than a refusal.
    """
    from coordinator_core.roadmap.plan_gate import (
        _baton_record,
        _iter_record_paths,
        _read_baton_fields,
    )

    known: Set[str] = set()
    for record in live_records:
        known.update(record["ids"])
        known.add(record["path"])

    wanted = {
        ident
        for key in _VERDICT_KEYS
        for entry in (wave_result.get(key) or [])
        if isinstance(entry, dict)
        for ident in (entry.get("batonId"),)
        if ident and ident not in known
    }
    if not wanted:
        return []

    found: List[Dict[str, Any]] = []
    for subdir in _ARCHIVE_SUBDIRS:
        for path in _iter_record_paths(worktree_root, subdir, recursive=True):
            fm = _read_baton_fields(path)
            if not fm:
                continue
            rel = path.relative_to(worktree_root).as_posix()
            record = _baton_record(rel, path, fm, live=False)
            if wanted & (set(record["ids"]) | {rel}):
                found.append(record)
    return found


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

    It carries ONE subtraction from that read: a baton whose replan this landing just
    minted is dropped, because the mint leaves the source open and the fresh read
    hands back both halves of the pair. `_without_replanned_sources` states the rule
    and its bounds — the subtraction is on the report only, nothing is written to the
    source, and succession is deliberately left unsettled.
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
    all_records = all_records + _archived_records_this_wave_names(
        worktree_root, wave_result, all_records
    )
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
            # A `ready` verdict on a PM-only route is the blitz-em resolving a PM
            # decision — the one thing the skill forbids outright. Refuse it HERE, by
            # name: tolerating it instead would reach `approve_ready` with `plan_path
            # None` and raise TypeError out of the whole op, after earlier `ready`
            # entries had already written to disk, so the caller gets a stack trace and
            # no landing report rather than one named per-baton refusal.
            route = entry.get("route")
            if route in _PM_ONLY_ROUTES:
                raise LandingRefused(
                    f"route: {route} is not a landable `ready` verdict — the exit is "
                    "the PM's, and this lane would be the blitz-em taking it. It "
                    "belongs in surfacedToPm."
                )
            # A route without a plan resolves no planPath — resolving it first would
            # refuse those lanes on a field it is correct for them not to have.
            plan_path = (
                None
                if _carries_no_plan(route)
                else _rel(
                    entry.get("planPath") or _plan_path_from_trail(entry, wave_result),
                    worktree_root,
                )
            )
            # The S lane parks its spec onto the baton and marks it execution-ready,
            # so `/execute-plan` resolves it as a straight dispatch instead of
            # handing back an un-actioned baton. Everything else takes the ordinary
            # approval, which is what opens the NEXT wave's planning gates.
            if entry.get("route") == "dispatch":
                # XS: the Dispatch phase already did the work. What is owed is the
                # terminal stamp, not an approval — there is no plan to approve.
                prior, rejected = _verified_prior_sha(worktree_root, entry.get("priorShippedIn"))
                if rejected and not shipped_in:
                    raise LandingRefused(
                        f"priorShippedIn rejected ({rejected}) and the landing carries no "
                        "shipped_in to fall back to — land again with shipped_in=<sha>"
                    )
                row = close_dispatched(worktree_root, baton_path, prior or shipped_in or "")
                if rejected:
                    row["prior_shipped_in_rejected"] = rejected
                closed.append(row)
            elif entry.get("route") == "spec-dispatch":
                row = authorize_execution(
                    worktree_root,
                    baton_path,
                    plan_path,
                    authorized_by=authorized_by,
                    note=(
                        f"plan-blitz wave {wave_result.get('waveIndex')} readiness gate: "
                        f"S-lane spec parked, execution-ready"
                    ),
                )
                if row.get("execution_ready"):
                    execution_ready.append(row)
                else:
                    refused.append(
                        {
                            "baton": entry.get("batonId"),
                            "reason": row.get("note")
                            or "authorize_execution declined to stamp",
                        }
                    )
            else:
                approved.append(
                    approve_ready(worktree_root, baton_path, plan_path, report)
                )
        except (LandingRefused, MutateAbort, OSError) as exc:
            refused.append({"baton": entry.get("batonId"), "reason": str(exc)})

    pulled: List[Any] = []
    for entry in wave_result.get("pulled") or []:
        baton_id = entry.get("batonId")
        try:
            baton_path = _baton_path_for(entry, report)
            # Same route carve-out as the `ready` lane: a `pulled` entry on a route
            # that carries no plan has none to link, and that is a no-op here, never
            # a refusal.
            #
            # TWO routes carry no plan, not one. `dispatch` (XS) is the obvious one.
            # `pm-decision` is the other and is the one that bit: an XL exit is not a
            # plannable route — the blitz-em scaffolds no sizing object and names no
            # reviewers for it by construction — so `planPath` is absent BY DESIGN and
            # refusing on its absence refuses the pipeline for behaving correctly.
            # Measured 2026-09-10 on example-cockpit-repo: one XL pulled to `pm-decision`
            # refused its whole landing with "verdict carries no planPath", which is a
            # STOP condition, so a run whose every other lane was clean halted on the
            # one baton it had handled exactly right.
            plan_path = (
                None
                if _carries_no_plan(entry.get("route"))
                else _rel(
                    entry.get("planPath") or _plan_path_from_trail(entry, wave_result),
                    worktree_root,
                )
            )
            if plan_path is not None:
                plan_abs = worktree_root / plan_path
                baton_abs = worktree_root / baton_path
                if not plan_abs.is_file():
                    raise LandingRefused(f"plan does not exist on disk: {plan_path}")
                if not baton_abs.is_file():
                    raise LandingRefused(f"baton does not exist on disk: {baton_path}")
                # Link only — never stamp. `pulled` is the EM leaving the plan's
                # status exactly where it was; the missing edge is what made the
                # next gate read it as never-planned, not the status.
                _link_baton_to_plan(worktree_root, baton_path, plan_path, report)
            pulled.append(baton_id)
        except (LandingRefused, MutateAbort, OSError) as exc:
            refused.append({"baton": baton_id, "reason": str(exc)})

    minted: List[Dict[str, Any]] = []
    #: Repo-relative paths of the SOURCE batons whose replan was minted in THIS landing.
    #: Read by the `next_wave` build below — see `_without_replanned_sources`.
    replanned_sources: set[str] = set()
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
            source_path = _baton_path_for(entry, report)
            # The subtraction below is earned by a mint that SUCCEEDED, so it is recorded after
            # one. Adding it here would drop a source out of `next_wave` on a refused mint — a
            # baton left open, `ready_to_fire`, carrying no approved plan and now no replan
            # either, omitted from the fire that was its way back. Every other refusal path in
            # this module leaves the record where it was so the next sweep self-heals; this one
            # would not.
            entry_minted = mint_replan_baton(
                worktree_root,
                source_baton_path=source_path,
                brief=entry.get("replanBrief") or entry.get("reason") or "",
                handoff_id=f"hnd-{id_slug(slug, 40)}-{suffix}",
                deliverable_id=deliverable_id,
                title=f"Replan — {entry['batonId']}",
                branch=branch,
                summary="Replan minted by a plan-blitz readiness gate; brief carries the reviewers' rationale.",
            )
            minted.append(entry_minted)
            replanned_sources.add(source_path)
        except (LandingRefused, OSError, KeyError) as exc:
            refused.append({"baton": entry.get("batonId"), "reason": str(exc)})

    after = assemble_plan_gate(worktree_root)
    wave = after["waves"][0] if after["waves"] else []
    by_id = {b["id"]: b for b in after["batons"]}
    wave = _without_replanned_sources(wave, by_id, replanned_sources)

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
        "pulled": pulled,
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

#: The ONLY routes that produce a plan document — mirrors `PLANNABLE_ROUTES` in
#: DoE-claude `coordinator/workflows/plan-blitz.mjs`. Every other route is a different
#: room and carries no `planPath` by construction: `dispatch` (the wave's Dispatch phase
#: already did the work), and the `ROUTE_EXITS` set — `pm-decision`, `shape`, `roadmap`,
#: `goal-setting` — none of which gets a sizing object or a planner.
#:
#: An ALLOWLIST, deliberately. The plan-free set is open and grows whenever an exit is
#: added; the plannable set is closed and stable, so enumerating the other side is the
#: list that goes stale silently. An UNKNOWN or absent route stays on the refusing side:
#: a legacy wave result carrying no `route` must still get the baton->plan link repair
#: the `pulled` lane exists for, rather than being silently skipped.
_PLANNABLE_ROUTES = frozenset({"plan", "spec-dispatch"})

#: Routes whose exit belongs to the PM. A `ready` verdict on one of these is the
#: blitz-em resolving a PM decision, which the skill forbids in as many words ("The
#: blitz-em is an EM proxy, never a PM proxy") — so the `ready` lane REFUSES them
#: loudly rather than tolerating a missing plan. The `pulled` lane accepts them: leaving
#: such a baton where it is, is exactly the right disposition.
_PM_ONLY_ROUTES = frozenset({"pm-decision", "shape", "roadmap", "goal-setting"})


def _carries_no_plan(route: Optional[str]) -> bool:
    """True when this route produces no plan document, so `planPath` is absent by design."""
    return route is not None and route not in _PLANNABLE_ROUTES


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
        _refuse_incomplete_wave(payload)
        return payload
    inner = payload.get("result")
    if isinstance(inner, dict) and any(key in inner for key in _VERDICT_KEYS):
        # Both halves are read: the workflow writes the reason onto its own return
        # value, the harness records the agent failures onto the envelope around it,
        # and a caller passing the envelope whole must not lose the outer half.
        _refuse_incomplete_wave(inner, payload)
        return inner
    raise LandingRefused(
        "wave_result carries none of "
        + ", ".join(_VERDICT_KEYS)
        + " — this is not a plan-blitz wave result. Pass the workflow's own return "
        "value, or the task-output file's `result` field."
    )


#: Fields by which a wave result DECLARES it did not finish. Read on the wave result
#: and on the task-output envelope alike, because the harness records the agent
#: failures and the workflow records the reason, and either one alone is enough.
_INCOMPLETE_COUNT_KEYS = ("agentErrors", "erroredAgents", "failedAgents")


def _incompleteness(payload: Dict[str, Any]) -> Optional[str]:
    """Why this payload says the wave did not finish, or None when it claims nothing.

    Absence is admitted, not refused: a payload predating these fields carries none
    of them, and a check that failed closed on absence would refuse every wave result
    already on disk without a single unfinished wave among them. Only a POSITIVE
    declaration refuses.
    """
    reason = payload.get("incompleteReason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if payload.get("completed") is False:
        return "the wave result declares completed: false and names no reason"
    for key in _INCOMPLETE_COUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return f"{key}: {value}"
        if isinstance(value, (list, tuple)) and value:
            return f"{key}: {len(value)} agent(s) errored"
    return None


def _refuse_incomplete_wave(*payloads: Dict[str, Any]) -> None:
    """Refuse a landing whose own payload says agents errored mid-wave.

    A session limit or an agent error mid-wave does not merely lose work — it
    MANUFACTURES verdicts, and they are plausible ones. Measured on
    example-market-data-repo 2026-09-11: each of wave 0's three fires had to complete
    three times before its agent set was clean, and verdicts MOVED between runs in
    the direction that matters. One baton came back `pulled` with a long, well-argued
    reason whose substance was that the integration pass had not run, then `ready`
    once it did; another's `converged` row read `skipped: no integration report to
    resolve over` and became `settled: 2, unaddressed: 0`.

    So an unfinished fire hands the driver an absence of judgment wearing the shape of
    a judgment, at exactly the moment the cheap read — lane counts — says the wave is
    done. Landing it writes that absence to disk as a verdict. The skill teaches this
    (tripwire AN-UNFINISHED-WAVE-IS-NOT-A-WAVE-THAT-OPENED-NOTHING) and the teaching
    held on the day it was measured; what it costs is driver diligence spent on every
    landing forever, which is what a mechanical check is for.

    Refused rather than downgraded to a per-baton refusal: the corruption is not
    localised to the lanes that errored. An agent that died is one whose absence
    changed a DIFFERENT baton's verdict, and nothing in the payload says which.
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        reason = _incompleteness(payload)
        if reason:
            raise LandingRefused(
                f"wave result reports an unfinished wave ({reason}) — refusing to land. "
                "An unfinished fire manufactures verdicts rather than losing them: a "
                "baton whose integration pass never ran returns `pulled` with a reason "
                "that reads as judgment. Re-run the fire until its agent set is clean, "
                "then land that result."
            )


def _plan_path_from_trail(entry: Dict[str, Any], wave_result: Dict[str, Any]) -> Optional[str]:
    """The plan this baton's own planning report names, or None.

    A wave whose readiness gate returns verdict rows WITHOUT `planPath` is
    unlandable in every lane at once — measured 2026-09-11 on claude-klabauter fire 0-1:
    four verdicts, 31 agents and 2.6M tokens, all refused on the missing field,
    with the plans themselves authored and reviewed on disk. The pass that wrote
    each plan also wrote `<trailSlotDir>/<batonId>.planning-report.md` carrying
    `plan:` in its frontmatter, and the result carries `trailSlotDir`, so the
    answer the verdict dropped is recoverable from the wave's own trail rather
    than guessed at. A trail that names no plan either still refuses.
    """
    slot = wave_result.get("trailSlotDir") or wave_result.get("trailDir")
    baton_id = entry.get("batonId")
    if not slot or not baton_id:
        return None
    report = Path(str(slot).replace("\\", "/")) / f"{baton_id}.planning-report.md"
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^plan:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return None
    named = match.group(1).strip().strip("\"'")
    return named or None


def _rel(path: Optional[str], worktree_root: Path) -> str:
    if not path:
        raise LandingRefused(
            "verdict carries no planPath, and no planning report in the wave's trail "
            "names one for this baton"
        )
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


def _without_replanned_sources(
    wave: List[str],
    by_id: Dict[str, Dict[str, Any]],
    replanned_sources: set,
) -> List[str]:
    """Drop, from the wave this landing hands back, any baton whose replan it just minted.

    A `replan` verdict mints a NEW baton carrying the gate's brief and leaves the source
    exactly as it was — open, `ready_to_fire`, carrying no approved plan. The fresh gate read
    below therefore returns BOTH, and the driver's next fire spends a sizing scout, a planner,
    a premise check, reviewers and an integrator on the source as well as on its replan.
    Measured 2026-09-10 on example-retrieval-repo-ue-addon: the landing of one PIVOT returned `rqsi-02`
    and `hnd-replan-rqsi-02-d15940` side by side, and the wave being landed had itself planned
    both halves of two OLDER pairs (`inst-02`, `dlv-20260901-handoff-2a3d9e`) — in each case
    the replan went ready and the source was pulled, after paying for it in full.

    THE FILTER IS ON THE REPORT, NOT ON DISK, and that is the whole design. This module writes
    no frontmatter it would then have to un-write: no `blocked_by` edge (which is
    decision-dependency vocabulary, not workflow state, and which nothing here would ever
    clear), no terminal stamp on the source (`continued` is a CODED state, so stamping it
    would open a dependent's EXECUTION gate on work that never landed). The source keeps its
    disk state, stays visible to the very next `roadmap.plan_gate` read, and is simply not
    handed to the fire the driver is about to make.

    So this stops the double-spend and does NOT settle succession. A replan is a successor in
    everything but its frontmatter, and the durable answer is to mint it as one (DR-172's
    `continued`/`continued_into` shape, with identity inherited so a dependent edge does not
    resolve `coded` against unlanded work). That is a work-graph semantics change and wants a
    decision record, not this patch. Until it exists, a pair already on disk from an EARLIER
    landing is outside this filter's reach by construction — it only knows what it just minted.
    """
    if not replanned_sources:
        return wave
    return [i for i in wave if (by_id.get(i) or {}).get("path") not in replanned_sources]


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
