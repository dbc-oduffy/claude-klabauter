"""
coordinator_core.ops.dispatch_emit.op — JSON-RPC "dispatch.emit" operation.

Purpose: thin RPC wrapper that registers the dispatch-emit pipeline
(``spine_read`` -> ``wave_map`` -> ``pathspec`` -> ``emit``,
docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md § C5) as an
op, and is the ONE place in this pipeline that touches disk for a write.
Every upstream module (``spine_read.py``, ``wave_map.py``, ``pathspec.py``,
``emit.py``) is pure — this module composes their output via
``emit.emit_script`` and writes the resulting script TEXT to a caller-named
path, path-guarded through ``coordinator_core.ops._path_guard.contained_path``
before the write (never written unguarded).

Unlike ``workflow.scaffold`` (returns text only, no disk write) and
``workflow.validate`` (reads a caller-supplied path, guarded via
``coordinator_core.cartography._guard.path_guard``), this op is the WRITE
leg — mirrors the containment shape most write-ops in this package family
use (``coordinator_core.ops._path_guard.contained_path`` with an explicit
``allowed_roots`` list), not the read-only ``cartography._guard`` shape.

Wire params:
    plan_path (str, required unless the queue route is used) — plan file to
                                     read the task spine from (passed straight
                                     to ``emit.emit_script``). Mutually
                                     exclusive with ``queue``/``profile``
                                     (``QueuePlanConflictError``).
    queue (list[str], required for the queue route) — one or more queue
                                     directories, forwarded to
                                     ``queue_emit.emit_queue_script``. Present
                                     (with ``profile``) selects the queue
                                     route instead of the plan route; queue
                                     input never falls back to the wave path.
    profile (str, required for the queue route) — the queue-grind profile
                                     name, forwarded to
                                     ``queue_emit.emit_queue_script``.
    profile_dir (str, required for the queue route) — directory
                                     ``<profile>.yaml`` lives under.
                                     Deliberately NOT containment-guarded
                                     against ``repo_root``/``target_root``:
                                     unlike ``queue`` (row content), a DoE
                                     profile is an operator-trusted input
                                     that routinely lives in a different
                                     repo (coordinator-content-repo) than the one whose
                                     rows are being closed. Guarding it here
                                     would refuse a legitimate cross-repo
                                     profile_dir in production.
    appetite (str, optional, default "standard") — forwarded to
                                     ``queue_emit.emit_queue_script``.
    overrides (dict, optional)    — knob overrides, keys ⊆ {"where", "limit",
                                     "budget_tokens"}, forwarded verbatim.
    output_path (str, required)   — path to write the emitted ``.mjs`` script
                                     to. Path-guarded under ``target_root``
                                     BEFORE the file is written. On the queue
                                     route, its guarded parent is passed to
                                     ``emit_queue_script`` as ``run_dir``.
    target_root (str, optional)   — explicit containment root. If omitted,
                                     the containment root defaults to
                                     ``repo_root`` (the per-request resolved
                                     repo root) when the caller's request
                                     carries one, so the guard meaningfully
                                     constrains a WRITE op (unlike a
                                     parent-of-output default, which is
                                     trivially satisfied by any path). Only
                                     when ``repo_root`` is unavailable does
                                     this fall back to ``output_path``'s own
                                     parent directory — the same
                                     default-derivation shape
                                     ``workflow.validate`` uses for its
                                     READ-only guard.
    name (str, optional)          — forwarded to ``emit.emit_script``.
    description (str, optional)   — forwarded to ``emit.emit_script``.
    pipeline (str, optional)      — the PIPELINE route: a DoE manifest name
                                     (``<content-root>/pipelines/**/<name>.manifest.yaml``).
                                     Needs ``brief`` (non-empty) and ``repo_root``
                                     or ``target_root``; optional ``subjects``
                                     (a list of strings or objects with a ``subject``
                                     key, or a research spec object with ``subjects``
                                     and ``topics``), ``lists`` (name -> list; a roster
                                     entry is ``{slug, agent_type}`` or ``slug=agent_type``),
                                     ``scratch_dir`` (repo-relative,
                                     guarded), ``flags`` (str->str|bool) and ``validator``
                                     (a one-line command; ``{{validator}}`` fills it, and a
                                     stage naming that token is emitted only when it is
                                     given, so a manifest fans it per batch with
                                     ``over: inputs.<list>``). Exclusive of
                                     every other route selector
                                     (``PipelineParamConflictError``). Output
                                     defaults to
                                     ``scratch/warp/<run-id>.workflow.mjs``;
                                     receipt extras add pipeline/run_id/
                                     manifest_sha256/subjects. Emit-only. Every
                                     emit declares the run's scratch dir as a
                                     per-run output root readers may write
                                     across a repo boundary
                                     (``bump_out_of_repo_tool_write`` honours
                                     it); the reply carries ``run_output_root``
                                     (absolute).
    resume_missing (bool, optional) — pipeline route: re-emit only what an
                                     interrupted run did not finish. Needs the
                                     interrupted run's ``scratch_dir``. Each
                                     element of an ``over: inputs.<list>`` stage
                                     whose filled ``output`` is already a
                                     non-empty file is dropped from the emitted
                                     script; the reply and receipt carry
                                     ``resume_missing: {list: {expected,
                                     present, missing}}``. Refused when no stage
                                     fans over an input list or nothing is missing.
    cloud_spawn (dict, optional)  — the CLOUD-SPAWN route: ``{kind: probe|worker,
                                     source_repo, parent_session_id,
                                     channel_pr, question}``. Replies
                                     ``{"ok": True, "create_session":
                                     {source_url, prompt, title}}`` and writes
                                     nothing; mutually exclusive with every
                                     other route param. See
                                     ``cloud_spawn_brief``.

    session_id (str, optional)    — recorded verbatim in the provenance
                                     receipt. Absent, the fleet-canonical
                                     ``session.core.resolve_session_id``
                                     ladder answers (bound request identity,
                                     ``COORDINATOR_SESSION_ID``,
                                     ``CLAUDE_SESSION_ID``,
                                     ``CLAUDE_CODE_SESSION_ID``); absent all
                                     of those, the empty string. NEVER
                                     minted — see ``_receipt_session_id``.

    ask (str | true, optional)    — the ASK route: a raw prompt, or true with
                                     ``sizing_path``. Composes the one
                                     in-session script via
                                     ``ask_compose.compose_ask_script``; output
                                     defaults to
                                     ``scratch/warp/<run-id>.workflow.mjs``.
                                     The reply adds ``run_id``.
    writes (list[str], optional)  — the ask's file footprint (XS); refused at
                                     emit when any path is outside repo root.
    sizing_path (str, optional)   — an existing sizing under
                                     ``state/sizings/``; implies the ASK route.
                                     Exclusive of plan/inventory/queue
                                     (``SizingPathConflictError``).
    baton (str, optional)         — ASK route: an existing baton under
                                     ``state/handoffs/``; deliverable_id (str,
                                     optional) must equal its id.

Reply fields:
    {"path": "<written path>", "ok": bool,
     "findings": [{"severity","code","message","line"?}, ...],
     "error_count": int, "warn_count": int,
     "receipt": "<written receipt path>" | None,
     "fire_args": {"repoRoot": "<posix root>", "runToken"?: "<hex>"} | absent,
     "run_id", "scratch_dir": ask/pipeline routes (scratch_dir: pipeline only)}
    ``receipt`` names the provenance sidecar written beside the script, or is
    ``None`` when it could not be written — receipt writing is best-effort and
    never fails the emit (§ The receipt is a property of emitting).
    ``ok := error_count == 0`` (WARN findings never fail the verdict) — the
    same run_checks verdict shape ``workflow.validate`` returns. The op
    writes the script to disk and returns this verdict for transparency; it
    does not refuse to write on a non-zero ``error_count`` — ``emit.py``'s
    own construction already targets AC5's zero-ERROR bar, and any refusal
    for an under-declared spine (``NoWavesError``, ``NoWritesDeclaredError``)
    is raised by ``emit_script``/``pathspec.py`` BEFORE this op ever reaches
    the write, and propagates uncaught. ``pathspec.NoTestTargetError`` is
    NOT one of these any more: ``emit.compose_script`` catches it and
    degrades to a falsifier phase or a loud no-test-phase narration instead
    of vetoing the emit — see ``emit.py`` module docstring § The terminal
    phase degrades, it never vetoes.
    ``fire_args`` is present, plan route only, when
    ``repo_root or _repo_root_for_plan(plan_path)`` resolves to a root —
    ``{"repoRoot": Path(root).as_posix()}``, a convenience for a caller that
    hand-fires from this reply. The key is OMITTED (never ``null``) when
    nothing resolves, and never present on the queue route. This is
    independent of ``fire.py``'s own root binding at fire time (which
    resolves and binds its own root regardless of whether this reply carries
    the key) — backward compatible, and a script emitted before this field
    existed still fires by ignoring an absent ``args``. On the ask route, when the emit minted
    the run's commit token, ``fire_args.runToken`` carries it: the launch args are the token's
    only carrier, never the script's bytes or a file under the repo.

The receipt is a property of emitting, not of one repo's wrapper:
    Every emission route writes a provenance sidecar at
    ``<script>.mjs.emitted.json``. The OTHER producer of that same sidecar is
    coordinator-content-repo's wrapper CLI ``coordinator/bin/emit-dispatch-workflow.py``
    (``_write_emission_receipt`` / ``script_sha256`` / ``restamp``), and the
    consumer is a DoE-side hook that verifies ``sha256`` against the script
    bytes on disk. The shape is therefore a CROSS-REPO CONTRACT: same keys,
    same raw-bytes digest, same ``isoformat(timespec="seconds")``, same
    ``json.dumps(..., indent=2, sort_keys=True) + "\\n"`` serialisation. A
    receipt differing by one key reads as tampering to that hook, which is
    worse than no receipt at all. The shape is REIMPLEMENTED here, never
    imported: claude-klabauter must not depend on a DoE path. Before this op wrote it,
    provenance depended on WHICH route emitted — a script emitted through the
    registered op was indistinguishable from a hand-authored one.

    The receipt is read by a FIRING GATE, not only by humans. ``session_id``
    and ``sha256`` are load-bearing: an empty or mismatched value has a
    behavioural consequence, not merely a loss of provenance. A receipt naming
    no session reads to the gate as a peer's claim and the emission is REFUSED
    at fire time ("its receipt names session ; this session is <id>") — which
    is why ``session_id`` resolves through the fleet-canonical ladder rather
    than a single env var, and why a mis-hashed ``sha256`` reads as tampering.

Negative-spec:
  - Does NOT derive waves, pathspecs, or script text itself — delegates
    entirely to ``emit.emit_script`` (plan route) or
    ``queue_emit.emit_queue_script`` (queue route). This module's only
    original code is the path guard, the route dispatch, the foreign-emission
    refusal, and the disk write.
  - Does NOT accept ``plan_path``/``inventory_path`` together with
    ``queue``/``profile`` — ``QueuePlanConflictError`` refuses the
    combination before either route runs.
  - Does NOT enumerate the tree, glob, or shell out. Exactly TWO filesystem
    targets, both fixed by the caller's one already-guarded ``output_path``
    and neither discovered by survey: (1) the guarded ``output_path`` itself —
    ``is_file()``/``read_bytes()``/``stat()`` for the foreign-emission
    comparison, one ``Path.write_text()``, and one ``read_bytes()`` to digest
    the bytes that actually landed; (2) the provenance receipt beside it,
    whose path is ``Path.with_name()`` off the ALREADY-GUARDED
    ``guarded_path`` — never off the raw ``output_path``, since deriving a
    write target from the unguarded input would reintroduce exactly the path
    escape ``contained_path`` exists to stop. Covered by
    ``tests/test_no_tree_survey.py``'s AST gate (extended to ``emit.py``;
    this module reads/writes no tree-survey surface of its own to gate).
  - Does NOT make ``output_path`` unique per session. Resume addresses the
    script by its deterministic name, so uniqueness would break resume;
    ``ForeignEmissionError`` is the mechanism instead.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C5
"""

from __future__ import annotations

import hashlib
import json
from coordinator_core.atomic_replace import atomic_write_bytes
import os
import re
import shlex
import stat
import sys
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from coordinator_core._content_root_primitive import content_root_for
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.content_root import read_content_root
from coordinator_core.git.commit_trailers import read_host_commit_trailers
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._research_grounding import GROUNDING_REPORT
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_SOURCE_PM,
    ASK_SOURCE_RELAYED,
    ASK_SOURCES,
    ASK_TEXT_FILE,
    RELAYED_ASK_FILE,
    RUN_DIR_ROOT,
    STOP_AFTER_KEY,
    STOP_AFTER_PLAN,
    record_dir,
)
from coordinator_core.ops.dispatch_emit import route_preference as _route_pref
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import slice_ask_source
from coordinator_core.ops.dispatch_emit.run_id_occupancy import spent_run_reason
from coordinator_core.ops.dispatch_emit.run_token import normalize_sizing_rel
from coordinator_core.ops.dispatch_emit.cloud_spawn_brief import build_cloud_spawn
from coordinator_core.ops.dispatch_emit.delivery_credit import rows_backed_before_base
from coordinator_core.ops.dispatch_emit.grind_admission import NOT_ADMITTED_EXTRA_KEY
from coordinator_core.ops.dispatch_emit.emission_receipt import (
    _load_review_inputs,
    _receipt_session_id,
    _script_sha256,
    _write_emission_receipt,
    emission_receipt_path,
)
from coordinator_core.ops.dispatch_emit.emit import (
    check_agent_types_resolve,
    NoReviewStageError,  # noqa: F401 -- re-exported for callers
    NoWavesError,
    ScriptOverCapError,
    emit_script,
    resolve_agent_type_host,
)
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    RUN_ID_PREFIX,
    PipelineEmitRefused,
    PipelineInputs,
    MANIFEST_SUFFIX,
    PIPELINES_DIR,
    segment_name,
    subject_key,
)
from coordinator_core.ops.dispatch_emit.pipeline_inputs import normalize_lists, subjects_from_value
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import (
    plans_with_falsifier,
    review_inputs,
    review_specs,
)
from coordinator_core.ops.dispatch_emit.inventory_mint import (
    _DEP_KIND_LIVE,
    DEFAULT_MAX_INVENTORY_ROWS,
    _resolve_dep_kinds,
    _row_body,
    _split_footprint,
    _split_id_list,
    _strip_backtick,
    mint_spine,
    parse_chunk_table,
)
from coordinator_core.ops.dispatch_emit.landed_reconcile import reconcile_landed
from coordinator_core.ops.dispatch_emit.queue_emit import QueuePathEscapeError, emit_queue_script
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
from coordinator_core.ops.review_mint import op as review_mint_op  # noqa: F401 -- patched via this module by tests
from coordinator_core.ops.review_mint.roster import (
    EMIT_ROUTE_INVENTORY,
    EMIT_ROUTE_PLAN,
    EMIT_ROUTE_QUEUE,
)
from coordinator_core.session.record_homes import home_dir
from coordinator_core.ops._param_alias import aliased_param, spellings

def _installed_plugin_root() -> Optional[str]:
    """The coordinator plugin's content root from the on-disk install (the
    harness install record, then the configured content root); None when no
    install is found. File reads only -- the emitter subprocess carries no
    harness plugin env, so ``CLAUDE_PLUGIN_ROOT`` alone under-reports."""
    from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

    return resolve_plugin_root()


# Generator-provenance: writes the emitted script to a caller-supplied,
# path-guarded output_path -- no fixed target, purely caller-named.
GENERATES = []


class InventoryOutsideRepoError(ValueError):
    """The inventory record sits outside the repo's `state/mise-inventory/`."""


class InventoryPathConflictError(ValueError):
    """Raised when a caller passes both ``plan_path`` and ``inventory_path``.

    Mutually exclusive: ``plan_path`` names a hand-authored plan-tasks
    spine to emit directly; ``inventory_path`` names a mise-inventory
    record this op mints a spine FROM first
    (``inventory_mint.mint_spine``), then emits. Accepting both would leave
    one of the two silently ignored -- see
    ``docs/plans/2026-09-18-doe-holds-no-scripts.md`` § S1-C4.
    """


class QueueRootMissingError(ValueError):
    """Raised on the queue route when neither the request's ``repo_root``
    nor an explicit ``target_root`` param is given.

    The queue route resolves relative ``queue`` directories against
    ``target_root`` (S5) -- with neither supplied, ``target_root`` would
    silently default to ``output_path``'s own parent (the plan route's
    fallback), anchoring queue-dir resolution and containment to wherever
    the caller happened to name ``output_path``, not the repo the queue
    rows actually live in. Refused rather than defaulted."""


class QueuePlanConflictError(ValueError):
    """Raised when a caller passes ``queue``/``profile`` together with
    ``plan_path``/``inventory_path``.

    Mutually exclusive: the queue route (``queue_emit.emit_queue_script``)
    composes a script from a profile and a frozen row manifest; the plan
    route (``emit.emit_script``) composes one from a hand-authored task
    spine. Accepting both would leave one silently ignored, same reasoning as
    ``InventoryPathConflictError`` -- queue input never falls back to the
    wave path (§ Design § Entrypoint).
    """


class SizingPathConflictError(ValueError):
    """Raised when ``sizing_path`` is passed with ``plan_path``/``inventory_path``/
    ``queue``/``profile``; the sizing route selects its own arm."""


class PipelineParamConflictError(ValueError):
    """Raised when ``pipeline`` is passed with another route's selector
    (plan/inventory/queue/profile/ask/sizing_path/cloud_spawn)."""


class ForeignEmissionError(ValueError):
    """Raised when ``output_path`` already holds a DIFFERENT session's emission.

    The plan-relative default (``<plan-basename>.workflow.mjs``) is a pure
    function of the plan, so two sessions executing the same plan target the
    same file with no claim between them. Identical bytes are the ordinary
    case and stay silent -- the emitter is deterministic over an unchanged
    plan. Differing bytes mean the plan moved between the two emits, and the
    loser is whichever session emits first and fires second: it fires a wave
    map it never generated. Measured 2026-08-30 (runs wf_7b8b1e10-cbb /
    wf_7c7058e4-6f1), where a peer's emit added a `Wave 1: C10, C11` phase
    ahead of the intended C12 wave and put an explicitly-dropped cross-repo
    memo back in play.

    Negative spec: this does NOT make the path unique per session. The path is
    addressed by name on resume (``Workflow({resumeFromRunId})`` re-reads the
    script from disk), so uniqueness would break resume; refusal is the
    mechanism, and ``force`` is the deliberate override.
    """


def _refuse_unapproved_body(plan_path: str) -> None:
    """Refuse a plan whose body changed after plan review; warn when unverifiable."""
    from coordinator_core.frontmatter.primitives import (
        APPROVED_BODY_CHANGED,
        APPROVED_BODY_UNVERIFIABLE,
        check_approved_body,
    )

    try:
        text = Path(plan_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    state, message = check_approved_body(text)
    if state == APPROVED_BODY_CHANGED:
        raise ValueError(f"dispatch.emit: {Path(plan_path).name}: {message}")
    if state == APPROVED_BODY_UNVERIFIABLE:
        print(f"dispatch.emit: {Path(plan_path).name}: {message}", file=sys.stderr)


def _certified_plans(inventory_path) -> list:
    """Spec paths of the full certified set an inventory run belongs to: a tranche record
    (`<base>-t<N>.md`) answers for its base inventory, whose Chunk table holds every plan
    the mise certified, so a seam consumer elsewhere in that set is not read as outside it."""
    if not inventory_path:
        return []
    path = Path(inventory_path)
    base = path.with_name(re.sub(r"-t\d+$", "", path.stem) + ".md")
    source = base if base.is_file() else path
    try:
        rows = parse_chunk_table(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted({s for s in (_strip_backtick(r.get("spec path", "")) for r in rows) if s.endswith(".md")})


def _repo_root_for_plan(plan_path: str) -> Optional[Path]:
    """The repo root the PLAN lives in, derived by walking up to the nearest
    ``.git``.

    Why this exists: ``dispatch.emit`` is registered ``scope: none``
    (DR-279), so ``--repo`` is refused and the per-request ``repo_root`` this
    op receives is ALWAYS ``None``. ``emit.emit_script`` uses ``repo_root``
    to anchor the executor prompts it writes -- which repo each dispatched
    agent is working in -- so with no anchor every emitted script says
    nothing about its target tree. That is harmless on a single-repo box and
    wrong the moment one session drives waves in two repos: the prompts are
    interchangeable, and an executor picks whichever tree its cwd happens to
    be in.

    The plan file itself is the one repo-identifying fact this op is always
    handed, so it is what the anchor is derived from. No subprocess: a plain
    parent walk, so this costs no spawn on a path already inside the op's
    invocation budget.

    Negative-spec:
      - Does NOT shell out to ``git rev-parse``. The walk is a filesystem
        check on each ancestor, matching the no-tree-survey AST gate this
        module is held to (``tests/test_no_tree_survey.py``).
      - Does NOT widen the guard. The returned root feeds ``emit_script``'s
        prompt anchoring only; ``target_root``/``contained_path`` containment
        is resolved separately, above, and is unaffected.
      - Does NOT fall back to the process cwd. An unresolvable plan path
        returns ``None``, which restores exactly the pre-existing
        no-anchor behaviour rather than anchoring on an unrelated tree.
    """
    try:
        resolved = Path(plan_path).resolve()
    except OSError:
        return None
    for candidate in resolved.parents:
        if (candidate / ".git").exists():
            return candidate
    return None


class SpentRunOverwriteError(ValueError):
    """Raised when an emit would rewrite a script or spine whose run id has landed or run."""


def _refuse_spent_overwrite(target: Path, new_bytes: bytes, script_path: Path) -> None:
    """Refuse to change `target` when `script_path`'s run id is spent (landed, or a
    land/resume sibling exists). Identical bytes pass: nothing would change. `force` does
    not bypass this refusal: it is checked before the foreign-emission guard."""
    if not target.is_file():
        return
    reason = spent_run_reason(script_path)
    if reason is None or target.read_bytes() == new_bytes:
        return
    raise SpentRunOverwriteError(
        f"refusing to overwrite {target.name}: its run id is spent ({reason}). "
        "The spine path derives from the run id, so a fresh output path does not clear it; "
        "for an --inventory emit pass --row-budget N to continue the family at the next tranche."
    )


def _refuse_foreign_emission(
    output_path: Path, script: str, session_id: str, repo_root: Optional[Path] = None
) -> None:
    """Refuse to overwrite an existing emission whose bytes differ from ours,
    unless its receipt names ``session_id``: re-emitting your own output is
    not a foreign write. The refusal names the target repo-relative (POSIX)
    under ``repo_root`` (short, <=280 chars, pinned by test_op); with no resolvable
    root the absolute path is shown and that bound does not hold.

    Compares against the bytes the write would actually land (``newline=""``,
    so the script's own "
" endings are what reaches disk) -- comparing the
    encoded text against a file written under any other newline policy would
    report every re-emit as foreign on Windows.
    """
    if not output_path.is_file():
        return
    existing = output_path.read_bytes()
    ours = script.encode("utf-8")
    if existing == ours:
        return
    receipt_path = emission_receipt_path(output_path)
    if session_id and receipt_path.is_file():
        try:
            recorded = json.loads(receipt_path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            recorded = None
        if recorded == session_id:
            return
    mtime = datetime.fromtimestamp(output_path.stat().st_mtime).isoformat(timespec="minutes")
    shown = output_path.as_posix()
    if repo_root is not None:
        try:
            shown = output_path.resolve().relative_to(Path(repo_root).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    raise ForeignEmissionError(
        f"{shown} already holds a DIFFERENT emission "
        f"(mtime {mtime}, {len(existing)}B vs ours {len(ours)}B). "
        "Coordinate with that session, name a different output_path, or pass force=true deliberately."
    )


class FiredDriftError(ValueError):
    """Raised by ``guard_against_fired_drift`` when a peer overwrote the
    deterministic emission path after this caller's emit returned but
    before its fire read the bytes back.
    """


def _reserved_run(root: Path, reserved: object, sizing_doc: Optional[dict], sizing_rel: Optional[str]) -> tuple:
    """(run_id, parent_id) for a fan-out slice's reserved run id; ValueError when the id is not the
    sizing's own `em_analysis.ask_parent` run or is bound to a run that has not halted.

    Trap: despite the lookup-shaped name this settles a stale run (writes); never call it read-only.
    A halted slice rebinds: its refire is the only way the siblings held `after` it release.
    A non-landed status with no live run for the sizing is settled `halted` here (a write, made
    before the caller's later checks); a landed or live run is refused by name."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
        STATUS_HALTED, STATUS_LANDED, parse_ask_parent, status_rel,
    )

    if not sizing_rel or sizing_doc is None:
        raise ValueError("dispatch.emit reserved_run_id takes a slice sizing_path, not a raw ask")
    marker = parse_ask_parent((sizing_doc.get("em_analysis") or {}).get("ask_parent"))
    if marker is None or marker[1] != reserved:
        raise ValueError(f"dispatch.emit reserved_run_id {reserved!r} is not the run {sizing_rel} reserves")
    run_id = marker[1]
    status = root / status_rel(run_id)
    try:
        prior = json.loads(status.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return run_id, marker[0]
    except (OSError, ValueError):
        prior = {}
    prior = prior if isinstance(prior, dict) else {}
    if prior.get("status") != STATUS_HALTED:
        if prior.get("status") == STATUS_LANDED:
            raise ValueError(f"run {run_id} already landed at {prior.get('sha')}; residue is a new sizing")
        from coordinator_core import warp_live

        live = warp_live.live_run_for_sizing(root, sizing_rel)
        if live is not None:
            raise ValueError(f"Run {live} is already working {sizing_rel}; its completion is your one wake.")
        from coordinator_core.ops.dispatch_emit import run_close

        run_close.sibling_release(root, run_id, None)
    return run_id, marker[0]


def _halted(status: Path) -> bool:
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

    try:
        return json.loads(status.read_text(encoding="utf-8")).get("status") == STATUS_HALTED
    except (OSError, ValueError):
        return False


def _run_base_rel(run_id: str) -> str:
    """Write-once file holding a reserved run's base sha, beside its status file."""
    return f"scratch/warp/{run_id}/run-base"


def _recorded_run_base(root: Path, run_id: str) -> Optional[str]:
    """What was this run's base before any of its rows committed: the sha its first bind recorded,
    None when no bind has."""
    try:
        return (root / _run_base_rel(run_id)).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _bind_reserved_run(root: Path, run_id: str, parent_id: str) -> None:
    """Copy the parent record into the run's own record dir, then mark the run `running`; the
    exclusive status create is the bind, so a second bind of one id is refused. The first bind
    also records HEAD as the run base, write-once, so a rebind keeps it."""
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
        STATUS_RUNNING, parent_record_rel, run_parent_copy_rel, status_rel,
    )
    from coordinator_core.session import claimed_write

    status = root / status_rel(run_id)
    status.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"run_id": run_id, "status": STATUS_RUNNING}) + "\n"
    try:
        claimed_write.create_exclusive(status, record)
    except FileExistsError:
        if not _halted(status):
            raise ValueError(f"dispatch.emit reserved_run_id {run_id!r} is already bound to a run") from None
        claimed_write.replace_text(status, record)
    parent = root / parent_record_rel(parent_id)
    if not parent.is_file():
        raise ValueError(f"dispatch.emit reserved_run_id {run_id!r}: parent record {parent_record_rel(parent_id)} is missing")
    copy = root / run_parent_copy_rel(run_id)
    copy.parent.mkdir(parents=True, exist_ok=True)
    claimed_write.replace_text(copy, parent.read_text(encoding="utf-8"))
    _record_run_base(root, run_id)


def _seed_run_base(root: Path, run_id: str, base: Optional[str]) -> None:
    """Write `base` as the run's base when it has none yet (write-once)."""
    from coordinator_core.session import claimed_write

    if base and _recorded_run_base(root, run_id) is None:
        claimed_write.replace_text(root / _run_base_rel(run_id), base + "\n")


def _record_run_base(root: Path, run_id: str) -> None:
    """Record HEAD as the run's base, write-once; a spawn-free HEAD read."""
    from coordinator_core.session import claimed_write

    if _recorded_run_base(root, run_id) is None:
        from coordinator_core.git.git_state import head_sha

        base = head_sha(root.as_posix())
        if base:
            claimed_write.replace_text(root / _run_base_rel(run_id), base + "\n")


def _start_requirements(ask_ctx: dict, *, all_of_pm_ask: bool) -> Optional[object]:
    """Make the run ledger-bearing and, for a `--sizing` run, seed that sizing's
    requirement_register with its PM words and the ask now; a raw ask's sizing is minted in-run and
    its gate binds it (the ask itself, verbatim in ask.md or relayed-ask.md, joins then).

    Every stage reads the rows, `workflow.revise` grows them, and the terminal commit judges against them.
    """
    from coordinator_core.ops.dispatch_emit import requirements_ledger as ledger

    root = Path(ask_ctx["root"])
    if ask_ctx.get("reserved_parent"):
        _bind_reserved_run(root, ask_ctx["run_id"], ask_ctx["reserved_parent"])
    if ask_ctx.get("rebound"):
        from coordinator_core.ops.dispatch_emit.halted_run import mark_resumed

        mark_resumed(record_dir(root, ask_ctx["run_id"]))
    if ask_ctx.get("resumes_ask"):
        from coordinator_core.ops.dispatch_emit.halted_run import mark_resumed

        mark_resumed(record_dir(root, ask_ctx["resumes_ask"]))
    if ask_ctx.get("sizing_rel") and not ask_ctx.get("reserved_parent"):
        _seed_run_base(root, ask_ctx["run_id"], ask_ctx.get("inherited_base"))
        _record_run_base(root, ask_ctx["run_id"])
    ledger.start(
        root, ask_ctx["run_id"], all_of_pm_ask=all_of_pm_ask, resumed_from=ask_ctx.get("resumes_ask"),
    )
    if ask_ctx.get("sizing_rel"):
        prompt = ask_ctx.get("prompt")
        relayed = ask_ctx.get("ask_source") == ASK_SOURCE_RELAYED
        return ledger.bind(
            root, ask_ctx["run_id"], ask_ctx["sizing_rel"],
            ask_text=None if relayed else prompt, relayed_text=prompt if relayed else None,
        )
    return None


def _script_path_under(guarded_path: Path, root: str) -> Optional[str]:
    """``guarded_path`` as the repo-relative forward-slash path
    ``dispatch.land_run`` takes as ``script_path``; None if not under ``root``."""
    try:
        return guarded_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def _terminal_commit_script_path(
    guarded_path: Path, repo_root: Optional[Path], plan_path: str, target_root: str
) -> Optional[str]:
    """Repo-root-relative ``script_path`` for ``dispatch.land_run``.

    ``target_root`` defaults to the output's own parent directory when neither
    the request nor the caller names one, so relativising against it alone
    emits a bare basename that land_run (which resolves against the
    repo root) cannot find. The plan's repo root is tried first; ``target_root``
    is the fallback for a script outside that tree.
    """
    plan_root = repo_root or _repo_root_for_plan(plan_path)
    if plan_root is not None:
        rel = _script_path_under(guarded_path, str(plan_root))
        if rel is not None:
            return rel
    return _script_path_under(guarded_path, target_root)


def guard_against_fired_drift(script_path: Path, expected_sha256: str) -> None:
    """Refuse to fire bytes this caller did not emit, ported from
    coordinator-content-repo's ``emit-dispatch-workflow.py ::
    _guard_against_fired_drift`` (this port's leg 3).

    ``_refuse_foreign_emission``/``_guard_against_foreign_overwrite`` close
    the emit leg only: they stop a caller clobbering a peer's already-landed
    emission. The window THIS guard closes is the other direction -- a peer
    writing the same deterministic path AFTER this caller's emit returned
    and BEFORE the script is read back to fire. A fire re-reads from disk,
    so an unguarded fire would execute the peer's wave map under this
    caller's handle with nothing in the handle saying so (DoE measured
    2026-08-30: a fired script had grown an extra wave ahead of the intended
    one, and the pre-empted row's disposition was NO MEMO OWED --
    re-dispatching it would have put an external-facing send back in play).

    ``expected_sha256`` -- a hex sha256 digest, never the script's full
    text. ``_dispatch_emit``'s own reply already carries this exact value
    (its ``"sha256"`` key, computed via ``_script_sha256`` over the bytes it
    just wrote) -- a caller threads that value straight through rather than
    holding the whole script text across the emit/fire boundary just to
    compare it. Digested the same way this module already digests every
    on-disk script (``read_bytes()``, never ``read_text()`` -- see
    ``_script_sha256``'s own docstring on why a text-mode digest disagrees
    with itself across platforms on a file nobody edited).

    Wired into ``coordinator_core.ops.workflow_fire.op :: _workflow_fire``
    (the ``workflow.fire`` RPC op) -- an ``expected`` wire param, when
    supplied, is THIS caller's sha256 (not the script text) and is checked
    immediately before ``fire.fire_workflow``'s spawn. Also wired into this
    package's own CLI (``cli.py :: main``'s ``--fire`` branch), the other
    production path that reaches ``fire_workflow`` -- both callers pass
    ``result["sha256"]`` straight from ``_dispatch_emit``'s reply, never a
    re-hashed or re-read copy.

    Negative spec: does NOT make the path unique -- resume addresses the
    script by its deterministic name, so a session-scoped path would break
    resume. The check is on the bytes' digest, never the filename. A
    missing ``script_path`` is a no-op here (``fire_workflow``'s own
    ``ScriptNotFoundError`` owns that refusal).
    """
    if not script_path.is_file():
        return
    on_disk = script_path.read_bytes()
    on_disk_sha256 = hashlib.sha256(on_disk).hexdigest()
    if on_disk_sha256 == expected_sha256:
        return
    mtime = datetime.fromtimestamp(script_path.stat().st_mtime).isoformat(timespec="seconds")
    raise FiredDriftError(
        f"{script_path} changed between emit and fire (written {mtime}, "
        f"{len(on_disk)} bytes on disk, sha256 {on_disk_sha256}; we emitted "
        f"sha256 {expected_sha256}) -- refusing to fire. A peer overwrote this "
        "deterministic path after our emit, so firing would run THEIR wave "
        "map under our handle, silently. Re-emit and re-read the wave map "
        "before firing: the rows that changed may carry dispositions this "
        "run was not authorized for."
    )


class ForeignSessionRestampError(ValueError):
    """Raised when ``restamp`` is asked to re-stamp a receipt naming a DIFFERENT
    session as the emitter.

    A restamp re-stamps THIS session's own deliberate edit, never a peer's
    emission -- restamping theirs would run their wave map under this
    session's handle with the one guard that notices switched off.
    """


class NoReceiptToRestampError(ValueError):
    """Raised when ``restamp`` is asked to re-stamp a script with no receipt
    beside it -- nothing to restamp, and a receiptless script is not gated
    by the foreign-emission check at all.
    """


class RestampScriptNotFoundError(NoReceiptToRestampError):
    """Raised when ``restamp``'s ``script_path`` itself does not exist.

    Distinct from the base class's "script exists
    but has no receipt beside it" case (typo'd path vs. a genuinely
    un-emitted script); a subclass so an existing ``except
    NoReceiptToRestampError`` still catches this, while a caller that cares
    about the distinction can catch this subclass first.
    """


class RestampParseError(ValueError):
    """Raised when ``restamp`` is asked to stamp a script that does not parse.

    A stamp vouches for bytes about to fire; stamping a broken edit moves the
    failure to the Workflow fire, after the run is already claimed."""


def restamp(script_path: Path, session_id: str) -> dict:
    """Re-stamp an emission receipt's ``sha256`` over a script THIS session
    deliberately edited after emission.

    Mirrors coordinator-content-repo's wrapper CLI ``emit-dispatch-workflow.py :: restamp``
    (the other producer of this same receipt shape, § The receipt is a
    property of emitting, not of one repo's wrapper, above) -- same refusal
    shape, same serialisation. The published surface documents
    ``--restamp <script>`` in three places; this is the engine-owned function
    that surface delegates to once it becomes a thin door-served CLI (S1-C7).

    Refuses unless the existing receipt already names ``session_id`` as the
    emitter -- a peer's emission cannot be laundered through it, and this is
    an explicit operator act naming the script, never something the fire
    path invokes on its own. What it drops is only the guarantee that the
    bytes are the emitter's verbatim output, which is exactly what the edit
    that necessitated the restamp already gave up.

    Returns the receipt dict as written to disk.

    Negative-spec:
      - Does NOT accept a raw, unguarded path. The caller (the future
        registered op wiring this up) is responsible for path-guarding
        ``script_path`` the same way ``_dispatch_emit`` guards
        ``output_path`` -- this function trusts the path it is given, same
        as ``emission_receipt_path``.
      - Does NOT resolve session identity itself. The caller supplies
        ``session_id`` (typically via ``_receipt_session_id``/
        ``resolve_session_id``), so this stays a pure refuse-or-rewrite
        function over an already-resolved identity -- no second private
        session-resolution ladder.
      - Does NOT re-derive or widen the receipt's key set. Only ``sha256``
        changes; every other key (``session_id``, ``emitted_at``, ``plan``)
        is carried over verbatim from the existing receipt.
    """
    if not script_path.is_file():
        raise RestampScriptNotFoundError(f"script not found: {script_path}")

    receipt_path = emission_receipt_path(script_path)
    if not receipt_path.is_file():
        raise NoReceiptToRestampError(
            f"no emission receipt beside {script_path.name} -- nothing to "
            "restamp. A script with no receipt is not gated by the "
            "foreign-emission check at all (it fails open on an absent "
            "receipt), so if a fire is being refused, something else is "
            "refusing it."
        )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    recorded = receipt.get("session_id")
    if not session_id or recorded != session_id:
        raise ForeignSessionRestampError(
            f"{receipt_path.name} names session {str(recorded)[:8]} as the "
            f"emitter; this session is {str(session_id)[:8]} -- refusing to "
            "restamp. This route re-stamps YOUR OWN deliberate edit, never a "
            "peer's emission: restamping theirs would run their wave map "
            "under your handle with the one guard that notices switched "
            "off. Coordinate with that session instead."
        )

    from coordinator_core.ops._workflow_contract import check_structure

    broken = check_structure(script_path.read_text(encoding="utf-8"))
    if broken:
        f = broken[0]
        raise RestampParseError(
            f"{script_path.name} line {f.line}: {f.message} ({f.code}) -- refusing to "
            "restamp bytes that will not parse. Fix the edit and restamp again."
        )

    receipt["sha256"] = _script_sha256(script_path)
    atomic_write_bytes(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return receipt


_PARAM_FIELDS = (
    *(
        Field(name, "str")
        for name in (
            "plan_path", "plan", "inventory_path", "profile", "profile_dir", "sizing_path",
            "output_path", "target_root", "preamble", "preamble_path", "preamble_sha256", "box_terms_path",
            "inventory_repo_root", "from_sizing", "pipeline", "brief", "scratch_dir", "validator", "part", "baton", "deliverable_id",
        )
    ),
    Field("inventory_part", "list"),
    Field("row_budget", "pos_int"),
    Field("queue", "list"),
    Field("overrides", "dict"),
    Field("stage_models", "dict"),
    Field("writes", "str_list"),
    Field("review_only_rows", "str_list"),
    Field("review_carried_paths", "str_list"),
    Field("context", "str_list"),
    Field("ask_brief", "str"),
    Field("ask_brief_file", "str"),
    Field("stop_after", "str"),
    Field("ask_source", "str"),
    Field("session_root", "str"),
    Field("route_preference", "str"),
    Field("em_rationale", "str"),
    Field("ask_context", "str_list"),
    Field("all_of_pm_ask", "bool"),
    Field("required_reviewers", "str_list"),
    Field("hold_rows", "str_list"),
    Field("hold_reason", "str"),
    Field("fanout_group", "str"),
    Field("box_terms", "str_list"),
    Field("run_base_sha", "str"),
    Field("flags", "dict"),
    Field("lists", "dict"),
    Field("per_group", "bool"),
    Field("group_refs", "str_list"),
)


_STAGE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validated_stage_models(value: Any) -> Optional[dict]:
    """The queue route's ``stage_models`` mapping, or ValueError naming the bad key and the valid ones."""
    if not value:
        return None
    # Trap: deferral saves nothing while any module-level import in this package still reaches grind_stages.
    from coordinator_core.ops.dispatch_emit.grind_stages import STAGE_MODELS

    valid = sorted(STAGE_MODELS)
    for kind, model in value.items():
        if kind not in STAGE_MODELS:
            raise ValueError(f"stage_models key {kind!r} is not a stage; valid keys: {valid}")
        if not isinstance(model, str) or not _STAGE_MODEL_RE.match(model):
            raise ValueError(
                f"stage_models[{kind!r}] = {model!r} is not a model name matching {_STAGE_MODEL_RE.pattern}"
            )
    return dict(value)


def _pipeline_content_root() -> Path:
    """Content root the pipeline manifests live under; refuses, never defaults."""
    root = read_content_root()
    if not root:
        raise PipelineEmitRefused(
            ["content root unresolved: read_content_root() returned empty"]
        )
    content_root = content_root_for(root)
    if content_root is None:
        raise PipelineEmitRefused(
            [f"{Path(root).as_posix()} is neither a private clone (no coordinator/) nor a flat mirror"]
        )
    return Path(content_root)


def _local_only_root(params: dict, research: dict) -> Optional[Path]:
    """The validated machine-local root for a local-only research run, else None. The sizing's
    ``research.destination: local-only`` sets the mode, so a fire from the sizing cannot drop
    it; the caller's ``local_only`` turns it on for a sizing that does not."""
    if not (params.get("local_only") or research.get("destination") == "local-only"):
        return None
    from coordinator_core.ops import _research_local

    try:
        return _research_local.local_root()
    except _research_local.LocalRootError as exc:
        raise PipelineEmitRefused([str(exc)]) from exc


def _local_only_scratch(local_root: Path, scratch_dir: Optional[str], run_id: str) -> str:
    """Absolute POSIX scratch dir under the local root: the caller's, or ``<root>/runs/<run_id>``.
    Absolute, so every ``root / scratch`` join in the composer lands outside the repo."""
    from coordinator_core.ops import _research_local

    given = Path(scratch_dir) if scratch_dir else Path("runs") / run_id
    try:
        scratch = _research_local.under_local_root(given, local_root, "scratch_dir")
    except _research_local.LocalRootError as exc:
        raise PipelineEmitRefused([str(exc)]) from exc
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch.as_posix()


def _pipeline_scratch_rel(root: Path, scratch_dir: Optional[str], run_id: str) -> str:
    """Repo-relative POSIX scratch dir: the caller's, guarded under ``root``, or the per-run default."""
    if not scratch_dir:
        return f"{RUN_DIR_ROOT}/{run_id}"
    given = Path(scratch_dir)
    guarded = contained_path(given if given.is_absolute() else Path(root) / given, [Path(root)])
    if guarded is None:
        raise PathEscapeError(f"scratch_dir escapes repo root: {scratch_dir!r} not under {Path(root).as_posix()!r}")
    return guarded.relative_to(Path(root).resolve()).as_posix()


def _output_landed(root: Path, rel: str) -> bool:
    """True when ``rel`` (repo-relative) is a non-empty regular file under ``root``.

    One ``stat``, no listing. A directory never counts: a half-written
    per-item directory would read as done, and re-reading is the safe error.
    """
    guarded = contained_path(Path(root) / rel, [Path(root)])
    if guarded is None:
        return False
    try:
        st = os.stat(guarded)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


def _resume_missing_inputs(manifest, schedule, inputs: PipelineInputs, root: Path):
    """Drop every input-list element whose output file already landed.

    An element is done only when, for every active stage fanned ``over:
    inputs.<list>``, the stage's filled ``output`` exists for it (for every
    subject when the stage is per-subject); a list shared by two stages
    re-emits an element either stage still lacks. Returns ``(inputs, report)``
    with ``report[list] = {expected, present, missing}``; refuses when no
    stage fans over an input list or nothing is missing.
    """
    from coordinator_core.ops.dispatch_emit.pipeline_compose import _element_value, _fill
    from coordinator_core.ops.dispatch_emit.pipeline_contract import SCOPE_SUBJECT, subject_slug

    stages = {s.id: s for s in manifest.stages}
    done: dict[str, list[bool]] = {}
    for stage in manifest.stages:
        if stage.id in schedule.skipped or not stage.fan_out.over_inputs:
            continue
        name = stage.fan_out.inputs
        elements = inputs.lists[name]
        flags = done.setdefault(name, [True] * len(elements))
        subjects = (
            [subject_key(s) for s in inputs.subjects] if schedule.scope[stage.id] == SCOPE_SUBJECT else [None]
        )
        for subject in subjects:
            scratch = (
                f"{inputs.scratch_dir.rstrip('/')}/{subject_slug(subject)}"
                if manifest.subjects_mode and subject is not None else None
            )
            for index, element in enumerate(elements):
                errors: list[str] = []
                rel = _fill(
                    stage.output, manifest=manifest, inputs=inputs, stages=stages, subject=subject,
                    errors=errors, item=_element_value(element), scratch=scratch,
                )
                flags[index] = flags[index] and not errors and _output_landed(root, rel)
    if not done:
        raise PipelineEmitRefused(["resume_missing needs a stage fanned over an input list (over: inputs.<list>)"])
    report = {
        name: {"expected": len(flags), "present": sum(flags), "missing": len(flags) - sum(flags)}
        for name, flags in done.items()
    }
    if all(entry["missing"] == 0 for entry in report.values()):
        raise PipelineEmitRefused(["resume_missing: every expected output already exists; nothing to re-emit"])
    lists = {
        name: tuple(e for e, ok in zip(inputs.lists[name], done[name]) if not ok) if name in done else inputs.lists[name]
        for name in inputs.lists
    }
    return replace(inputs, lists=lists), report


def _em_mailbox(root: Path, scratch_rel: str) -> dict:
    """Create the run's EM mailbox (empty) and name the background commands that watch it.

    Members append escalations there mid-run (`chatty.EM_MAILBOX`); nothing else reads it.
    Each command blocks until one new line arrives, prints it and exits, so it runs as a
    background Bash run (no Monitor timeout). `em_watch` reads from the first line;
    `em_rewatch` starts at the current end, so a re-armed watch never re-delivers a line
    it already saw.
    """
    from coordinator_core.ops.dispatch_emit.chatty import EM_MAILBOX

    path = root / scratch_rel / "mail" / EM_MAILBOX
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return {
        "em_mailbox": path.as_posix(),
        "em_watch": f"python -m coordinator_core.workflow_watch.mailbox_line --from-start {shlex.quote(path.as_posix())}",
        "em_rewatch": f"python -m coordinator_core.workflow_watch.mailbox_line {shlex.quote(path.as_posix())}",
    }


#: A research manifest fired by name (`--pipeline`) closes like the research route's segment.
_LEGACY_RESEARCH_TIER = {"scouts": "scouts", "notebooklm": "corpus", "unblock": "deep"}


def _sizing_scout_mode(research: dict, given_mode: Optional[str]) -> Optional[str]:
    """The sizing's `question_mode`, refused when an explicit `scout_mode` names another or it is not a mode."""
    from coordinator_core.ops import _research_contract as rc

    sized = research.get(rc.QUESTION_MODE_KEY)
    if not sized:
        return None
    if sized not in rc.SCOUT_MODES:
        raise PipelineEmitRefused([f"sizing {rc.QUESTION_MODE_KEY} {sized!r} is not one of {', '.join(rc.SCOUT_MODES)}"])
    if given_mode and given_mode != sized:
        raise PipelineEmitRefused(
            [f"{rc.SCOUT_MODE_PARAM} {given_mode!r} differs from the sizing's {rc.QUESTION_MODE_KEY} {sized!r}; drop one"]
        )
    return sized


def _sizing_scout_questions(research: dict) -> list:
    """`research.questions` as scout questions: each gains the `question_targets` entries numbered for it (a question with none is web).

    Refuses an entry whose question number is outside 1..N, or any entry when there are no questions.
    """
    from coordinator_core.ops import _research_contract as rc

    texts = [str(q) for q in research.get("questions") or ()]
    raw_entries = list(research.get(rc.QUESTION_TARGETS_KEY) or ())
    reasons = [f"{rc.QUESTION_TARGETS_KEY} entry {e!r} is not a mapping" for e in raw_entries if not isinstance(e, dict)]
    entries = [e for e in raw_entries if isinstance(e, dict)]
    reasons += [
        f"{rc.QUESTION_TARGETS_KEY} entry names question {e.get('question')!r}, outside 1..{len(texts)}"
        for e in entries
        if not (type(e.get("question")) is int and 1 <= e["question"] <= len(texts))
    ]
    reasons += [
        f"{rc.QUESTION_TARGETS_KEY} entry {e!r} has no string source" for e in entries if not isinstance(e.get("source"), str)
    ]
    if reasons:
        raise PipelineEmitRefused(reasons)
    questions: list = []
    for n, text in enumerate(texts, 1):
        mine = [{"target": e["source"], "ref": e.get("ref", "")} for e in entries if e["question"] == n]
        questions.append({"text": text, "targets": mine} if mine else text)
    return questions


def _shape_or_refuse(research: dict) -> dict:
    """`research_shape.shape`, its ValueError (spike, unknown class) surfaced as a pre-write refusal."""
    from coordinator_core.ops import research_shape

    try:
        return research_shape.shape(research)
    except ValueError as exc:
        raise PipelineEmitRefused([str(exc)]) from exc


def _research_route_setup(
    params: dict,
    repo_root: Optional[Path],
    from_sizing: Optional[str],
    ask: object,
    group: Optional[dict] = None,
    manifests: Optional[dict] = None,
) -> tuple[dict, dict, dict]:
    """Resolve the research route: shape, scratch dir, ask file, loaded and validated segments.

    Returns ``(research_ctx, pipeline_ctx, params)``; ``pipeline_ctx`` carries the first
    segment's inputs so the pipeline route's scratch-root declaration and reply fields apply
    unchanged, and ``params`` gains the default ``output_path``. ``from_sizing`` takes its
    research block from the sizing and binds the sizing file as the brief; an ask writes
    ``<scratch>/ask.md`` and binds that; ``context`` files are named in the bound brief
    (``research_emit.bind_context``). ``lists['questions']`` are the scout questions; any other
    given list or ``flags`` entry overrides the segment whose manifest declares it. Every
    segment's refusals are raised together, prefixed by pipeline.

    ``group`` (``{"ref", "topic_slug"}``, per-group fan-out) narrows the sizing's notebooklm targets
    to that one ref, digests it into the run id and fixes the close topic; ``manifests`` is a
    pipeline-keyed cache shared across the groups of one emit.
    """
    from coordinator_core.ops import research_shape
    from coordinator_core.ops.dispatch_emit import research_emit
    from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    from coordinator_core.ops import _research_contract as rc

    given_topic = params.get("topic_slug")
    if given_topic is not None:
        slug_refusal = research_emit.given_slug_refusal(given_topic)
        if slug_refusal:
            raise PipelineEmitRefused([slug_refusal])
    if group:
        given_topic = group["topic_slug"]
    lists = dict(params.get("lists") or {})
    given_mode = params.get(rc.SCOUT_MODE_PARAM)
    mode = given_mode or rc.DEFAULT_SCOUT_MODE
    if mode not in rc.SCOUT_MODES:
        raise PipelineEmitRefused([f"{rc.SCOUT_MODE_PARAM} {mode!r} is not one of {', '.join(rc.SCOUT_MODES)}"])
    raw_questions = params.get(rc.SCOUT_QUESTIONS_PARAM)
    if raw_questions:
        mixed = []
        if from_sizing:
            mixed.append(f"{rc.SCOUT_QUESTIONS_PARAM} cannot combine with from_sizing; the sizing's research block carries its questions")
        if lists.get("questions"):
            mixed.append(f"{rc.SCOUT_QUESTIONS_PARAM} cannot combine with lists.questions; pass one or the other")
        if mixed:
            raise PipelineEmitRefused(mixed)
        if isinstance(raw_questions, (str, bytes)) or not isinstance(raw_questions, (list, tuple)):
            raise PipelineEmitRefused([f"{rc.SCOUT_QUESTIONS_PARAM} must be a list of questions"])
        questions = list(raw_questions)
    else:
        questions = [str(q) for q in lists.get("questions") or ()]
    sizing_rel = ""
    if from_sizing:
        root = _sizing_root(params, repo_root, str(from_sizing))
        brief_rel = sizing_rel = normalize_sizing_rel(root, str(from_sizing))
        research = load_sizing(root, brief_rel).get("research")
        if not isinstance(research, dict) or not research:
            raise PipelineEmitRefused([f"sizing {brief_rel} has no research block; set one with sizing-assemble --research-class"])
        if group:
            kept, taken = [], False
            for t in research.get("targets") or ():
                if isinstance(t, dict) and t.get("source") == "notebooklm":
                    if taken or t.get("ref") != group["ref"]:
                        continue
                    taken = True
                kept.append(t)
            research = {**research, "targets": kept}
        mode = _sizing_scout_mode(research, given_mode) or mode
        if not questions:
            questions = _sizing_scout_questions(research)
    else:
        given_root = repo_root or params.get("target_root")
        if not given_root:
            raise ValueError("dispatch.emit research route requires repo_root or target_root")
        root = Path(given_root)
        research = {"value_class": "scouts", "appetite": "medium"}
        brief_rel = ""
    allowlist = (
        research_emit.probe_allowlist(_pipeline_content_root())
        if any(research_emit._has_local_target(q) for q in questions)
        else None
    )
    questions = research_emit.normalize_scout_questions(root, questions, allowlist)
    shape = _shape_or_refuse(research)
    digest_parts = {
        "brief": brief_rel or str(ask), "research": research, "lists": lists,
        "flags": params.get("flags") or {}, "context": [str(c) for c in params.get("context") or ()],
    }
    if group:
        digest_parts["group"] = group["ref"]
    if raw_questions or from_sizing or mode != rc.DEFAULT_SCOUT_MODE:
        digest_parts["scout_questions"] = questions
        digest_parts["scout_mode"] = mode
    digest = research_emit.ask_digest(root, digest_parts)
    run_id = research_emit.run_id_for(
        root, RUN_DIR_ROOT, f"{RUN_ID_PREFIX}research-", digest, datetime.now().strftime("%Y%m%dT%H%M%S")
    )
    local_root = _local_only_root(params, research)
    if local_root is None:
        scratch_rel = _pipeline_scratch_rel(root, params.get("scratch_dir"), run_id)
    else:
        scratch_rel = _local_only_scratch(local_root, params.get("scratch_dir"), run_id)
    if not from_sizing or (questions and not lists.get("questions")):
        ask_text = str(ask or "")
        if (raw_questions or from_sizing) and not ask_text.strip():
            ask_text = questions[0]["text"]
        brief_rel = research_emit.write_ask(root, scratch_rel, ask_text, questions, mode, allowlist, research)
    members_brief = research_emit.bind_context(
        root, scratch_rel, brief_rel, [str(c) for c in params.get("context") or ()]
    )
    content_root = _pipeline_content_root()
    targets = [t for t in research.get("targets") or () if isinstance(t, dict)]
    repo_segments: list[tuple[str, str]] = []
    compare_brief = ""
    multi_repo = ""
    seeds = [s for s in research.get("seeds") or () if isinstance(s, dict)]
    seed_briefs = research_emit.bind_seed_briefs(root, scratch_rel, members_brief, seeds)
    repo_seeds = [s for s in seeds if rc.SEED_KIND_SOURCE.get(s.get("kind")) == research_emit.REPO_PIPELINE]
    repo_refs = sum(1 for t in targets if t.get("source") == research_emit.REPO_PIPELINE) + len(repo_seeds)
    if research_emit.REPO_PIPELINE in shape["pipelines"] and repo_refs == 0 and (targets or questions):
        raise PipelineEmitRefused([
            f"sizing {sizing_rel or '(research route)'}: the repo pipeline has no resolved repo target; "
            "record one with sizing-assemble --research-target repo=<path> or a repo seed"
        ])
    if research_emit.REPO_PIPELINE in shape["pipelines"] and repo_refs >= (1 if repo_seeds else 2):
        compare_name = research_emit.REPO_COMPARE_PIPELINE + MANIFEST_SUFFIX
        if repo_refs < 2 or any(p.is_file() for p in (content_root / PIPELINES_DIR).rglob(compare_name)):
            repo_segments, compare_brief = research_emit.bind_repo_targets(
                root, scratch_rel, members_brief, targets, seeds
            )
        else:
            multi_repo = "unavailable: no repo-compare manifest in the content root"
    pairs = research_emit.segments_for(
        shape,
        brief_rel=members_brief,
        scratch_rel=scratch_rel,
        questions=questions,
        sources=research.get("sources") or (),
        targets=targets,
        mode=mode,
        repo_segments=repo_segments,
        compare_brief=compare_brief,
        seed_briefs=seed_briefs,
    )
    given_flags = {str(k): str(v) for k, v in (params.get("flags") or {}).items()}
    segments = []
    reasons: list[str] = []
    claimed: set[str] = set()
    for pipeline, inputs in pairs:
        if manifests is None:
            manifest = load_manifest(content_root, pipeline)
        else:
            manifest = manifests.get(pipeline) or manifests.setdefault(pipeline, load_manifest(content_root, pipeline))
        if pipeline == "notebooklm":
            try:
                manifest = research_emit.gate_notebooklm(manifest)
            except PipelineEmitRefused as exc:
                reasons.extend(f"pipeline {pipeline!r}: {r}" for r in exc.reasons)
                continue
        if inputs.lists.get(rc.PROBES_LIST) and rc.PROBES_LIST not in manifest.lists:
            reasons.append(
                f"pipeline {pipeline!r}: a local target needs the {rc.PROBES_LIST} list, which this manifest does not declare"
            )
            continue
        inputs = replace(
            inputs,
            lists={k: v for k, v in inputs.lists.items() if k != rc.PROBES_LIST or k in manifest.lists},
            flags={k: v for k, v in inputs.flags.items() if k != rc.LOCAL_PROBE_FLAG or k in manifest.flags},
        )
        defaults = research_emit.corpus_default_lists(
            pipeline, [q["text"] if isinstance(q, dict) else q for q in questions]
        )
        given = {
            **{k: v for k, v in defaults.items() if k in manifest.lists and k not in inputs.lists},
            **{k: tuple(v) for k, v in lists.items() if k in manifest.lists and k != "questions"},
        }
        flags = {**inputs.flags, **{k: v for k, v in given_flags.items() if k in manifest.flags}}
        claimed.update(k for k in given_flags if k in manifest.flags)
        inputs = replace(
            inputs, flags=flags, lists=normalize_lists(manifest.lists, {**inputs.lists, **given})
        )
        key_refusals = research_emit.list_key_refusals(pipeline, inputs.lists)
        if key_refusals:
            reasons.extend(f"pipeline {pipeline!r}: {r}" for r in key_refusals)
            continue
        if "notebooks" in manifest.lists and not inputs.lists.get("notebooks") and pipeline not in seed_briefs:
            reasons.append(
                f"pipeline {pipeline!r} needs its notebooks: record them on the sizing with "
                "sizing-assemble --research-target notebooklm=<letter or id>, or pass --list notebooks=A,B"
            )
            continue
        try:
            segments.append((manifest, inputs, validate(manifest, inputs)))
        except PipelineEmitRefused as exc:
            reasons.extend(f"pipeline {pipeline!r}: {r}" for r in exc.reasons)
    reasons.extend(
        f"--flag {name} is declared by none of this run's pipelines ({', '.join(shape['pipelines'])})"
        for name in given_flags if name not in claimed
    )
    if reasons:
        raise PipelineEmitRefused(reasons)
    if not aliased_param(params, "output_path", "out_path"):
        params = {**params, "output_path": str(root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs")}
    close_params = {
        "scratch_dir": (root / scratch_rel).as_posix(),
        "tier": shape["tier"],
        "run_id": run_id,
        "topic_slug": given_topic
        or research_emit.topic_slug(str(ask or ""), sizing_rel),
        "value_class": str(research["value_class"]),
    }
    if from_sizing:
        close_params["sizing"] = sizing_rel
    if local_root is not None:
        close_params["local_root"] = local_root.as_posix()
    for index, (manifest, _, _) in enumerate(segments):
        if manifest.pipeline == "notebooklm":
            claims = research_emit.claims_output(manifest)
            close_params["require_claims"] = [claims if index == 0 else f"notebooklm/{claims}"]
    if any(
        m.pipeline == rc.SCOUTS_PIPELINE
        and any(PurePosixPath(st.output).name == GROUNDING_REPORT for st in m.stages)
        for m, _, _ in segments
    ):
        close_params["require_grounding"] = True
    # pipeline_compose nests a segment under <scratch>/<segment name>/ when it is keyed or not first.
    nested = [segment_name(m, i) for index, (m, i, _) in enumerate(segments) if index > 0 or i.segment_key]
    if nested:
        close_params["segment_dirs"] = nested
    if compare_brief:
        compare_manifest = next(m for m, _, _ in segments if m.pipeline == research_emit.REPO_COMPARE_PIPELINE)
        close_params["require_outputs"] = [
            f"{research_emit.REPO_COMPARE_PIPELINE}/{research_emit.compare_output(compare_manifest)}"
        ]
    scout_targets: list[dict] = []
    if any(m.pipeline == rc.SCOUTS_PIPELINE for m, _, _ in segments):
        slugs = [rc.COMBINED_SCOUT_ITEM] * len(questions) if mode == "combine" else research_emit.scout_slugs(questions)
        scout_targets = [{"slug": s, "targets": q["targets"]} for s, q in zip(slugs, questions)]
    research_ctx = {
        "root": root,
        "run_id": run_id,
        "brief": brief_rel,
        "segments": segments,
        "shape": {
            "tier": shape["tier"],
            "reason": shape["reason"],
            "pipelines": shape["pipelines"],
            "scout_mode": mode,
            "scout_targets": scout_targets,
            "segments": [
                {
                    "pipeline": m.pipeline, "segment": segment_name(m, i),
                    "flags": dict(i.flags), "lists": {k: list(v) for k, v in i.lists.items()},
                }
                for m, i, _ in segments
            ],
        },
        "multi_repo": multi_repo,
        "next_action": {"op": "research.close", "params": close_params},
        "legs": research_emit.halt_legs([m.pipeline for m, _, _ in segments]),
    }
    pipeline_ctx = {"root": root, "run_id": run_id, "resume_missing": False, "inputs": segments[0][1]}
    return research_ctx, pipeline_ctx, params


def _emit_per_group(params: dict, repo_root: Optional[Path], from_sizing: object, emit: Callable) -> dict:
    """One single-group research emit per notebooklm target of the sizing; the reply is the fan-out plan.

    Every group of the sizing is set up first so each close names all sibling run ids (a ``group_refs``
    subset still closes against the full set, so the research block survives until every group closes),
    then the selected groups are emitted through ``emit`` (the op itself) with their prepared route
    context. Refusals are raised together.
    """
    from coordinator_core.ops import research_shape
    from coordinator_core.ops.dispatch_emit import research_emit
    from coordinator_core.ops.dispatch_emit.research_groups import (
        GROUP_RUNS_CLOSE_PARAM, GROUPS_PARAM, GROUPS_REPLY_KEY, PER_GROUP_PARAM, groups_for, per_group_refusals,
    )
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    if not from_sizing:
        raise ValueError(f"dispatch.emit {PER_GROUP_PARAM} needs from_sizing")
    if aliased_param(params, "output_path", "out_path") or params.get("research"):
        raise ValueError(f"dispatch.emit {PER_GROUP_PARAM} derives each script path and takes no output_path or research")
    given_topic = params.get("topic_slug")
    if given_topic is not None:
        slug_refusal = research_emit.given_slug_refusal(given_topic)
        if slug_refusal:
            raise PipelineEmitRefused([slug_refusal])
    root = _sizing_root(params, repo_root, str(from_sizing))
    sizing_rel = normalize_sizing_rel(root, str(from_sizing))
    research = load_sizing(root, sizing_rel).get("research")
    if not isinstance(research, dict) or not research:
        raise PipelineEmitRefused([f"sizing {sizing_rel} has no research block; set one with sizing-assemble --research-class"])
    selected = tuple(params.get(GROUPS_PARAM) or ())
    reasons = per_group_refusals(_shape_or_refuse(research), research, selected)
    if reasons:
        raise PipelineEmitRefused(reasons)
    specs = groups_for(research, given_topic or research_emit.topic_slug("", sizing_rel))
    base = {k: v for k, v in params.items() if k not in (PER_GROUP_PARAM, GROUPS_PARAM, "topic_slug")}
    manifests: dict = {}
    prepared = [
        _research_route_setup(
            base, repo_root, from_sizing, None,
            group={"ref": s.ref, "topic_slug": s.topic_slug}, manifests=manifests,
        )
        for s in specs
    ]
    run_ids = [ctx["run_id"] for ctx, _, _ in prepared]
    entries = []
    for spec, ready in zip(specs, prepared):
        if selected and spec.ref not in selected:
            continue
        ready[0]["next_action"]["params"][GROUP_RUNS_CLOSE_PARAM] = run_ids
        sub = emit(ready[2], repo_root, _research_ready=ready)
        if "path" not in sub:
            return sub
        entries.append((spec, sub))
    first = entries[0][1]
    return {
        **first,
        PER_GROUP_PARAM: True,
        "ok": all(sub["ok"] for _, sub in entries),
        GROUPS_REPLY_KEY: [
            {
                "group": spec.group, "ref": spec.ref, "priority": spec.priority, "run_id": sub["run_id"],
                "path": sub["path"], "sha256": sub["sha256"], "receipt": sub["receipt"],
                "fanout": sub.get("fanout"), "next_action": sub["next_action"],
            }
            for spec, sub in entries
        ],
    }


def _account_inventory(
    spine_path,
    item_ids: list,
    ledger: dict,
    *,
    landed_rows=None,
    sibling=None,
) -> dict:
    """The `inventory_accounting` reply: every Chunk-table item in exactly one of
    emitted, skipped (mint ledger, landed rows, other sibling) or refused
    (`read_spine` exclusions). Raises `InventoryAccountingError` on a gap, so no
    script ships with an unaccounted row. Rows are read the way `emit_script`
    reads them; trailer-landed rows are not subtracted."""
    from coordinator_core.ops.dispatch_emit.inventory_accounting import IN_ANOTHER_PART, account
    from coordinator_core.ops.dispatch_emit.inventory_mint import _row_item_ids
    from coordinator_core.ops.dispatch_emit.spine_read import read_spine

    exclusions: list = []
    rows = read_spine(spine_path, exclusions=exclusions)
    owner = dict(zip((r.id for r in rows), _row_item_ids([{"id": r.id} for r in rows], item_ids)))
    landed = frozenset(landed_rows or ())
    keep = frozenset(sibling["keep_rows"]) if sibling else None

    emitted: dict = {}
    skipped: dict = dict(ledger)
    spine_items: dict = {}
    for r in rows:
        spine_items.setdefault(owner[r.id], []).append(r.id)
    for item, ids in spine_items.items():
        live = [i for i in ids if i not in landed and (keep is None or i in keep)]
        if live:
            emitted[item] = True
        elif keep is not None and not any(i in landed for i in ids):
            skipped[item] = IN_ANOTHER_PART
        else:
            skipped[item] = "rows already landed (landed_rows)"
    refused: dict = {}
    if exclusions:
        ex_ids = [{"id": str(e["id"])} for e in exclusions]
        for e, item in zip(exclusions, _row_item_ids(ex_ids, item_ids)):
            if item not in emitted and item not in skipped:
                refused.setdefault(item, []).append(f"{e['id']} ({e['reason']}: {e['detail']})")
    return account(item_ids, emitted, skipped, {i: "; ".join(why) for i, why in refused.items()})


def _refuse_inventory_outside_repo(inventory_path: str, repo_root) -> None:
    """A plan row's `spec path` resolves against the inventory's own
    `<repo>/state/mise-inventory/` grandparent; an inventory anywhere else
    expands no plan and emits a script of one-line stubs. A warp run's JSON manifest is not an
    inventory record at all (it has no chunk table), so its refusal names the plan route."""
    inventory = Path(inventory_path).resolve()
    root = Path(repo_root).resolve() if repo_root else _repo_root_for_plan(str(inventory))
    if root is None:
        return
    inventory_home = Path(home_dir(str(root), "mise-inventory"))
    if inventory.name == "manifest.json" and inventory.parent.parent == Path(home_dir(str(root), "warp")):
        raise InventoryOutsideRepoError(
            f"{inventory_path!r} is a warp run manifest, not an inventory record. "
            "Review that run's rows with --plan <plan.md> --review-only --rows <ids> --run-base <sha>."
        )
    if inventory.parent != inventory_home:
        raise InventoryOutsideRepoError(
            f"inventory {inventory_path!r} is not under {inventory_home}; "
            "plan specs resolve against that directory's repo, so this emission "
            "would carry no plan chunks. Move the record there, or pass --repo-root."
        )


def _emit_script_file(
    path: Path,
    script: str,
    *,
    receipt_plan_path: Optional[str],
    params: dict,
    receipt_extras: Optional[dict],
    plan_findings: list,
    session_id: str,
    agent_type_host: Optional[str],
    before_write: Callable[[], None],
) -> dict:
    """Check, write and receipt one emitted script; returns its path, sha256, receipt and findings."""
    check_agent_types_resolve(
        script,
        claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT"),
        agent_type_host=agent_type_host,
    )
    findings = [*run_checks(script), *plan_findings]
    # newline="" suppresses the platform line-ending translation: a CRLF-carrying .mjs is
    # rejected by the Workflow surface that fires it.
    _refuse_spent_overwrite(path, script.encode("utf-8"), path)
    if not params.get("force"):
        _refuse_foreign_emission(path, script, session_id, _repo_root_for_plan(str(path)))
    before_write()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8", newline="")
    # Best-effort like the receipt: a failed digest degrades sha256 to None, never fails the emit.
    try:
        script_sha256 = _script_sha256(path)
    except OSError as exc:
        print(
            f"dispatch.emit: wrote {path} but could not digest it "
            f"({exc}). The script stands; its reply carries no sha256, and "
            "a --fire/workflow.fire caller cannot drift-guard this emission.",
            file=sys.stderr,
        )
        script_sha256 = None
    finding_dicts = [
        {
            "severity": f.severity.value,
            "code": f.code,
            "message": f.message,
            **({"line": f.line} if f.line is not None else {}),
        }
        for f in findings
    ]
    receipt = _write_emission_receipt(
        path,
        receipt_plan_path,
        params,
        extras={**(receipt_extras or {}), "findings": finding_dicts},
        sha256=script_sha256,
    )
    return {
        "path": path,
        "sha256": script_sha256,
        "receipt": receipt,
        "findings": finding_dicts,
        "error_count": sum(1 for f in findings if f.severity is Severity.ERROR),
        "warn_count": sum(1 for f in findings if f.severity is Severity.WARN),
    }


class _EmitWrites:
    """The files one inventory emit wrote, with what each held before, so a refused emit
    restores the tree: a created file is removed, an overwritten one gets its bytes back."""

    def __init__(self) -> None:
        self._prior: dict = {}
        self._made_dirs: list = []

    def write(self, path: Path, text: str) -> None:
        if path not in self._prior:
            self._prior[path] = path.read_bytes() if path.is_file() else None
        missing = [d for d in reversed(path.parents) if not d.exists()]
        path.parent.mkdir(parents=True, exist_ok=True)
        self._made_dirs.extend(missing)
        path.write_text(text, encoding="utf-8", newline="\n")

    def rollback(self) -> None:
        for path, prior in self._prior.items():
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(prior)
        for made in reversed(self._made_dirs):
            try:
                made.rmdir()
            except OSError:
                pass


@register_op("dispatch.emit")
def _dispatch_emit(
    params: dict,
    repo_root: Optional[Path] = None,
    *,
    only_review_plans: Optional[frozenset] = None,
    _research_ready: Optional[tuple] = None,
) -> dict:
    writes = _EmitWrites()
    try:
        return _dispatch_emit_body(
            params,
            repo_root,
            only_review_plans=only_review_plans,
            _research_ready=_research_ready,
            _writes=writes,
        )
    except ScriptOverCapError:
        # The over-cap re-entry cuts parts from the tranche record this emit minted.
        raise
    except BaseException:
        writes.rollback()
        raise


def _dispatch_emit_body(
    params: dict,
    repo_root: Optional[Path] = None,
    *,
    only_review_plans: Optional[frozenset] = None,
    _research_ready: Optional[tuple] = None,
    _writes: _EmitWrites,
) -> dict:
    """JSON-RPC "dispatch.emit" handler.

    ``_writes`` ledgers every file the inventory route writes before the script emit; the
    caller rolls it back when the emit refuses.

    ``_research_ready`` is the per-group seam's own (never a request param): a research route
    context already set up by ``_emit_per_group``.

    ``per_group`` (bool, with ``from_sizing``; optional ``group_refs`` subset): one independent
    single-notebook research emit per notebooklm target in sizing order, replied as
    ``research_groups`` plus ``per_group: true``.

    ``only_review_plans`` is the lanes flow's own seam (never a request
    param): when not ``None``, an ``--inventory`` emission reviews only those
    plans' falsifiers, the rest being reviewed by the part that owns them.

    Args (via params):
        plan_path (str): plan file to read the task spine from. Mutually
            exclusive with ``inventory_path`` (see
            ``InventoryPathConflictError``) and with ``queue``/``profile``
            (see ``QueuePlanConflictError``).
        inventory_path (str, optional): a mise-inventory record to mint a
            spine FROM first (``inventory_mint.mint_spine``), written to
            ``<run-id>.spine.md`` beside the record, then emitted exactly
            as a hand-authored ``plan_path`` would be. Mutually exclusive
            with ``plan_path``.
        queue (list[str], optional): one or more queue directories -- the
            QUEUE route. Present together with ``profile`` instead of
            ``plan_path``/``inventory_path``; mutually exclusive with them
            (``QueuePlanConflictError``). Forwarded to
            ``queue_emit.emit_queue_script``.
        profile (str, optional): the queue-grind profile name -- required
            alongside ``queue`` for the queue route.
        profile_dir (str, optional): directory ``<profile>.yaml`` lives
            under -- required alongside ``queue``/``profile``.
        appetite (str, optional, default "standard"): forwarded to
            ``queue_emit.emit_queue_script``.
        overrides (dict, optional): knob overrides, keys ⊆ {"where", "limit",
            "budget_tokens"}, forwarded verbatim.
        output_path (str): path to write the emitted ``.mjs`` script to.
        target_root (str, optional): explicit containment root; defaults to
            ``repo_root`` when the request carries one, else to
            ``output_path``'s parent directory (see module docstring).
        name (str, optional): forwarded to ``emit.emit_script`` (plan route).
        description (str, optional): forwarded to ``emit.emit_script`` (plan
            route).
        cloud_spawn (dict, optional): the cloud-spawn route -- see module
            docstring. Replies ``{"ok": True, "create_session": {...}}``.
        chatty (bool, optional, default False): plan route; compose an opt-in
            chatty workflow (``emit.compose_script``'s ``chatty``).
        force (bool, optional, default False): overwrite an ``output_path``
            that already holds a different session's emission. Off by
            default -- see ``ForeignEmissionError``.
        session_id (str, optional): emitter identity recorded in the
            provenance receipt; falls back to the fleet-canonical
            ``session.core.resolve_session_id`` ladder, then to "". Never
            minted -- see ``_receipt_session_id``.
        preamble (str, optional): a run-wide posture block rendered
            once (a ``_shared``/``PREAMBLE`` const, route-dependent) into
            every EXECUTOR-tier prompt this emission composes -- never the
            commit/preflight/verify-op/test phases. Forwarded verbatim to
            ``emit.emit_script`` (plan route) or ``queue_emit.
            emit_queue_script`` (queue route); this op never opens or reads
            a preamble FILE itself.
        preamble_path (str, optional): the ``--preamble FILE`` path the
            caller read ``preamble`` from -- recorded in the receipt
            alongside ``preamble_sha256``, never resolved or re-read here.
        preamble_sha256 (str, optional): the caller-computed digest of the
            preamble file's bytes -- recorded verbatim, never recomputed.
        box_terms (list[str], optional), box_terms_path (str, optional): the
            driver's binding box constraints (``--box-terms FILE``, one term per
            line). The caller splices them into ``preamble``; this op records them
            in the emission receipt and, on the plan route, the script's
            ``meta.boxTerms``.
        lanes (bool, optional): with ``inventory_path`` -- partition the
            inventory into write-disjoint lanes and byte-bounded sequential
            parts, pin ``<run-id>.lanes.json``, and emit one script per ready
            part holding a live row (``_emit_lanes``). Exclusive of
            ``output_path``.
        part (str, optional): with ``lanes`` -- emit only this part; refuses
            when it is not ready.
        lane_count (int, optional, default 3), hot_files (int, optional,
            default 40): partition parameters, honoured only when the lane
            map is first written.

    Returns:
        {"path": str, "ok": bool, "findings": [<finding dict>, ...],
         "error_count": int, "warn_count": int,
         "receipt": str | None,
         "fire_args": {"repoRoot": str} | absent}
        ``receipt`` is the provenance sidecar's path, or ``None`` when it could
        not be written -- that failure never changes the verdict. ``fire_args``
        is present (plan route only) when a repo root resolves -- see module
        docstring § Reply fields.

    Raises:
        ValueError — if ``plan_path``/``output_path`` (plan route) or
        ``queue``/``profile``/``profile_dir``/``output_path`` (queue route)
        is missing (descriptive message naming the required param), matching
        the cartography.symbols/tree and ``workflow.validate`` error
        contract. Also raised (as ``emit.NoWavesError`` / ``pathspec.
        NoWritesDeclaredError``, propagated uncaught) if the spine
        under-declares — see ``emit.py`` module docstring.
        ``pathspec.NoTestTargetError`` does NOT reach here: ``emit_script``
        -> ``compose_script`` catches it and degrades the terminal phase
        instead (see ``emit.py`` module docstring § The terminal phase
        degrades, it never vetoes).
        QueuePlanConflictError — if ``queue``/``profile`` is passed together
        with ``plan_path``/``inventory_path``.
        PathEscapeError — if ``output_path`` resolves outside
        ``target_root``.
        QueuePathEscapeError (queue route) — if ``run_dir`` (the guarded
        ``output_path``'s parent) or a ``queue`` directory resolves outside
        ``repo_root``.
        ForeignEmissionError — if ``output_path`` already holds a different
        emission and ``force`` is not set.
    """
    refusal = validate_params("dispatch.emit", params, _PARAM_FIELDS)
    if refusal is not None:
        return refusal
    from coordinator_core.ops.dispatch_emit.fanout_cost import FANOUT_GROUP_MODES, read_fanout

    fanout_group = params.get("fanout_group")
    if fanout_group is None:
        fanout_group = "auto"
    if fanout_group not in FANOUT_GROUP_MODES:
        return {"error": f"dispatch.emit params.fanout_group must be one of {list(FANOUT_GROUP_MODES)}, got {fanout_group!r}"}
    # Passed only when it departs from the default: "auto" is each composer's own default.
    fanout_kwargs = {"fanout_group": fanout_group} if fanout_group != "auto" else {}
    plan_path = aliased_param(params, "plan_path", "plan")
    inventory_path = params.get("inventory_path")
    queue = params.get("queue")
    profile_name = params.get("profile")
    sizing_path = params.get("sizing_path")
    ask = params.get("ask")
    ask_ctx: Optional[dict] = None
    receipt_extras: Optional[dict] = None
    pipeline_name = params.get("pipeline")
    pipeline_ctx: Optional[dict] = None
    lanes_requested = bool(params.get("lanes"))
    lane_flags = [
        name
        for name in ("lanes", "part", "lane_count", "hot_files")
        if params.get(name) is not None and params.get(name) is not False
    ]
    if lane_flags and not inventory_path:
        raise ValueError(
            f"dispatch.emit {', '.join(lane_flags)} requires inventory_path"
        )
    if params.get("part") and not lanes_requested:
        raise ValueError("dispatch.emit part requires lanes")
    if lanes_requested and aliased_param(params, "output_path", "out_path"):
        raise ValueError(
            "dispatch.emit lanes derives each part's script path; output_path is not accepted"
        )

    if params.get("resume_missing") and not pipeline_name:
        raise ValueError("dispatch.emit resume_missing is a pipeline-route option and needs pipeline")

    from_sizing = params.get("from_sizing")
    research_flag = params.get("research", False)
    if not isinstance(research_flag, bool):
        raise ValueError("dispatch.emit research must be a boolean")
    research_route = bool(from_sizing) or research_flag
    if _research_ready is None and (params.get("per_group") or params.get("group_refs")):
        if params.get("per_group") is False:
            raise ValueError("dispatch.emit group_refs needs per_group")
        return _emit_per_group(params, repo_root, from_sizing, _dispatch_emit)
    if fanout_group == "split" and not (research_route or pipeline_name):
        return {
            "error": "dispatch.emit params.fanout_group 'split' is honoured by the pipeline and research "
            "routes only; the plan, queue and ask composers take no override"
        }
    research_ctx: Optional[dict] = None
    if research_route:
        if research_flag and not (isinstance(ask, str) and ask):
            raise ValueError("dispatch.emit research needs an ask prompt string")
        if (
            plan_path or inventory_path or queue or profile_name or pipeline_name or sizing_path
            or params.get("cloud_spawn") is not None or (from_sizing and ask)
        ):
            raise PipelineParamConflictError(
                "dispatch.emit research route accepts from_sizing, or research with an ask, "
                "alone -- not with plan_path/inventory_path/queue/profile/pipeline/sizing_path/cloud_spawn"
            )
        research_ctx, pipeline_ctx, params = _research_ready or _research_route_setup(params, repo_root, from_sizing, ask)
        repo_root = repo_root or research_ctx["root"]

    if pipeline_name:
        if plan_path or inventory_path or queue or profile_name or ask or sizing_path or params.get("cloud_spawn") is not None:
            raise PipelineParamConflictError(
                "dispatch.emit accepts pipeline alone, not with plan_path/inventory_path/"
                f"queue/profile/ask/sizing_path/cloud_spawn (got pipeline={pipeline_name!r})"
            )
        brief = params.get("brief")
        if not (isinstance(brief, str) and brief.strip()):
            raise ValueError("dispatch.emit pipeline route requires a brief path")
        given_root = repo_root or params.get("target_root")
        if not given_root:
            raise ValueError("dispatch.emit pipeline route requires repo_root or target_root")
        pipeline_root = Path(given_root)
        brief_path = Path(brief)
        try:
            os.stat(brief_path if brief_path.is_absolute() else pipeline_root / brief_path)
        except OSError:
            raise ValueError(f"dispatch.emit pipeline brief path does not exist: {brief}") from None
        brief = brief_path.as_posix()
        run_id = (
            f"{RUN_ID_PREFIX}{pipeline_name}-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        resume_missing = params.get("resume_missing", False)
        if not isinstance(resume_missing, bool):
            raise ValueError("dispatch.emit resume_missing must be a boolean")
        if resume_missing and not params.get("scratch_dir"):
            raise ValueError(
                "dispatch.emit resume_missing needs the interrupted run's scratch_dir; "
                "the per-run default is fresh and holds no outputs"
            )
        raw_subjects = params.get("subjects")
        subjects = () if raw_subjects is None else tuple(subjects_from_value(raw_subjects, base=pipeline_root))
        pipeline_ctx = {
            "root": pipeline_root,
            "run_id": run_id,
            "resume_missing": resume_missing,
            "inputs": PipelineInputs(
                brief=brief,
                subjects=subjects,
                scratch_dir=_pipeline_scratch_rel(pipeline_root, params.get("scratch_dir"), run_id),
                flags=dict(params.get("flags") or {}),
                lists=dict(params.get("lists") or {}),
                validator=params.get("validator"),
            ),
        }
        if not aliased_param(params, "output_path", "out_path"):
            params = {**params, "output_path": str(pipeline_root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs")}
        repo_root = repo_root or pipeline_root

    if (ask or sizing_path) and research_ctx is None:
        if plan_path or inventory_path or queue or profile_name:
            raise SizingPathConflictError(
                "dispatch.emit accepts ask/sizing_path alone, not with plan_path/"
                f"inventory_path/queue/profile (got ask={ask!r}, sizing_path={sizing_path!r})"
            )
        if not sizing_path and not (isinstance(ask, str) and ask):
            raise ValueError("dispatch.emit ask requires a prompt string or sizing_path")
        chosen_source = params.get("ask_source")
        ask_source = chosen_source if chosen_source else (ASK_SOURCE_PM if sizing_path else ASK_SOURCE_RELAYED)
        pm_verdict: Optional[str] = None
        if ask_source not in ASK_SOURCES:
            raise ValueError(f"dispatch.emit ask_source takes one of {ASK_SOURCES}, got {ask_source!r}")
        if ask_source == ASK_SOURCE_RELAYED and sizing_path:
            raise ValueError("dispatch.emit ask_source 'relayed' marks a raw ask's text; sizing_path carries none")
        route_pref = _route_pref.RoutePreference(None, None)
        if params.get("route_preference") is not None or params.get("em_rationale") is not None:
            if sizing_path:
                raise ValueError(
                    "dispatch.emit route_preference/em_rationale mark a raw ask; sizing_path carries its own"
                )
            bad = _route_pref.refusal(
                "dispatch.emit", params.get("route_preference"), params.get("em_rationale")
            )
            if bad:
                raise ValueError(bad)
            route_pref = _route_pref.parse(params.get("route_preference"), params.get("em_rationale"))
        sizing_rel: Optional[str] = None
        relayed_slice_text: Optional[str] = None
        if sizing_path:
            ask_root = _sizing_root(params, repo_root, str(sizing_path))
            sizing_rel = normalize_sizing_rel(ask_root, str(sizing_path))
            ask_baton = _read_baton_ids(
                ask_root, params.get("baton"), params.get("deliverable_id")
            )
            from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing as _load_ask_sizing
            from coordinator_core.ops.sizing_intake_contract import (
                is_intake, read_em_brief, read_em_context_paths,
            )

            intake_doc = _load_ask_sizing(ask_root, sizing_rel)
            sizing_doc = intake_doc
            if is_intake(intake_doc):
                # Sized in-run: the gate and plan-blitz args are the run's, as for a raw ask.
                ask_sizing = {"writes": list(params.get("writes") or []), "batons": []}
                route_pref = _route_pref.from_em_analysis(intake_doc)
                if slice_ask_source(intake_doc) == ASK_SOURCE_RELAYED:
                    ask_source = ASK_SOURCE_RELAYED
                    relayed_slice_text = str(intake_doc.get("intent") or "") or None
            else:
                intake_doc = None
                ask_sizing = _gate_sizing_at_emit(
                    ask_root, sizing_rel, list(params.get("writes") or []), baton=ask_baton,
                    cross_repo_approved=bool(params.get("cross_repo_approved")),
                )
        else:
            intake_doc = sizing_doc = None
            given_root = repo_root or params.get("target_root")
            if not given_root:
                raise ValueError("dispatch.emit ask requires repo_root or target_root")
            ask_root = Path(given_root)
            ask_baton = _read_baton_ids(
                ask_root, params.get("baton"), params.get("deliverable_id")
            )
            if ask_source == ASK_SOURCE_PM:
                from coordinator_core.ops import baton_pm_turns as _pm_turns

                pm_check = _pm_turns.verify_pm_ask(
                    ask, session_id=_receipt_session_id(params) or None,
                    cwd=params.get("session_root") or str(ask_root),
                )
                pm_verdict = pm_check["verdict"]
                if pm_verdict == "unmatched":
                    msg = "dispatch.emit ask_source 'pm': the ask quotes no recorded PM turn of this session."
                    nearest = pm_check.get("nearest")
                    if nearest:
                        msg += f" Nearest recorded turn {nearest['turn']}: {nearest['text'][:400]!r}."
                        if pm_check.get("diff"):
                            msg += f" Diff: {pm_check['diff']}."
                    msg += (
                        " Put one verbatim turn in `ask` and the others in `context`,"
                        " or mark the ask relayed."
                    )
                    raise ValueError(msg)
        run_stamp = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_id = f"warp-{Path(sizing_rel).stem}-{run_stamp}" if sizing_rel else f"ask-{run_stamp}"
        reserved_parent = None
        rebound = False
        output_redirect = None
        inherited_base = None
        resumes_ask = None
        if params.get("reserved_run_id") is not None:
            run_id, reserved_parent = _reserved_run(ask_root, params["reserved_run_id"], sizing_doc, sizing_rel)
        elif sizing_rel:
            from coordinator_core.ops.dispatch_emit.halted_run import (
                halted_ask_run_for, halted_run_base, halted_run_for,
            )

            halted_id = halted_run_for(ask_root, sizing_rel)
            if halted_id:
                inherited_base = _recorded_run_base(ask_root, halted_id) or halted_run_base(
                    record_dir(ask_root, halted_id)
                )
            else:
                resumes_ask = halted_ask_run_for(ask_root, sizing_rel)
                if resumes_ask:
                    inherited_base = _recorded_run_base(ask_root, resumes_ask) or halted_run_base(
                        record_dir(ask_root, resumes_ask)
                    )
            if halted_id:
                from coordinator_core.ops.dispatch_emit.halted_run import rebind_blocker, recorded_emitter

                blocker = None
                if not aliased_param(params, "output_path", "out_path"):
                    blocker = rebind_blocker(ask_root, halted_id, _receipt_session_id(params))
                if blocker is None:
                    run_id, rebound = halted_id, True
                else:
                    output_redirect = {
                        "from": f"{RUN_DIR_ROOT}/{halted_id}.workflow.mjs",
                        "to": f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs",
                        "halted_run": halted_id,
                        "prior_session": recorded_emitter(ask_root, halted_id),
                        "reason": blocker,
                    }
        prompt = ask if isinstance(ask, str) and ask else None
        sizing_stop = (
            intake_doc["em_analysis"].get(STOP_AFTER_KEY)
            if intake_doc is not None and isinstance(intake_doc.get("em_analysis"), dict)
            else None
        )
        effective_stop = params.get(STOP_AFTER_KEY) or sizing_stop
        if effective_stop not in (None, STOP_AFTER_PLAN):
            raise ValueError(
                f"dispatch.emit {STOP_AFTER_KEY} takes only {STOP_AFTER_PLAN!r}, got {effective_stop!r}"
            )
        ask_ctx = {
            "stop_after": effective_stop,
            "root": ask_root, "prompt": prompt, "sizing_rel": sizing_rel, "run_id": run_id,
            "reserved_parent": reserved_parent, "rebound": rebound, "output_redirect": output_redirect,
            "inherited_base": inherited_base, "resumes_ask": resumes_ask,
            "ask_source": ask_source, "pm_verification": pm_verdict,
            "relayed_slice_text": relayed_slice_text,
            "route_preference": route_pref,
            "required_reviewers": list(params.get("required_reviewers") or ()),
        }
        if ask_baton is not None:
            ask_ctx["baton"] = ask_baton
        from coordinator_core.ops.dispatch_emit import ask_brief as _ask_brief

        try:
            em_brief = _ask_brief.load_em_brief(
                ask_root,
                brief=params.get("ask_brief"),
                brief_file=params.get("ask_brief_file"),
                context=params.get("ask_context")
                or (read_em_context_paths(intake_doc) if intake_doc is not None else ()),
                session_id=_receipt_session_id(params),
            )
        except _ask_brief.EmBriefRefused as exc:
            from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused

            raise SizingFireRefused([str(exc)]) from exc
        if intake_doc is not None and not (params.get("ask_brief") or params.get("ask_brief_file")):
            sizing_brief = read_em_brief(intake_doc)
            if sizing_brief:
                em_brief = _ask_brief.EmBrief(
                    text=sizing_brief, source=sizing_rel,
                    context=em_brief.context if em_brief is not None else (),
                )
        if em_brief is not None:
            ask_ctx["em_brief"] = em_brief
            receipt_extras = {"em_brief": _ask_brief.receipt_fields(em_brief)}
        if sizing_path and intake_doc is None:
            ask_ctx.update(ask_sizing)
            receipt_extras = {
                **(receipt_extras or {}),
                "batons": ask_sizing["batons"],
                "uncommitted": ask_sizing["uncommitted"],
            }
        else:
            # A raw ask's arm is unknown until the in-run size phase, so its script always
            # carries the planBlitz branch and needs the launch args the sizing route resolves.
            ask_ctx["plan_blitz_args"] = _resolve_plan_blitz_args(None)
            if intake_doc is not None:
                ask_ctx["writes"] = ask_sizing["writes"]
        if not aliased_param(params, "output_path", "out_path"):
            default_out = ask_root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
            default_out.parent.mkdir(parents=True, exist_ok=True)
            params = {**params, "output_path": str(default_out)}
        repo_root = repo_root or ask_root
        if sizing_path:
            if ask_baton is not None:
                from coordinator_core.ops.dispatch_emit.ask_contract import write_run_baton

                write_run_baton(ask_root, run_id, ask_baton["path"])
            _repoint_fire_holds(ask_root, ask_sizing["batons"], Path(params["output_path"]))

    cloud_spawn = params.get("cloud_spawn")
    if cloud_spawn is not None:
        if not isinstance(cloud_spawn, dict):
            raise ValueError("dispatch.emit cloud_spawn must be an object")
        if plan_path or inventory_path or queue or profile_name or params.get("output_path"):
            raise ValueError(
                "dispatch.emit accepts cloud_spawn alone, not with plan_path/"
                "inventory_path/queue/profile/output_path"
            )
        return {
            "ok": True,
            "create_session": build_cloud_spawn(
                cloud_spawn.get("kind"),
                cloud_spawn.get("source_repo"),
                cloud_spawn.get("parent_session_id"),
                cloud_spawn.get("channel_pr"),
                cloud_spawn.get("question"),
            ),
        }
    is_queue_route = bool(queue) or bool(profile_name)

    if is_queue_route and (plan_path or inventory_path):
        raise QueuePlanConflictError(
            "dispatch.emit accepts either queue/profile or plan_path/"
            f"inventory_path, not both (got queue={queue!r}, "
            f"profile={profile_name!r}, plan_path={plan_path!r}, "
            f"inventory_path={inventory_path!r})"
        )

    if plan_path and inventory_path:
        raise InventoryPathConflictError(
            "dispatch.emit accepts either plan_path or inventory_path, not both "
            f"(got plan_path={plan_path!r}, inventory_path={inventory_path!r})"
        )

    if lanes_requested:
        return _emit_lanes(params, repo_root)

    inventory_review_specs: list = []
    inventory_accounting: Optional[dict] = None
    tranche_inventory: dict = {}
    if not is_queue_route and ask_ctx is None and pipeline_ctx is None:
        if inventory_path:
            _refuse_inventory_outside_repo(
                inventory_path, params.get("inventory_repo_root") or repo_root
            )
            part = params.get("inventory_part")
            resume_skipped: list = []
            tranche_report: dict = {}
            withheld_plans: dict = {}
            part_items: list = []
            accounting_ledger: dict = {}
            accounting_item_ids = [
                _strip_backtick(r["id"])
                for r in parse_chunk_table(Path(inventory_path).read_text(encoding="utf-8"))
            ]
            spine_text, spine_path = mint_spine(
                inventory_path,
                max_rows=None if params.get("review_only_rows") else (params.get("max_rows") or DEFAULT_MAX_INVENTORY_ROWS),
                part=(int(part[0]), int(part[1])) if part else None,
                skip_landed=bool(params.get("skip_landed")),
                skipped_out=resume_skipped,
                row_budget=params.get("row_budget"),
                tranche_out=tranche_report,
                withheld_out=withheld_plans,
                tranche_inventory_out=tranche_inventory,
                part_items_out=part_items,
                accounting_out=accounting_ledger,
                review_rows=frozenset(params.get("review_only_rows") or ()),
            )
            if part:
                # A part is cut from the inventory's rows, not its record, so
                # the record still lists every plan: scope the review to the
                # part's own plans.
                part_item_set = set(part_items)
                part_plans = frozenset(
                    _strip_backtick(r["spec path"])
                    for r in parse_chunk_table(Path(inventory_path).read_text(encoding="utf-8"))
                    if _strip_backtick(r["id"]) in part_item_set
                )
                only_review_plans = (
                    part_plans if only_review_plans is None else only_review_plans & part_plans
                )
            if tranche_inventory:
                # The tranche is its own inventory record from here on: its
                # landed-reconcile and review specs must see only its plans.
                inventory_path = str(tranche_inventory["path"])
                _writes.write(Path(inventory_path), tranche_inventory["text"])
            guarded_spine_path = contained_path(
                spine_path, [Path(inventory_path).resolve().parent]
            )
            if guarded_spine_path is None:
                raise PathEscapeError(
                    f"minted spine path escapes its inventory record's directory: "
                    f"{spine_path!r} not under {Path(inventory_path).resolve().parent!r}"
                )
            _refuse_spent_overwrite(
                guarded_spine_path,
                spine_text.encode("utf-8"),
                guarded_spine_path.with_name(guarded_spine_path.name.removesuffix(".md") + ".workflow.mjs"),
            )
            _writes.write(guarded_spine_path, spine_text)
            plan_path = str(guarded_spine_path)
            # A review-only run executes nothing: no landed-flip writes to the
            # plans and no pre-dispatch checks or falsifier reviews.
            review_only_run = params.get("review_only_rows") is not None
            landed_reconciled = {} if review_only_run else reconcile_landed(Path(inventory_path))
            inventory_review_specs = (
                []
                if review_only_run
                else _inventory_review_specs(Path(inventory_path), only_review_plans, _writes)
            )

        if not plan_path:
            raise ValueError(f"dispatch.emit requires param: {spellings('plan_path', 'plan')}")

    output_path = aliased_param(params, "output_path", "out_path")
    if not output_path and inventory_path and not is_queue_route:
        output_path = str(guarded_spine_path.with_name(guarded_spine_path.stem + ".workflow.mjs"))
    if not output_path:
        raise ValueError(f"dispatch.emit requires param: {spellings('output_path', 'out_path')}")

    if is_queue_route and not params.get("target_root") and repo_root is None:
        raise QueueRootMissingError(
            "dispatch.emit queue route requires either the request's "
            "repo_root or an explicit target_root param -- neither was "
            "given, and the queue route never defaults to output_path's "
            "own parent directory"
        )

    target_root = (
        params.get("target_root")
        or (str(repo_root) if repo_root is not None else None)
        or str(Path(output_path).resolve().parent)
    )

    guarded_path = contained_path(Path(output_path), [Path(target_root)])
    if guarded_path is None:
        raise PathEscapeError(
            f"output_path escapes target_root: {output_path!r} not under {target_root!r}"
        )

    # Resolved ONCE and shared by both the commit-phase prompt and the
    # emission receipt below.
    emitting_session_id = _receipt_session_id(params)
    params = {**params, "session_id": emitting_session_id}

    # S1-C5/S1-C6 (docs/plans/2026-09-18-doe-holds-no-scripts.md): resolve the
    # host agent-type-host ladder from the CALLER's own env, not the warm
    # server's. `os.environ` here reads the CALLER's carried
    # `COORDINATOR_AGENT_TYPE_HOST`/`CLAUDE_PLUGIN_ROOT` for an isolated warm
    # dispatch -- `warm.entry_seam._environ_identity_borrow` mirrors the
    # caller-declared `CALLER`-mode env set into `os.environ` for the life of
    # this call and restores it after (see that module's docstring) -- and
    # the caller's own real env on a cold spawn. `resolve_agent_type_host`
    # itself reads nothing from the environment; this is the one read.
    agent_type_host = resolve_agent_type_host(
        coordinator_agent_type_host=os.environ.get("COORDINATOR_AGENT_TYPE_HOST"),
        claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT") or _installed_plugin_root(),
    )

    plan_findings: list = []
    receipt_plan_path: Optional[str] = plan_path
    preamble = params.get("preamble")

    # recorded verbatim, whichever route runs -- the caller (the
    # `--preamble FILE` CLI leg) already read the file and hashed its bytes;
    # this op never opens the file itself, matching the negative-spec every
    # other param here keeps (does not derive facts a caller already holds).
    if params.get("preamble_path") or params.get("preamble_sha256"):
        receipt_extras = {
            **(receipt_extras or {}),
            "preamble_path": params.get("preamble_path"),
            "preamble_sha256": params.get("preamble_sha256"),
        }

    box_terms = tuple(params.get("box_terms") or ())
    if box_terms:
        receipt_extras = {
            **(receipt_extras or {}),
            "box_terms": list(box_terms),
            "box_terms_path": params.get("box_terms_path"),
        }

    review_route = (
        EMIT_ROUTE_QUEUE if is_queue_route
        else EMIT_ROUTE_INVENTORY if inventory_path
        else EMIT_ROUTE_PLAN
    )

    if is_queue_route:
        if not queue:
            raise ValueError("dispatch.emit queue route requires param: queue")
        if not profile_name:
            raise ValueError("dispatch.emit queue route requires param: profile")
        profile_dir = params.get("profile_dir")
        if not profile_dir:
            raise ValueError("dispatch.emit queue route requires param: profile_dir")
        stage_models = _validated_stage_models(params.get("stage_models"))

        review_roster_fragment, review_stage_schemas = _load_review_inputs(review_route)
        target_root_path = Path(target_root)
        emission = emit_queue_script(
            profile_name,
            params.get("appetite") or "standard",
            params.get("overrides"),
            queue=[
                q_path if (q_path := Path(q)).is_absolute() else target_root_path / q_path
                for q in queue
            ],
            profile_dir=Path(profile_dir),
            repo_root=Path(target_root),
            run_dir=guarded_path.parent,
            session_id=emitting_session_id,
            agent_type_host=agent_type_host,
            preamble=preamble,
            commit_trailers=read_host_commit_trailers(target_root_path),
            review_roster_fragment=review_roster_fragment,
            review_stage_schemas=review_stage_schemas,
            stage_models=stage_models,
        )
        script = emission.script
        receipt_extras = {**emission.receipt_extras, **(receipt_extras or {})}
        receipt_plan_path = None
    elif research_ctx is not None:
        from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_parts

        chain_parts = compose_chain_parts(
            research_ctx["segments"],
            run_id=research_ctx["run_id"],
            agent_type_host=agent_type_host,
            next_action=research_ctx["next_action"],
            legs=research_ctx["legs"],
            **fanout_kwargs,
        )
        script = chain_parts[0].script
        receipt_extras = {
            **(receipt_extras or {}),
            "run_id": research_ctx["run_id"],
            "brief": research_ctx["brief"],
            **research_ctx["shape"],
        }
        receipt_plan_path = None
    elif pipeline_ctx is not None:
        from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_pipeline_script
        from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate

        manifest = load_manifest(_pipeline_content_root(), pipeline_name)
        pipeline_inputs = replace(
            pipeline_ctx["inputs"],
            lists=normalize_lists(manifest.lists, pipeline_ctx["inputs"].lists),
        )
        pipeline_ctx["inputs"] = pipeline_inputs
        schedule = validate(manifest, pipeline_inputs)
        if pipeline_ctx["resume_missing"]:
            pipeline_inputs, resume_report = _resume_missing_inputs(
                manifest, schedule, pipeline_inputs, pipeline_ctx["root"]
            )
            pipeline_ctx["inputs"] = pipeline_inputs
            pipeline_ctx["resume_report"] = resume_report
        script = compose_pipeline_script(
            manifest,
            pipeline_inputs,
            schedule,
            run_id=pipeline_ctx["run_id"],
            agent_type_host=agent_type_host,
            **fanout_kwargs,
        )
        receipt_extras = {
            **(receipt_extras or {}),
            "pipeline": pipeline_name,
            "run_id": pipeline_ctx["run_id"],
            "brief": pipeline_ctx["inputs"].brief,
            "manifest_sha256": manifest.sha256,
            "subjects": [subject_key(s) for s in pipeline_ctx["inputs"].subjects],
            **({"validator": pipeline_ctx["inputs"].validator} if pipeline_ctx["inputs"].validator else {}),
            **({"resume_missing": pipeline_ctx["resume_report"]} if pipeline_ctx["resume_missing"] else {}),
        }
        receipt_plan_path = None
    elif ask_ctx is not None:
        from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script

        review_roster_fragment, review_stage_schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
        ask_script_path = _script_path_under(guarded_path, ask_ctx["root"])
        from coordinator_core.ops.dispatch_emit import run_token as _run_token

        try:
            ask_run_token = _run_token.mint(
                Path(ask_ctx["root"]), ask_ctx["run_id"], ask_script_path or "",
                session_id=emitting_session_id, sizing_rel=ask_ctx["sizing_rel"],
            )
        except (OSError, ValueError):
            # No git dir to hold the record, or no repo-relative script to bind it to: the run
            # hands its commit back to the EM.
            ask_run_token = None
        ask_ctx["run_token"] = ask_run_token
        script = compose_ask_script(
            review_roster_fragment=review_roster_fragment,
            review_stage_schemas=review_stage_schemas,
            repo_root=Path(ask_ctx["root"]).as_posix(),
            prompt=ask_ctx["prompt"],
            sizing_rel=ask_ctx["sizing_rel"],
            run_id=ask_ctx["run_id"],
            session_id=emitting_session_id,
            script_path=ask_script_path,
            plan_blitz_args=ask_ctx.get("plan_blitz_args"),
            writes=ask_ctx.get("writes", ()),
            cross_repo_approved=bool(params.get("cross_repo_approved")),
            agent_type_host=agent_type_host,
            baton=ask_ctx.get("baton"),
            accept_pending=bool(ask_ctx.get("accept_pending")),
            preamble=preamble,
            em_brief=ask_ctx.get("em_brief"),
            stop_after=ask_ctx.get("stop_after"),
            in_run_commit=ask_run_token is not None,
            ask_source=ask_ctx["ask_source"],
            route_preference=ask_ctx["route_preference"],
            required_reviewers=ask_ctx["required_reviewers"],
            run_base_sha=ask_ctx.get("inherited_base") or (
                _recorded_run_base(Path(ask_ctx["root"]), ask_ctx["run_id"])
                if ask_ctx.get("reserved_parent") or ask_ctx.get("rebound")
                else None
            ),
        )
        ask_body = ask_ctx["prompt"] or ask_ctx["relayed_slice_text"]
        if ask_body:
            # Verbatim, engine-written into the record dir: the stage reads it into every brief; no agent copies it.
            ask_name = RELAYED_ASK_FILE if ask_ctx["ask_source"] == ASK_SOURCE_RELAYED else ASK_TEXT_FILE
            ask_file = record_dir(Path(ask_ctx["root"]), ask_ctx["run_id"]) / ask_name
            ask_file.parent.mkdir(parents=True, exist_ok=True)
            ask_file.write_text(ask_body, encoding="utf-8", newline="\n")
        if not ask_ctx["route_preference"].is_empty():
            pref_file = record_dir(Path(ask_ctx["root"]), ask_ctx["run_id"]) / _route_pref.ROUTE_PREFERENCE_FILE
            pref_file.parent.mkdir(parents=True, exist_ok=True)
            pref_file.write_text(_route_pref.to_json(ask_ctx["route_preference"]), encoding="utf-8", newline="\n")
        bound = _start_requirements(ask_ctx, all_of_pm_ask=bool(params.get("all_of_pm_ask")))
        ask_ctx["revisions_inherited"] = [
            {"id": i.id, "first_line": i.text.split("\n", 1)[0].strip()}
            for i in (bound.items if bound is not None else ())
            if i.origin == "revision"
        ]
        receipt_plan_path = None
    else:
        # AC22: the plan route loads the roster fragment and DoE's stage
        # schemas itself, through the existing content-root resolution
        # -- never a caller-supplied fragment param, and never a guessed
        # roster on an unresolvable sibling root (refuses instead).
        if not inventory_path:
            _refuse_unapproved_body(plan_path)
        review_roster_fragment, review_stage_schemas = _load_review_inputs(review_route)
        hold_rows = frozenset(params.get("hold_rows") or ())
        if hold_rows and inventory_path:
            raise ValueError("dispatch.emit hold_rows is a plan-route option and does not take inventory_path")
        if params.get("hold_reason") and not hold_rows:
            raise ValueError("dispatch.emit hold_reason requires hold_rows")
        held_out: dict = {}
        try:
            script = emit_script(
                plan_path,
                name=params.get("name"),
                description=params.get("description"),
                repo_root=repo_root or _repo_root_for_plan(plan_path),
                session_id=emitting_session_id,
                review_roster_fragment=review_roster_fragment,
                review_stage_schemas=review_stage_schemas,
                agent_type_host=agent_type_host,
                preamble=preamble,
                box_terms=box_terms,
                script_path=_terminal_commit_script_path(guarded_path, repo_root, plan_path, target_root),
                findings_out=plan_findings,
                landed_rows=frozenset(params["landed_rows"]) if params.get("landed_rows") is not None else None,
                review_only_rows=frozenset(params["review_only_rows"]) if params.get("review_only_rows") is not None else None,
                run_base_sha=params.get("run_base_sha"),
                review_carried_paths=tuple(params.get("review_carried_paths") or ()),
                chatty=bool(params.get("chatty")),
                cross_repo_approved=bool(params.get("cross_repo_approved")),
                predispatch=bool(inventory_path) and params.get("review_only_rows") is None,
                review_specs=inventory_review_specs,
                certified_plans=_certified_plans(inventory_path),
                credit_rows=rows_backed_before_base,
                hold_rows=hold_rows or None,
                hold_reason=params.get("hold_reason"),
                held_out=held_out,
                auto_split=bool(params.get("auto_split")),
                sibling=params.get("sibling"),
            )
        except ScriptOverCapError as over:
            # Part emits re-enter on this tranche record, not a fresh one (`_emit_inventory_parts`).
            if tranche_inventory:
                over.tranche_inventory = inventory_path
            raise
        except NoWavesError as empty:
            from coordinator_core.ops.dispatch_emit.spine_read import describe_exclusions

            dropped = describe_exclusions(plan_path)
            if dropped:
                raise NoWavesError(f"{empty}; read_spine excluded: {dropped}") from empty
            raise
        if inventory_path and params.get("review_only_rows") is None:
            inventory_accounting = _account_inventory(
                plan_path,
                accounting_item_ids,
                accounting_ledger,
                landed_rows=params.get("landed_rows"),
                sibling=params.get("sibling"),
            )
        if held_out:
            receipt_extras = {
                **(receipt_extras or {}),
                "hold": {
                    **held_out,
                    "by": emitting_session_id,
                    "at": datetime.now().isoformat(timespec="seconds"),
                },
            }

    def _declare_output_root() -> None:
        nonlocal pipeline_output_root
        if pipeline_ctx is None:
            return
        from coordinator_core.bash_guards._write_bump_applicability import declare_run_output_root

        pipeline_output_root = (Path(pipeline_ctx["root"]) / pipeline_ctx["inputs"].scratch_dir).resolve()
        declare_run_output_root(pipeline_ctx["run_id"], str(pipeline_output_root))

    pipeline_output_root = None
    split_parts = len(chain_parts) > 1 if research_ctx is not None else False
    if split_parts:
        stem = str(guarded_path)
        stem = stem[: -len(".workflow.mjs")] if stem.endswith(".workflow.mjs") else guarded_path.with_suffix("").as_posix()
        emitted = []
        for index, part in enumerate(chain_parts, start=1):
            label = "+".join(part.pipelines)
            emitted.append((part, _emit_script_file(
                Path(f"{stem}-{label}.workflow.mjs"),
                part.script,
                receipt_plan_path=receipt_plan_path,
                params=params,
                receipt_extras={
                    **(receipt_extras or {}),
                    "part": f"{index}/{len(chain_parts)}",
                    "pipelines": list(part.pipelines),
                },
                plan_findings=plan_findings,
                session_id=emitting_session_id,
                agent_type_host=agent_type_host,
                before_write=_declare_output_root,
            )))
        written = emitted[0][1]
        # plan_findings ride on every part; count each distinct finding once.
        finding_dicts = list({json.dumps(f, sort_keys=True): f for _, e in emitted for f in e["findings"]}.values())
        error_count = sum(1 for f in finding_dicts if f["severity"] == Severity.ERROR.value)
        warn_count = sum(1 for f in finding_dicts if f["severity"] == Severity.WARN.value)
    else:
        written = _emit_script_file(
            guarded_path,
            script,
            receipt_plan_path=receipt_plan_path,
            params=params,
            receipt_extras=receipt_extras,
            plan_findings=plan_findings,
            session_id=emitting_session_id,
            agent_type_host=agent_type_host,
            before_write=_declare_output_root,
        )
        error_count = written["error_count"]
        warn_count = written["warn_count"]
        finding_dicts = written["findings"]

    reply = {
        "path": str(written["path"]),
        "receipt": written["receipt"],
        # The bytes-on-disk digest; a `--fire` caller compares THIS against the
        # script it is about to fire, never the receipt file (best-effort, may be None).
        "sha256": written["sha256"],
        "ok": error_count == 0,
        "findings": finding_dicts,
        "error_count": error_count,
        "warn_count": warn_count,
    }
    # A split chain has one script per part: each part reports its own fanout, none is summed.
    if not split_parts:
        reply["fanout"] = read_fanout(script)
    if split_parts:
        reply["parts"] = [
            {
                "pipeline": "+".join(part.pipelines),
                "path": str(e["path"]),
                "sha256": e["sha256"],
                "receipt": e["receipt"],
                "part": f"{i}/{len(emitted)}",
                "fanout": read_fanout(part.script),
            }
            for i, (part, e) in enumerate(emitted, start=1)
        ]

    if receipt_extras and NOT_ADMITTED_EXTRA_KEY in receipt_extras:
        reply[NOT_ADMITTED_EXTRA_KEY] = receipt_extras[NOT_ADMITTED_EXTRA_KEY]
    if receipt_extras and "hold" in receipt_extras:
        reply["hold"] = receipt_extras["hold"]

    if ask_ctx is not None:
        reply["run_id"] = ask_ctx["run_id"]
        reply["ask_source"] = ask_ctx["ask_source"]
        if ask_ctx.get("pm_verification"):
            reply["pm_verification"] = ask_ctx["pm_verification"]
        if ask_ctx.get("output_redirect"):
            from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY

            reply[OUTPUT_PATH_REDIRECT_KEY] = ask_ctx["output_redirect"]
        if "batons" in ask_ctx:
            reply["batons"] = ask_ctx["batons"]
            reply["uncommitted"] = ask_ctx["uncommitted"]

    if pipeline_ctx is not None:
        reply["run_id"] = pipeline_ctx["run_id"]
        reply["brief"] = pipeline_ctx["inputs"].brief
        reply["scratch_dir"] = pipeline_ctx["inputs"].scratch_dir
        reply["run_output_root"] = pipeline_output_root.as_posix()
        reply.update(_em_mailbox(Path(pipeline_ctx["root"]), pipeline_ctx["inputs"].scratch_dir))
        if pipeline_ctx["resume_missing"]:
            reply["resume_missing"] = pipeline_ctx["resume_report"]
        legacy_tier = _LEGACY_RESEARCH_TIER.get(str(pipeline_name or ""))
        if research_ctx is None and legacy_tier:
            from coordinator_core.ops.dispatch_emit import research_emit

            reply["next_action"] = {
                "op": "research.close",
                "params": {
                    "scratch_dir": (Path(pipeline_ctx["root"]) / pipeline_ctx["inputs"].scratch_dir).as_posix(),
                    "tier": legacy_tier,
                    "value_class": legacy_tier,
                    "run_id": pipeline_ctx["run_id"],
                    "topic_slug": research_emit.topic_slug("", pipeline_ctx["inputs"].brief),
                },
            }

    if research_ctx is not None:
        reply.update(research_ctx["shape"])
        reply["next_action"] = research_ctx["next_action"]
        if research_ctx["multi_repo"]:
            reply["multi_repo"] = research_ctx["multi_repo"]

    if inventory_path:
        reply["landed_reconciled"] = landed_reconciled
        if inventory_accounting is not None:
            reply["inventory_accounting"] = inventory_accounting
        if params.get("skip_landed"):
            reply["resume_skipped_landed"] = resume_skipped
        if tranche_report:
            reply["tranche"] = tranche_report
        if withheld_plans:
            reply["withheld_plans"] = withheld_plans

    if not is_queue_route:
        anchor_root = repo_root or (
            ask_ctx["root"] if plan_path is None else _repo_root_for_plan(plan_path)
        )
        if anchor_root is not None:
            reply["fire_args"] = {"repoRoot": Path(anchor_root).as_posix()}
        if ask_ctx is not None and ask_ctx.get("run_token"):
            # The run's commit token rides the launch args only: never the script's bytes.
            reply.setdefault("fire_args", {})["runToken"] = ask_ctx["run_token"]
        if ask_ctx is not None and ask_ctx.get("revisions_inherited"):
            reply["revisions_inherited"] = list(ask_ctx["revisions_inherited"])

    return reply


_LANE_RUN_DIR = Path(home_dir(".", "mise-inventory")).as_posix()
_MAX_HEADROOM_RETRIES = 3
_HEADROOM_STEP = 0.05


def _inventory_repo_root(inventory: Path) -> Path:
    """The repo root an inventory record's relative paths resolve against: the
    grandparent of its ``state/`` directory, as ``inventory_mint`` resolves spec paths."""
    parents = inventory.resolve().parents
    if len(parents) < 3:
        raise ValueError(
            f"inventory {str(inventory)!r} is not under <repo>/state/mise-inventory/"
        )
    return parents[2]


def _has_live_row(chunk_rows: list) -> bool:
    kinds = _resolve_dep_kinds(chunk_rows)
    return any(kinds[_strip_backtick(r["id"])] == _DEP_KIND_LIVE for r in chunk_rows)


def _live_spec_paths(chunk_rows: list) -> list:
    kinds = _resolve_dep_kinds(chunk_rows)
    return sorted(
        {
            _strip_backtick(r["spec path"])
            for r in chunk_rows
            if kinds[_strip_backtick(r["id"])] == _DEP_KIND_LIVE and _strip_backtick(r["spec path"])
        }
    )


def _existing_plans(spec_paths, root: Path) -> list:
    return [p for p in spec_paths if (root / p).is_file()]


def _inventory_review_specs(
    inventory: Path, only: Optional[frozenset], writes: Optional[_EmitWrites] = None
) -> list:
    """Blinded falsifier-integrity review specs for the plans the live rows of
    ``inventory`` cite; writes each plan's can-report-red JSON beside the record."""
    root = _inventory_repo_root(inventory)
    run_id = read_frontmatter_field(str(inventory), "run_id") or inventory.stem
    plans = sorted(
        _existing_plans(
            _live_spec_paths(parse_chunk_table(inventory.read_text(encoding="utf-8"))), root
        )
    )
    if only is not None:
        plans = [p for p in plans if p in only]
    inputs = review_inputs(
        plans, repo_root=root, report_dir=Path(f"{_LANE_RUN_DIR}/{run_id}.can-report-red")
    )
    for item in inputs:
        if item.report_path is None:
            continue
        target = contained_path(root / item.report_path, [root])
        if target is None:
            raise PathEscapeError(f"can-report-red path escapes the repo root: {item.report_path!r}")
        if writes is not None:
            writes.write(target, item.report_json)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.report_json, encoding="utf-8", newline="\n")
    return review_specs(inputs)


def _wave_row_of(chunk_row: dict):
    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

    row_id = _strip_backtick(chunk_row["id"])
    writes, writes_under = _split_footprint(row_id, chunk_row["footprint"])
    summary = chunk_row["summary"]
    return WaveRow(
        id=row_id,
        title=summary,
        surface=(writes or writes_under or [summary])[0],
        writes=writes,
        reads=[],
        depends_on=_split_id_list(chunk_row["deps"]),
        body=_row_body(
            row_id,
            _strip_backtick(chunk_row["spec path"]),
            summary,
            chunk_row["verification"],
            chunk_row["complexity"],
        ),
        writes_under=tuple(writes_under),
    )


def _read_lane_map(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_lane_map(path: Path, lane_map: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lane_map, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _cost_model(chunk_rows: list, *, root: Path, run_id: str, preamble: Optional[str]) -> tuple:
    """``(row_bytes, fixed_bytes)`` for ``chunk_rows``: per-row estimates from
    ``emit.row_block_bytes`` and the script shell measured off one composed probe row."""
    from coordinator_core.ops.dispatch_emit import emit

    wave_rows = [_wave_row_of(r) for r in chunk_rows]
    context = {
        "plan_path": f"{_LANE_RUN_DIR}/{run_id}.spine.md",
        "repo_root": root,
        "preamble": preamble,
    }
    row_bytes = emit.row_block_bytes(wave_rows, predispatch=True, **context)
    fragment, schemas = _load_review_inputs(EMIT_ROUTE_INVENTORY)
    probe = wave_rows[0]
    shell = emit.compose_script(
        [[probe]],
        name="lane-probe",
        description="lane-probe",
        plan_path=context["plan_path"],
        review_roster_fragment=fragment,
        review_stage_schemas=schemas,
        predispatch=True,
        preamble=preamble,
    )
    fixed_bytes = max(0, len(shell.encode("utf-8")) - row_bytes[probe.id])
    return row_bytes, fixed_bytes


def _build_lane_map(
    chunk_rows: list,
    *,
    root: Path,
    inventory: Path,
    run_id: str,
    start_sha: Optional[str],
    lane_count: int,
    hot_files: int,
    preamble: Optional[str],
) -> dict:
    from coordinator_core.ops.dispatch_emit import emit, lanes

    row_bytes, fixed_bytes = _cost_model(chunk_rows, root=root, run_id=run_id, preamble=preamble)
    params = lanes.LaneParams(
        lanes=lane_count,
        hot_files=hot_files,
        byte_cap=emit._WORKFLOW_SCRIPT_BYTE_CAP,
    )
    bound = plans_with_falsifier(
        _existing_plans(sorted({_strip_backtick(r["spec path"]) for r in chunk_rows}), root),
        repo_root=root,
    )
    return lanes.partition(
        chunk_rows,
        params=params,
        row_bytes=row_bytes,
        fixed_bytes=fixed_bytes,
        bound_plans=bound,
        run_id=run_id,
        source_inventory=inventory.resolve().relative_to(root).as_posix(),
        start_sha=start_sha,
    )


def _resplit_part(
    lane_map: dict,
    part_id: str,
    chunk_by_id: dict,
    *,
    root: Path,
    preamble: Optional[str],
    headroom: float,
) -> Optional[str]:
    """Repack one over-cap part at a tightened ``headroom``, in place: the part
    becomes several sequential parts of its lane and later parts of that lane
    are renumbered. Returns the id of the first resulting part, or ``None``
    when the tightened estimate still fits it in one part. Only a never-emitted
    part is split."""
    from coordinator_core.ops.dispatch_emit import lanes

    lane = next(l for l in lane_map["lanes"] if any(p["id"] == part_id for p in l["parts"]))
    idx = next(i for i, p in enumerate(lane["parts"]) if p["id"] == part_id)
    if any(p["script"] for p in lane["parts"][idx:]):
        raise lanes.RowOverBudgetError(
            f"part {part_id!r} or a later part of lane {lane['id']!r} is already emitted; "
            f"delete {lanes.lane_map_path(lane_map['run_id'])} to re-partition"
        )
    row_bytes, fixed_bytes = _cost_model(
        [chunk_by_id[r] for r in lane["parts"][idx]["rows"]],
        root=root,
        run_id=lane_map["run_id"],
        preamble=preamble,
    )
    budget = lane_map["params"]["byte_cap"] * headroom
    chunks: list = []
    current: list = []
    used = 0
    for r in lane["parts"][idx]["rows"]:
        cost = row_bytes[r] + fixed_bytes
        if cost > budget:
            raise lanes.RowOverBudgetError(
                f"row {r!r}: {row_bytes[r]} row bytes + {fixed_bytes} fixed bytes exceeds "
                f"the part budget {int(budget)}"
            )
        if current and used + cost > budget:
            chunks.append(current)
            current, used = [], 0
        current.append(r)
        used += row_bytes[r]
    chunks.append(current)
    if len(chunks) == 1:
        return None
    tail_rows = [p["rows"] for p in lane["parts"][idx + 1:]]
    rebuilt = lane["parts"][:idx]
    for rows in [*chunks, *tail_rows]:
        pid = f"{lane['id']}-p{len(rebuilt) + 1}"
        rebuilt.append(
            {
                "id": pid,
                "after": [rebuilt[-1]["id"]] if rebuilt else [],
                "rows": rows,
                "inventory": f"{_LANE_RUN_DIR}/{lane_map['run_id']}-{pid}.md",
                "script": None,
                "bytes": 0,
            }
        )
    lane["parts"] = rebuilt
    plan_of = {r: _strip_backtick(chunk_by_id[r]["spec path"]) for r in chunk_by_id}
    review: dict = {}
    for l in lane_map["lanes"]:
        for p in l["parts"]:
            for r in p["rows"]:
                if plan_of[r] in lane_map["falsifier_review_part"]:
                    review.setdefault(plan_of[r], p["id"])
    lane_map["falsifier_review_part"] = dict(sorted(review.items()))
    return rebuilt[idx]["id"]


def _int_param(params: dict, name: str, default: int, *, minimum: int = 1) -> int:
    value = params.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"dispatch.emit {name} must be an integer >= {minimum}, got {value!r}")
    return value


def _waiting_on(lane_map: dict, dispositions: dict, part_id: str) -> list:
    from coordinator_core.ops.dispatch_emit import lanes
    from coordinator_core.ops.dispatch_emit.inventory_mint import _raw_disposition_kind

    parts = lanes._parts_by_id(lane_map)
    return sorted(
        p
        for p in lanes._after_closure(parts, part_id)
        if any(
            _raw_disposition_kind(r, dispositions[r]) == _DEP_KIND_LIVE
            for r in parts[p]["rows"]
        )
    )


def _part_already_emitted(root: Path, part: dict) -> bool:
    """True when the lane map records ``part``'s script and that file is on disk."""
    script = part.get("script")
    return bool(script) and (root / script).is_file()


def _emit_lanes(params: dict, repo_root: Optional[Path]) -> dict:
    """The ``--lanes`` flow: pin or load the lane map, then emit one script per
    ready part holding a live row. Returns ``{"ok", "lane_map", "parts", "not_ready", ...}``."""
    from coordinator_core.ops.dispatch_emit import lanes

    inventory = Path(params["inventory_path"])
    _refuse_inventory_outside_repo(
        str(inventory), params.get("inventory_repo_root") or repo_root
    )
    root = _inventory_repo_root(inventory)
    master_text = inventory.read_text(encoding="utf-8")
    run_id = read_frontmatter_field(str(inventory), "run_id") or inventory.stem
    start_sha = read_frontmatter_field(str(inventory), "start_sha") or None
    chunk_rows = parse_chunk_table(master_text)
    dispositions = {_strip_backtick(r["id"]): r["disposition"] for r in chunk_rows}
    map_path = root / lanes.lane_map_path(run_id)
    lane_map = _read_lane_map(map_path)
    pinned = lane_map is not None
    if not pinned:
        lane_map = _build_lane_map(
            chunk_rows,
            root=root,
            inventory=inventory,
            run_id=run_id,
            start_sha=start_sha,
            lane_count=_int_param(params, "lane_count", lanes.LaneParams.lanes),
            hot_files=_int_param(params, "hot_files", lanes.LaneParams.hot_files, minimum=0),
            preamble=params.get("preamble"),
        )
        _write_lane_map(map_path, lane_map)

    inner = {
        k: v
        for k, v in params.items()
        if k
        not in ("lanes", "part", "lane_count", "hot_files", "inventory_path", "output_path", "inventory_part")
    }
    wanted = params.get("part")
    chunk_by_id = {_strip_backtick(r["id"]): r for r in chunk_rows}
    resplits = 0
    emitted: dict = {}
    skipped: list = []
    while True:
        ready = lanes.ready_parts(lane_map, dispositions)
        parts_by_id = lanes._parts_by_id(lane_map)
        if wanted:
            if wanted not in parts_by_id:
                raise ValueError(
                    f"lane map {map_path.name} has no part {wanted!r}; parts: {sorted(parts_by_id)}"
                )
            if wanted not in ready:
                raise lanes.PartNotReadyError(
                    f"part {wanted!r} is not ready: waiting on "
                    f"{_waiting_on(lane_map, dispositions, wanted)}"
                )
            targets = [wanted]
        else:
            targets = ready
        pending_files: list = []
        try:
            for part_id in targets:
                if part_id in emitted or part_id in skipped:
                    continue
                part = parts_by_id[part_id]
                sub_text = lanes.render_part_inventory(master_text, lane_map, part_id)
                if not _has_live_row(parse_chunk_table(sub_text)):
                    skipped.append(part_id)
                    continue
                if not wanted and _part_already_emitted(root, part):
                    continue
                sub_path = root / part["inventory"]
                sub_path.parent.mkdir(parents=True, exist_ok=True)
                sub_path.write_text(sub_text, encoding="utf-8", newline="\n")
                script_path = sub_path.with_name(sub_path.stem + ".workflow.mjs")
                pending_files = [sub_path, sub_path.with_name(sub_path.stem + ".spine.md")]
                review_here = frozenset(
                    plan
                    for plan, owner in lane_map["falsifier_review_part"].items()
                    if owner == part_id
                )
                reply = _dispatch_emit(
                    {**inner, "inventory_path": str(sub_path), "output_path": str(script_path)},
                    repo_root,
                    only_review_plans=review_here,
                )
                part["script"] = script_path.relative_to(root).as_posix()
                part["bytes"] = script_path.stat().st_size
                emitted[part_id] = {"part": part_id, **reply}
        except ScriptOverCapError as exc:
            for stale in pending_files:
                stale.unlink(missing_ok=True)
            headroom = lane_map["params"]["headroom"]
            for attempt in range(1, _MAX_HEADROOM_RETRIES + 1):
                first_new = _resplit_part(
                    lane_map,
                    part_id,
                    chunk_by_id,
                    root=root,
                    preamble=params.get("preamble"),
                    headroom=round(headroom - _HEADROOM_STEP * attempt, 4),
                )
                if first_new is not None:
                    resplits += 1
                    if wanted == part_id:
                        wanted = first_new
                    break
            else:
                raise lanes.RowOverBudgetError(
                    f"part {part_id!r} composes over the byte cap after {_MAX_HEADROOM_RETRIES} "
                    f"headroom reductions: {exc}"
                ) from exc
            continue
        break

    _write_lane_map(map_path, lane_map)
    not_ready = [
        {"part": p, "waiting_on": _waiting_on(lane_map, dispositions, p)}
        for p in sorted(parts_by_id)
        if p not in ready
    ]
    for item in not_ready:
        print(
            f"dispatch.emit: part {item['part']} not ready, waiting on {item['waiting_on']}",
            file=sys.stderr,
        )
    return {
        "ok": all(e["ok"] for e in emitted.values()),
        "lane_map": map_path.relative_to(root).as_posix(),
        "lane_map_pinned": pinned,
        "part_resplits": resplits,
        "parts": list(emitted.values()),
        "skipped_closed_parts": skipped,
        "not_ready": not_ready,
        "fire_args": {"repoRoot": Path(repo_root or root).as_posix()},
    }


def _read_baton_ids(root: Path, baton: Optional[str], deliverable_id: Optional[str]) -> Optional[dict]:
    """`{"path", "deliverable_id"}` read in-process from an existing baton, or None when neither
    flag is given. Refuses a missing file, a null id, or a `deliverable_id` the baton disagrees with."""
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused

    if baton is None and deliverable_id is None:
        return None
    if baton is None:
        raise SizingFireRefused(["--deliverable-id needs --baton"])
    rel = str(baton).replace("\\", "/")
    prefix = Path(home_dir("", "handoffs")).as_posix() + "/"
    if not rel.startswith(prefix) or ".." in rel.split("/"):
        raise SizingFireRefused([f"baton must be repo-relative under {prefix}: {rel}"])
    target = Path(root) / rel
    if not target.is_file():
        raise SizingFireRefused([f"baton not found on disk: {rel}"])
    split = split_frontmatter(target.read_text(encoding="utf-8"))
    fm = split.fm_text if split else ""
    handoff_id = read_fm_field_unquoted(fm, "handoff_id")
    baton_dlv = read_fm_field_unquoted(fm, "deliverable_id")
    if not handoff_id or handoff_id == "null":
        raise SizingFireRefused([f"baton {rel} carries no handoff_id"])
    if not baton_dlv or baton_dlv == "null":
        raise SizingFireRefused([f"baton {rel} carries a null deliverable_id"])
    if deliverable_id is not None and deliverable_id != baton_dlv:
        raise SizingFireRefused(
            [f"--deliverable-id {deliverable_id!r} differs from baton {rel}'s {baton_dlv!r}"]
        )
    return {"path": rel, "deliverable_id": baton_dlv}


_BATON_REQUIRED_SECTIONS = (
    "## Specification",
    "## Reference materials (read first)",
    "## Acceptance criteria",
)


def _empty_baton_sections(path: Path) -> list:
    """Required baton sections with no non-whitespace before the next ``## `` heading."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    bodies: dict = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.strip()
            bodies.setdefault(current, [])
        elif current is not None:
            bodies[current].append(line)
    return [h for h in _BATON_REQUIRED_SECTIONS if h in bodies and not "".join(bodies[h]).strip()]


def _resolve_plan_blitz_args(sizing_abs: Optional[str]) -> dict:
    """planBlitz launch args for an emit whose script carries a planBlitz branch.

    Raises ``SizingFireRefused`` when provisionSidecarCli is unresolved: plan-blitz refuses
    every baton without it, so a script emitted here would halt every M+ run with "no ready
    plan". Fail at emit, never later.
    """
    from coordinator_core.ops.dispatch_emit import plan_blitz_args
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
    from coordinator_core.warm.caller_context import resolve_caller_context

    plugin_root = resolve_caller_context().plugin_root
    args = plan_blitz_args.resolve(
        plugin_root=Path(plugin_root) if plugin_root else None,
        engine_root=Path(__file__).resolve().parents[3],
        sizing_abs=sizing_abs,
    )
    if not args.get("provisionSidecarCli"):
        raise SizingFireRefused(
            [
                "provisionSidecarCli unresolved: no provision-sidecar launcher under the "
                "settings home, on PATH, or at <engine>/coordinator/bin/provision-sidecar.py; "
                "plan-blitz would refuse every baton. Install the coordinator settings home "
                "or set COORDINATOR_SETTINGS_HOME, then re-emit."
            ]
        )
    return args


_dispatch_emit.__doc__ = _dispatch_emit_body.__doc__


def _gate_sizing_at_emit(
    root: Path,
    sizing_rel: str,
    writes: list,
    baton: Optional[dict] = None,
    cross_repo_approved: bool = False,
) -> dict:
    """Run the in-run gate and the footprint check before any script is composed.

    Returns ``plan_blitz_args``, ``writes``, ``batons`` and ``uncommitted`` for the emit, plus
    ``baton`` and ``accept_pending`` when they apply; raises ``SizingFireRefused`` on any halt.
    A ``touchpoint`` halt on a sizing whose mode is in ``APM_ADMISSIBLE_MODES``, or whose route and
    size skip sizing acceptance (``sizing_acceptance_skipped``), is non-fatal:
    the script embeds the accept phase and re-gates in-run. In-process only; ``uncommitted``
    is derived from whether the sizing already named a baton, never from git.
    """
    from coordinator_core.ops.dispatch_emit.ask_gate import gate
    from types import SimpleNamespace

    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_TOUCHPOINT
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import CrossRepoWriteError, check_cross_repo_writes
    from coordinator_core.ops.dispatch_emit.sizing_fire import (
        ARM_M_PLUS,
        ARM_ROADMAP,
        SizingFireRefused,
        SizingHandBack,
        load_sizing_effective,
        resolve_arm,
    )
    from coordinator_core.ops.sizing_acceptance import APM_ADMISSIBLE_MODES, sizing_acceptance_skipped

    if not writes:
        from coordinator_core.ops.dispatch_emit.sizing_xs_mint import recorded_xs_writes

        writes = recorded_xs_writes(Path(root), sizing_rel)
    try:
        check_cross_repo_writes(
            [SimpleNamespace(id="--writes", writes=list(writes), writes_under=())],
            Path(root), approved=cross_repo_approved,
        )
    except CrossRepoWriteError as exc:
        raise SizingFireRefused([str(exc)]) from exc
    sizing = load_sizing_effective(root, sizing_rel)
    had_baton = bool(sizing.get("baton"))
    # An XS footprint is authored at run time when --writes is absent; the gate's
    # "XS needs writes" check re-runs in-run against the seeded set.
    verdict = gate(
        root,
        sizing_rel,
        writes=writes or ["<footprint authored at run time>"],
        baton=baton["path"] if baton else None,
    )
    accept_pending = False
    if (
        verdict.halt is not None
        and verdict.halt.get("kind") == HALT_TOUCHPOINT
        and (
            sizing.get("interaction_mode") in APM_ADMISSIBLE_MODES
            or sizing_acceptance_skipped(
                sizing.get("route"), (sizing.get("estimate") or {}).get("tshirt")
            )
        )
    ):
        accept_pending = True
        verdict = replace(verdict, arm=resolve_arm(sizing), halt=None)
    if verdict.halt is not None:
        halt = verdict.halt
        if halt.get("handback"):
            raise SizingHandBack(halt["handback"])
        line =f"{halt['kind']}: {halt['reason']}"
        if halt.get("touchpoint"):
            line += f" — run: {halt['touchpoint']}"
        raise SizingFireRefused([line])
    minted = verdict.baton or baton
    if minted:
        empty = _empty_baton_sections(Path(root) / minted["path"])
        if empty:
            raise SizingFireRefused([f"baton {minted['path']} has empty sections: {', '.join(empty)}"])
    out: dict = {"writes": writes, "batons": [], "uncommitted": []}
    if baton:
        out["baton"] = baton
    if accept_pending:
        out["accept_pending"] = True
    if verdict.arm in (ARM_M_PLUS, ARM_ROADMAP):
        out["plan_blitz_args"] = _resolve_plan_blitz_args((Path(root) / sizing_rel).as_posix())
        if verdict.baton:
            baton_path = verdict.baton["path"]
            out["batons"] = [baton_path]
            out["uncommitted"] = [] if had_baton else [baton_path, sizing_rel]
        elif accept_pending and baton:
            out["batons"] = [baton["path"]]
    return out


def _repoint_fire_holds(root: Path, batons: "list[str]", script: Path) -> None:
    """Point each reused baton's fire hold at this emission's receipt, the canonical one.

    The lobby's earlier emit stamped the hold citing a script that will now never fire.
    """
    from coordinator_core.roadmap.blitz_land import repoint_fire_hold

    receipt = script.with_name(script.name + ".emitted.json")
    try:
        cite = receipt.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return
    for baton in batons:
        repoint_fire_hold(Path(root), baton, cite)


def _sizing_root(params: dict, repo_root: Optional[Path], sizing_path: str) -> Path:
    """Repo root a ``sizing_path`` resolves against: the request root, an explicit
    ``target_root``, or the ``.git`` ancestor of the sizing file."""
    if repo_root is not None:
        return Path(repo_root)
    if params.get("target_root"):
        return Path(params["target_root"])
    found = _repo_root_for_plan(sizing_path)
    if found is None:
        raise ValueError(
            f"dispatch.emit sizing_path {sizing_path!r} has no .git ancestor; "
            "pass target_root"
        )
    return found
