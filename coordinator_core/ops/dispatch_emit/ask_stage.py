"""dispatch.ask_stage: a plan or an XS sizing -> brief files, manifest.json and commit request.

Working files land under `RUN_DIR_ROOT/<run_id>/`: `briefs/<row>.md` (the plan route's row prompt) and
`commit-request.txt` (the `render_marker` line, empty when no row declares a path). Run records land
under `ask_contract.record_dir`: the minted spine, `pm-intent.md` and `manifest.json` (a `StageManifest`). In-process file writes only; no git, no subprocess, so the
op holds the 500ms bar. Trap: unlike `compose_script`, the per-row pathspec is not
gitignore-filtered, because that filter spawns git.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.git_state import head_branch
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.testing.suite_run_record import read_head_sha
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import design_route
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_TEXT_FILE,
    DELIVERY_NOTE_DIR,
    PM_INTENT_FILE,
    RELAYED_ASK_FILE,
    RELAYED_ASK_HEADING,
    RUN_DIR_ROOT,
    GatedRow,
    ManifestRow,
    OwedRow,
    StageManifest,
    record_dir,
    rulable_ids,
)
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    check_cross_repo_writes,
    gated_rows,
    own_gate_owners,
)
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.credit_gate import DOCS_CHUNK_ID, SIZING_CHUNK_ID
from coordinator_core.ops.dispatch_emit.fanout_cost import FanoutCost, Unit, propose_groups
from coordinator_core.ops.dispatch_emit.engine_owned_rows import engine_owner_rows
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import (
    REVIEWER_AGENT_TYPE,
    review_inputs,
    review_specs,
)
from coordinator_core.ops.dispatch_emit.emit import (
    resolve_agent_model,
    _EXECUTOR_AGENT_TYPE,
    _dispatch_report_path,
    _plan_deliverable_id,
    _dedupe_preserve_order,
    _model_opt,
    _plan_id,
    _row_agent_type,
    _row_prompt,
    _widen_with_test_candidates,
    check_unschedulable_rows,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.pathspec import (
    NoTestTargetError,
    NoWritesDeclaredError,
    commit_pathspec_or_none,
    terminal_test_scope,
)
from coordinator_core.ops.requirement_register import pin_drift_reason, plan_pin_drift
from coordinator_core.ops.review_findings_ledger import LedgerError, targets_add
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.dispatch_emit import requirements_ledger, resume_snapshot, sibling_split
from coordinator_core.ops.dispatch_emit.self_dr_discharge import discharge_clauses
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS, SizingFireRefused, load_sizing, resolve_arm
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine
from coordinator_core.ops.dispatch_emit.spine_read import (
    EM_OWNED_REASONS,
    InvalidChangeKindError,
    read_spine,
)
from coordinator_core.ops.dispatch_emit.wave_map import build_waves, cap_waves, dag_from_waves
from coordinator_core.ops.plan_tasks_render import load_rows

# Writes per-run staging files (manifest, commit-request) under a run dir, not a stamped artifact.
GENERATES = []

_PARAMS = (
    Field("run_id", "str", required=True),
    Field("plan_path", "str"),
    Field("sizing_path", "str"),
    Field("commit_sizing_path", "str"),
    Field("writes", "str_list"),
    Field("gated", "list"),
    Field("cross_repo_approved", "bool"),
    Field("session_id", "str"),
    Field("only_rows", "str_list"),
)

__all__ = ["AskStageError", "stage", "scaffold_markers", "pm_intent_block", "relayed_ask_block", "manifest_payload"]

PM_INTENT_HEADING = "## PM intent (verbatim)"


_HARNESS_RELAY_MARKERS = (
    "[Workflow harness",
    "was relayed from the driving session",
    "<system-reminder>",
)


def _is_harness_relay(text: str) -> bool:
    """True when `text` is a harness-composed resume relay, not the PM's words."""
    head = text.lstrip()
    return any(head.startswith(m) for m in _HARNESS_RELAY_MARKERS) or _HARNESS_RELAY_MARKERS[1] in head[:400]


def pm_intent_block(sizing: Optional[Mapping], ask_text: Optional[str]) -> str:
    """The brief's head: the PM's own words (sizing `pm_verbatims`, else a PM-verbatim `intent`,
    plus the ask as submitted), the exit criterion, and the precedence rule. Never the EM's
    paraphrase: an `em-elaborated` intent is left out. Empty when no PM words are on record."""
    sizing = sizing or {}
    quotes = requirements_ledger.pm_words(sizing)
    if ask_text and ask_text.strip() and not _is_harness_relay(ask_text) and ask_text.strip() not in quotes:
        quotes.append(ask_text.strip())
    if not quotes:
        return ""
    lines = [PM_INTENT_HEADING, ""]
    for q in quotes:
        lines.extend("> " + ln if ln else ">" for ln in q.splitlines())
        lines.append("")
    criterion = sizing.get("exit_criterion")
    statement = criterion.get("statement") if isinstance(criterion, Mapping) else None
    if isinstance(statement, str) and statement.strip():
        lines.extend([f"Exit criterion: {statement.strip()}", ""])
    lines.append(
        "These words are the outcome you are held to; design the change that makes each of them "
        "literally true. Where the brief below, a plan, a memo or in-repo doctrine reads narrower "
        "than these words or against them, these words win: keep the ask and name the conflict in "
        "your report."
    )
    return "\n".join(lines) + "\n"


def relayed_ask_block(relayed_text: Optional[str]) -> str:
    """The brief's relayed-ask section, below the PM-intent block and never inside it: an EM's
    relay of the ask, stated as not the PM's words. Empty when nothing was relayed."""
    if not relayed_text or not relayed_text.strip():
        return ""
    lines = [RELAYED_ASK_HEADING, ""]
    for ln in relayed_text.strip().splitlines():
        lines.append("> " + ln if ln else ">")
    lines.extend([
        "",
        "An agent relayed this ask; it is not the PM's own wording. Serve it, and where the PM's "
        "verbatim words above (or in the sizing) read differently, the PM's words outrank it.",
    ])
    return "\n".join(lines) + "\n"


def _run_sizing_rel(sizing_rel: Optional[str], plan_text: str) -> Optional[str]:
    if sizing_rel:
        return sizing_rel
    split = split_frontmatter(plan_text)
    return read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None


def _run_sizing(root: Path, rel: Optional[str]) -> Optional[dict]:
    if not rel:
        return None
    try:
        return load_sizing(root, rel)
    except (SizingFireRefused, OSError, ValueError):
        return None


_DOC_TARGET_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|#.*)$")


def doc_writes(run_id: str, sizing: Optional[Mapping]) -> tuple[str, ...]:
    """The docs stage's declared write set: the run's delivery note, then every path the sizing
    names in `doc_consumers` and `ask_conflicts` (a `:<line>` or `#<anchor>` suffix stripped).
    A sizing or plan path is never one -- those stay process records."""
    sizing = sizing or {}
    raw = [p for p in sizing.get("doc_consumers") or [] if isinstance(p, str)]
    raw += [
        c.get("path") for c in sizing.get("ask_conflicts") or []
        if isinstance(c, Mapping) and isinstance(c.get("path"), str)
    ]
    from coordinator_core.session.record_homes import home_dir

    process = (PurePosixPath(home_dir(".", "sizings")).as_posix() + "/", "docs/plans/")
    out = [f"{DELIVERY_NOTE_DIR}/{run_id}.md"]
    for entry in raw:
        rel = PurePosixPath(_DOC_TARGET_SUFFIX.sub("", entry.strip()).replace("\\", "/")).as_posix()
        if (
            rel and rel != "." and not PurePosixPath(rel).is_absolute() and ".." not in PurePosixPath(rel).parts
            and not rel.startswith(process) and rel not in out
        ):
            out.append(rel)
    return tuple(out)


def _test_scope(waves, root: Path) -> tuple[tuple[str, ...], Optional[dict]]:
    """(test targets, None), or ((), the typed no-target record) when the locator maps no
    testable write to a runnable target -- e.g. a .ts-only spine in a repo whose tests it cannot
    see. Never a crash and never a silent pass: the run reads its tests as degraded."""
    try:
        return tuple(terminal_test_scope(waves, repo_root=root)), None
    except NoTestTargetError as exc:
        return (), {
            "reason": str(exc),
            "unmapped_paths": list(exc.unmapped_paths),
            "testable_omissions": list(exc.testable_omissions),
        }
    except NoWritesDeclaredError as exc:
        return (), {"reason": str(exc), "unmapped_paths": [], "testable_omissions": []}

# Mirrors DoE's coordinator/lib/plan_scaffold_markers.py; claude-klabauter never imports DoE's tree.
_SCAFFOLD_MARKERS = (
    ("PLACEHOLDER", re.compile(r"\bPLACEHOLDER\b")),
    ("<REPLACE:", re.compile(r"<REPLACE:")),
    ("path/to/file", re.compile(r"path/to/file")),
)


def scaffold_markers(text: str) -> dict[str, int]:
    """Marker -> first 1-based line carrying it; `#` YAML comment lines are template docs, skipped."""
    found: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            continue
        for name, rx in _SCAFFOLD_MARKERS:
            if name not in found and rx.search(line):
                found[name] = n
    return found


class AskStageError(ValueError):
    """The stage inputs name no stageable plan or sizing, or escape the run dir."""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _set_aside_prior_report(root: Path, report_rel: str) -> Optional[str]:
    """Rename a row report left by an earlier run to `<row id>.prior.md` beside it, replacing an
    older one, and return that repo-relative path; None when no report sits there. Rename only.
    Trap: without this a refired executor finds its own report and replays it."""
    report = root / report_rel
    if not report.is_file():
        return None
    prior = report.with_name(report.stem + ".prior.md")
    report.replace(prior)
    return _rel(root, prior)


def _prior_report_line(prior_rel: str) -> str:
    return (
        f"Prior run's report, set aside: {prior_rel}. It is the previous run's history, never this "
        "run's answer; this row re-executes against the current requirements."
    )


def _discharge_clauses_for(plan_text: str, root: Path, raw_rows: list) -> dict[str, str]:
    """Self-DR discharge clauses keyed by row id; {} without a readable `sizing_object`."""
    split = split_frontmatter(plan_text)
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    if not cited:
        return {}
    try:
        sizing = load_sizing(root, cited)
    except SizingFireRefused:
        return {}
    return discharge_clauses(raw_rows, sizing)


def _reviewer_brief(root: Path, run_dir: Path, row_id: str, raw: Mapping) -> str:
    """The blinded reviewer row's brief: exactly the falsifier-integrity phase's spec prompt for
    the plan the row's `falsifier_review_of` names, and nothing else. Writes the can-report-red
    report the prompt cites under the run dir first. Trap: no intent block, plan context or plan
    text may be added -- any of them un-blinds the reviewer. Raises AskStageError, failing the whole
    stage, when the target's verdict is already cached."""
    target = raw.get("falsifier_review_of")
    if not isinstance(target, str) or not target.strip():
        raise AskStageError(f"reviewer row {row_id}: names no falsifier_review_of")
    if raw.get("writes") != []:
        raise AskStageError(f"reviewer row {row_id}: a reviewer is read-only, its writes must be []")
    reviewed = contained_path(root / target.strip(), [root])
    if reviewed is None or not reviewed.is_file():
        raise AskStageError(f"reviewer row {row_id}: falsifier_review_of {target!r} is not a plan under the repo")
    report_dir = Path(_rel(root, run_dir)) / "can-report-red"
    inputs = review_inputs([_rel(root, reviewed)], repo_root=root, report_dir=report_dir)
    if not inputs:
        raise AskStageError(f"reviewer row {row_id}: {target!r} carries no falsifier block")
    item = inputs[0]
    if item.cached is not None:
        raise AskStageError(f"reviewer row {row_id}: a cached verdict needs no reviewer dispatch")
    if item.report_path is not None:
        report = contained_path(root / item.report_path, [root])
        if report is None:
            raise AskStageError(f"reviewer row {row_id}: can-report-red path escapes the repo root")
        _write(report, item.report_json or "")
    return review_specs(inputs)[0].prompt


def _with_owner_roots(gated: Sequence[GatedRow]) -> list[GatedRow]:
    """Fill each gated row's ``owner_root`` from the machine-local registry, one lookup per
    distinct ``owner_repo``; ``""`` when unregistered. No process is spawned."""
    gated = list(gated)
    owners = {g.owner_repo for g in gated if g.owner_repo}
    if not owners:
        return gated
    from coordinator_core.machine_resolver import registry_get
    from coordinator_core.ops.fleet._memo_resolver import receiver_em_to_repo_key

    roots = {o: registry_get(receiver_em_to_repo_key(o)) or "" for o in owners}
    return [replace(g, owner_root=roots.get(g.owner_repo, "")) for g in gated]


def _engine_rows(
    root: Path,
    plan_path: Path,
    raw_by_id: Mapping[str, Mapping],
    gated: Sequence[GatedRow],
    run_sizing: Optional[Mapping],
) -> tuple[dict, ...]:
    """Rows delivered in the engine plane, over the plan's ungated raw rows, the run sizing's
    register rows and the plan's evidence sidecar. One text scan and at most one resolver call."""
    from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path

    withheld = {g.id for g in gated}
    plan_rows = [r for rid, r in raw_by_id.items() if rid not in withheld]
    register = ((run_sizing or {}).get("requirement_register") or {}).get("rows") or []
    sidecar = evidence_sidecar_path(plan_path)
    try:
        evidence = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else ""
    except OSError:
        evidence = ""
    return tuple(engine_owner_rows(plan_rows, [r for r in register if isinstance(r, Mapping)], evidence, root))


def _only_rows(
    rows: list, exclusions: list, keep: frozenset, not_to_run: Optional[frozenset] = None
) -> tuple[list, list]:
    """The resume subset of a spine read: `keep` rows only, edges onto the dropped (landed) rows
    stripped, and only `keep` rows among the owed/gated exclusions. With `not_to_run` (the gate's
    snapshot), every known row outside it is kept, rows a re-plan added included."""
    known = {r.id for r in rows} | {e.get("id") for e in exclusions}
    unknown = keep - known
    if unknown:
        raise AskStageError(f"resume row id(s) not in the plan's spine: {sorted(unknown)}")
    if not_to_run is not None:
        keep = frozenset(known - not_to_run)
    landed = known - keep
    kept = []
    for row in rows:
        if row.id not in keep:
            continue
        edges = [e for e in row.depends_on if not (isinstance(e, dict) and e.get("chunk") in landed)]
        kept.append(row._replace(depends_on=edges) if len(edges) != len(row.depends_on) else row)
    return kept, [e for e in exclusions if e.get("id") in keep]


def _memo_draft_steps(
    root: Path,
    run_dir: Path,
    owed: Sequence[OwedRow],
    raw_by_id: Mapping[str, Mapping],
    intent: str,
    staged: Sequence[ManifestRow] = (),
) -> tuple[list[OwedRow], list[ManifestRow]]:
    """Does the outbox hold the draft each owed `memo-send` row's send needs? One dispatched draft
    step per row whose draft is absent and whose receiver is named; the row stays owed for the EM's
    send either way, its detail saying why no step was staged. No subagent sends.

    The step inherits the owed row's `depends_on` edges that name a `staged` row, and sits one wave
    after the latest of them: the draft carries content those rows produce."""
    from coordinator_core.ops.dispatch_emit.spine_read import memo_draft_fields
    from coordinator_core.session.machinery_paths import LEGACY_MEMO_OUTBOX_RELDIR, MEMO_OUTBOX_RELDIR

    out_owed: list[OwedRow] = []
    steps: list[ManifestRow] = []
    for o in owed:
        if o.reason != "memo-send":
            out_owed.append(o)
            continue
        raw = raw_by_id.get(o.id) or {}
        fields, why = memo_draft_fields(dict(raw))
        if fields is None:
            out_owed.append(replace(o, detail=f"{o.detail} (no draft step staged: {why})"))
            continue
        slug = fields["slug"]
        draft_rel = f"{MEMO_OUTBOX_RELDIR}/{slug}.md"
        if (root / draft_rel).is_file() or (root / LEGACY_MEMO_OUTBOX_RELDIR / f"{slug}.md").is_file():
            out_owed.append(o)
            continue
        deps, wave = _staged_dependencies(raw, staged)
        step_id = f"{o.id}-memo-draft"
        brief = run_dir / "briefs" / f"{step_id}.md"
        body = raw.get("brief") or raw.get("body") or ""
        text = (
            f"Stage the outbox draft for memo-send row {o.id}. Run, from the repo root:\n\n"
            f"    cross-repo-memo draft {slug} --to {shlex.quote(fields['to'])} --kind {fields['kind']} "
            f"--title {shlex.quote(fields['title'])}\n\n"
            f"Then Edit the memo body in {draft_rel} to carry the row's content below. Never run "
            "`cross-repo-memo send`: the EM sends. Touch no other file.\n\n"
            f"Row body:\n\n{body}\n"
        )
        _write(brief, (intent + "\n" + text) if intent else text)
        steps.append(
            ManifestRow(
                id=step_id,
                agent_type=_EXECUTOR_AGENT_TYPE,
                model=resolve_agent_model(_EXECUTOR_AGENT_TYPE, None),
                brief_path=_rel(root, brief),
                writes=(draft_rel,),
                wave=wave,
                deps=deps,
            )
        )
        out_owed.append(o)
    return out_owed, steps


def _staged_dependencies(raw: Mapping, staged: Sequence[ManifestRow]) -> tuple[tuple[str, ...], int]:
    """The ids `raw.depends_on` names among `staged`, and the wave one past the latest of them (0 for none)."""
    waves = {r.id: r.wave for r in staged}
    ids: list[str] = []
    for edge in raw.get("depends_on") or ():
        chunk = edge.get("chunk") if isinstance(edge, Mapping) else edge
        if isinstance(chunk, str) and chunk in waves and chunk not in ids:
            ids.append(chunk)
    return tuple(ids), (max(waves[i] for i in ids) + 1 if ids else 0)


def _group_rows(
    rows: Sequence[ManifestRow], sizes: Mapping[str, int], raw_by_id: Mapping[str, Mapping]
) -> tuple[list[ManifestRow], tuple[tuple[str, ...], ...]]:
    """Rows with `group` set where small same-wave rows share one agent, and every agent's member
    ids in proposal order (solo rows included). Reviewer and `needs_slot` rows get a unique key so
    they stay solo; a one-member group leaves `group` empty. Row order is unchanged."""
    units = []
    for r in rows:
        solo = r.agent_type == REVIEWER_AGENT_TYPE or (raw_by_id.get(r.id) or {}).get("needs_slot") is True
        key = f"solo:{r.id}" if solo else f"{r.wave}|{','.join(sorted(r.deps))}|{r.agent_type}|{r.model}"
        units.append(Unit(r.id, key, sizes.get(r.id, 0)))
    groups = propose_groups(sorted(units, key=lambda u: u.key))
    group_of = {m: f"g{next(r.wave for r in rows if r.id == g[0])}-{g[0]}" for g in groups if len(g) > 1 for m in g}
    return [replace(r, group=group_of.get(r.id, "")) for r in rows], groups


def _refuse_unapproved_design(sizing: Mapping, sizing_rel: str) -> None:
    """Refuse staging under a canvas design_route the PM has not approved."""
    payload, error = design_route.read(sizing, sizing_rel)
    if error:
        raise AskStageError(error)
    refusal = design_route.stage_refusal(payload)
    if refusal:
        raise AskStageError(refusal)


def _refuse_misrouted_xs_mint(sizing: Mapping, sizing_rel: str, writes: Sequence[str]) -> None:
    """Raise AskStageError `XS-MINT-MISROUTED` for a sizing the XS mint must not take: one with a
    `plan:` edge or a non-XS `estimate.tshirt`; `XS-MINT-NO-WRITES` for an empty `writes`.

    A sizing with no readable tshirt is left to the writes check (the mint itself needs no estimate)."""
    edge = sizing.get("plan")
    has_edge = isinstance(edge, str) and bool(edge.strip())
    try:
        arm = resolve_arm(sizing)
    except SizingFireRefused:
        arm = ARM_XS
    if has_edge or arm != ARM_XS:
        why = f"plan edge {edge!r}" if has_edge else f"arm {arm!r}"
        raise AskStageError(
            f"XS-MINT-MISROUTED: {sizing_rel} has {why}; stage it by plan_path, not sizing_path"
        )
    if not any(str(w).strip() for w in writes):
        raise AskStageError(f"XS-MINT-NO-WRITES: {sizing_rel} is an XS arm and needs at least one writes path")


LIVE_RUN_WRITE_OVERLAP = "live-run-write-overlap"


def _refuse_live_write_overlap(root: Path, run_id: str, writes: Sequence[str]) -> None:
    """Raise AskStageError `live-run-write-overlap` naming each other live run whose staged
    footprint intersects `writes`, and release this run's token so the refusal ends the run.
    Trap: not atomic; two stages interleaving before either writes its manifest both pass."""
    from coordinator_core.ops.dispatch_emit import live_run_writes, run_token

    hits = live_run_writes.overlaps(writes, live_run_writes.live_footprints(root, exclude_run_id=run_id))
    if not hits:
        return
    named = "; ".join(f"{rid}: {', '.join(paths)}" for rid, paths in sorted(hits.items()))
    run_token.consume(root, run_id)
    raise AskStageError(f"{LIVE_RUN_WRITE_OVERLAP}: writes overlap live run {named}. Resubmit when it lands.")


def stage(
    repo_root: Path,
    *,
    run_id: str,
    plan_rel: Optional[str] = None,
    sizing_rel: Optional[str] = None,
    writes: Sequence[str] = (),
    gated: Sequence[Mapping] = (),
    cross_repo_approved: bool = False,
    session_id: Optional[str] = None,
    commit_sizing_rel: Optional[str] = None,
    only_rows: Optional[Sequence[str]] = None,
    ledger_resume: bool = False,
) -> StageManifest:
    """Stage the run for `plan_rel`, or for the XS sizing `sizing_rel` (minted to X1 over `writes`).

    `only_rows` (a resume: the halted plan's incomplete row ids) stages just those rows; the rest
    are landed, so an edge onto one is satisfied. A resume whose rows are all owed or gated
    returns a manifest with no rows and the owed/gated lists.
    `ledger_resume=True` (an all-landed plan resumed at its ledger) admits a spine with zero
    dispatchable rows and returns a whole manifest with rows []; the default refuses it.
    """
    root = Path(repo_root)
    if not safe_id(run_id):
        raise AskStageError(f"run_id {run_id!r} is not a single safe path segment")
    if bool(plan_rel) == bool(sizing_rel):
        raise AskStageError("name exactly one of plan_rel and sizing_rel")

    run_dir = contained_path(root / RUN_DIR_ROOT / run_id, [root / RUN_DIR_ROOT])
    if run_dir is None:
        raise AskStageError(f"run dir for {run_id!r} escapes {RUN_DIR_ROOT}")
    try:
        record = record_dir(root, run_id)
    except ValueError as exc:
        raise AskStageError(str(exc)) from exc

    if sizing_rel:
        sizing = load_sizing(root, sizing_rel)
        run_sizing_rel, run_sizing = sizing_rel, sizing
        _refuse_unapproved_design(sizing, sizing_rel)
        _refuse_misrouted_xs_mint(sizing, sizing_rel, writes)
        spine_text, spine_path = mint_xs_spine(
            sizing, sizing_rel=sizing_rel, writes=writes, out_dir=record, gated=gated
        )
        _write(spine_path, spine_text)
        plan_path = spine_path
    else:
        plan_path = contained_path(root / plan_rel, [root])
        if plan_path is None or not plan_path.is_file():
            raise AskStageError(f"plan not found under the repo: {plan_rel!r}")
    plan_text = plan_path.read_text(encoding="utf-8")
    if not sizing_rel:
        run_sizing_rel = _run_sizing_rel(commit_sizing_rel, plan_text)
        run_sizing = _run_sizing(root, run_sizing_rel)
        # An unloadable sizing_object yields None here and skips the design check (fails open).
        if run_sizing is not None:
            _refuse_unapproved_design(run_sizing, run_sizing_rel)
    plan_posix = _rel(root, plan_path)

    if plan_rel:
        unfilled = scaffold_markers(plan_text)
        if unfilled:
            named = ", ".join(f"{k} (line {v})" for k, v in sorted(unfilled.items()))
            raise AskStageError(f"PLAN-SCAFFOLD-UNFILLED: {plan_posix} still carries {named}")
    exclusions: list = []
    raw_by_id = {
        raw["id"]: raw
        for raw in load_rows(plan_text).rows
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    try:
        rows = read_spine(
            plan_path,
            exclusions=exclusions,
            cleared_gate_owners=own_gate_owners(raw_by_id.values(), root),
        )
    except InvalidChangeKindError as exc:
        raise AskStageError(f"INVALID-CHANGE-KIND: {plan_posix}: {exc}") from exc
    if only_rows is not None:
        rows, exclusions = _only_rows(
            rows, exclusions, frozenset(only_rows), resume_snapshot.read(root, run_id, plan_posix)
        )
    check_unschedulable_rows(rows, raw_by_id)
    _split = split_frontmatter(plan_text)
    _drift = plan_pin_drift(_split.fm_text, root) if _split is not None else []
    if _drift:
        raise AskStageError(pin_drift_reason(_drift))
    gated = _with_owner_roots(gated_rows(exclusions, raw_by_id))
    owed = [
        OwedRow(id=e["id"], reason=e["reason"], detail=e.get("detail") or "")
        for e in exclusions
        if e.get("reason") in EM_OWNED_REASONS
    ]
    try:
        check_cross_repo_writes(rows, root, approved=cross_repo_approved, session_id=session_id)
    except CrossRepoWriteError as exc:
        raise AskStageError(str(exc)) from exc
    # Split, not refuse: runtime_cap is CPU-dependent and a refusal would halt hands-off runs.
    waves = cap_waves(build_waves(rows), sibling_split.runtime_cap())
    if not waves and not ledger_resume and not (only_rows is not None and (owed or gated)):
        named = f"; gated: {', '.join(g.id for g in gated)}" if gated else ""
        if owed:
            named += f"; owed by the EM: {', '.join(o.id for o in owed)}"
        raise AskStageError(f"the spine derives zero dispatchable rows{named}")

    context = derive_plan_context(
        plan_text, fallback_title=plan_path.stem, repo_root=root.as_posix()
    )

    clauses = _discharge_clauses_for(plan_text, root, list(raw_by_id.values()))

    ask_file = record / ASK_TEXT_FILE
    ask_text = ask_file.read_text(encoding="utf-8") if ask_file.is_file() else None
    pm_block = pm_intent_block(run_sizing, ask_text)
    if pm_block:
        _write(record / PM_INTENT_FILE, pm_block)
    relayed_file = record / RELAYED_ASK_FILE
    relayed_text = relayed_file.read_text(encoding="utf-8") if relayed_file.is_file() else None
    relayed = relayed_ask_block(relayed_text)
    intent = pm_block + ("\n" if pm_block and relayed else "") + relayed
    if run_sizing_rel:
        # The gate bound it already; idempotent, and it folds in a revision queued since.
        try:
            requirements_ledger.bind(root, run_id, run_sizing_rel, ask_text=ask_text, relayed_text=relayed_text)
        except requirements_ledger.LedgerRefused as exc:
            raise AskStageError(f"requirements: {exc}") from exc
    test_targets, no_test_target = _test_scope(waves, root)

    after = {node.row.id: tuple(node.after) for node in dag_from_waves(waves).nodes}
    manifest_rows: list[ManifestRow] = []
    chunks: list[ChunkCommit] = []
    declared: list[str] = []
    brief_sizes: dict[str, int] = {}
    for wave_no, wave in enumerate(waves):
        for row in wave:
            agent_type = _row_agent_type(row)
            _model_opt(agent_type, row.agent_model)  # raises on bad model grammar or a pinned-type override
            model = resolve_agent_model(agent_type, row.agent_model)
            brief = run_dir / "briefs" / f"{row.id}.md"
            prior = _set_aside_prior_report(root, _dispatch_report_path(plan_posix, row.id))
            if agent_type == REVIEWER_AGENT_TYPE:
                text = _reviewer_brief(root, run_dir, row.id, raw_by_id.get(row.id) or {})
            else:
                text = _row_prompt(row, plan_posix, context)
                if intent:
                    text = intent + "\n" + text
                if row.id in clauses:
                    text = text.rstrip() + "\n\n" + clauses[row.id] + "\n"
                if prior:
                    text = text.rstrip() + "\n\n" + _prior_report_line(prior) + "\n"
            _write(brief, text)
            brief_sizes[row.id] = len(text.encode("utf-8"))
            paths = _widen_with_test_candidates(
                commit_pathspec_or_none([row]) or [], root
            )
            declared.extend(paths)
            manifest_rows.append(
                ManifestRow(
                    id=row.id,
                    agent_type=agent_type,
                    model=model,
                    brief_path=_rel(root, brief),
                    writes=tuple(paths),
                    wave=wave_no,
                    deps=after.get(row.id, ()),
                )
            )
            chunks.append(
                ChunkCommit(
                    id=row.id,
                    title=row.title,
                    paths=tuple(paths),
                    prefixes=tuple(row.writes_under),
                    report=_dispatch_report_path(plan_posix, row.id),
                )
            )

    manifest_rows, row_groups = _group_rows(manifest_rows, brief_sizes, raw_by_id)
    owed, draft_rows = _memo_draft_steps(root, run_dir, owed, raw_by_id, intent, manifest_rows)
    manifest_rows.extend(draft_rows)
    fanout = FanoutCost(
        units=len(manifest_rows),
        agents=len(row_groups) + len(draft_rows),
        basis="exact",
        groups=(*row_groups, *((r.id,) for r in draft_rows)),
    )

    docs = doc_writes(run_id, run_sizing)
    _refuse_live_write_overlap(
        root, run_id, [*declared, *docs, *(w for r in draft_rows for w in r.writes)]
    )
    # The docs stage writes only these; terminal_commit holds them to the chunk test code rows get.
    chunks.append(ChunkCommit(id=DOCS_CHUNK_ID, title="run docs", paths=docs))
    carried_sizing = sizing_rel or commit_sizing_rel
    if carried_sizing:
        # The run flips the sizing to routed and links its plan; no row declares it.
        chunks.append(
            ChunkCommit(id=SIZING_CHUNK_ID, title="sizing record", paths=(carried_sizing,))
        )

    branch = head_branch(root)
    marker = render_marker(
        CommitRequest(
            chunks=tuple(chunks),
            deliverable_id=_plan_deliverable_id(plan_text),
            repo_root=root.as_posix(),
            plan_path=plan_posix,
            expected_branch=None if branch in (None, "HEAD") else branch,
        )
    )
    marker_path = run_dir / "commit-request.txt"
    _write(marker_path, f"{marker}\n" if marker else "")

    manifest = StageManifest(
        run_dir=_rel(root, run_dir),
        rows=tuple(manifest_rows),
        review_declared_paths=tuple(_dedupe_preserve_order(declared)),
        marker_path=_rel(root, marker_path),
        plan_id=_plan_id(plan_text),
        gated=tuple(gated),
        owed=tuple(owed),
        engine_rows=_engine_rows(root, plan_path, raw_by_id, gated, run_sizing),
        test_targets=test_targets,
        no_test_target=no_test_target,
        fanout=fanout.meta_dict(),
        doc_writes=docs,
        rulable_ids=rulable_ids(
            [r for r in ((run_sizing or {}).get("requirement_register") or {}).get("rows") or [] if isinstance(r, Mapping)]
        ),
    )
    _write(record / "manifest.json", json.dumps(manifest_payload(root, manifest), indent=2) + "\n")
    if session_id:
        # Confined reviewers' Edits are admitted only for review-targets.txt
        # paths; the plan route registers them at compose time, the ask route
        # only learns them here.
        try:
            targets_add(root, session_id, list(manifest.review_declared_paths))
        except LedgerError:
            pass
    return manifest


def manifest_payload(root: Path, manifest: StageManifest) -> dict:
    """`manifest.to_json()` plus `head_sha`: the execute-start HEAD (stage commits nothing), None when unresolvable."""
    return {**manifest.to_json(), "head_sha": read_head_sha(root)}


@register_op("dispatch.ask_stage")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ask_stage": params `run_id` (str), exactly one of `plan_path` / `sizing_path`, `writes`.

    Replies `StageManifest.to_json()` plus `head_sha`; a refused request replies `{"error": str}`.
    """
    if repo_root is None:
        return {"error": "dispatch.ask_stage requires repo_root"}
    refusal = validate_params("dispatch.ask_stage", params, _PARAMS)
    if refusal is not None:
        return refusal
    writes = params.get("writes") or []
    run_id = params["run_id"]
    root = main_worktree_root(Path(repo_root))
    try:
        manifest = stage(
            root,
            run_id=run_id,
            plan_rel=params.get("plan_path") or None,
            sizing_rel=params.get("sizing_path") or None,
            writes=writes,
            gated=[g for g in (params.get("gated") or []) if isinstance(g, dict)],
            cross_repo_approved=bool(params.get("cross_repo_approved")),
            session_id=params.get("session_id") or None,
            commit_sizing_rel=params.get("commit_sizing_path") or None,
            only_rows=params["only_rows"] if params.get("only_rows") is not None else None,
        )
    except (AskStageError, SizingFireRefused) as exc:
        return {"error": str(exc)}
    return manifest_payload(root, manifest)
