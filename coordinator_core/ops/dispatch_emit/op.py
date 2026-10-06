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
                                     guarded) and ``flags`` (str->str|bool). Exclusive of
                                     every other route selector
                                     (``PipelineParamConflictError``). Output
                                     defaults to
                                     ``scratch/warp/<run-id>.workflow.mjs``;
                                     receipt extras add pipeline/run_id/
                                     manifest_sha256/subjects. Emit-only.
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
     "fire_args": {"repoRoot": "<posix root>"} | absent,
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
    existed still fires by ignoring an absent ``args``.

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
import sys
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator_core._content_root_primitive import content_root_for
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.content_root import read_content_root
from coordinator_core.git.commit_trailers import read_host_commit_trailers
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
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
    ScriptOverCapError,
    emit_script,
    resolve_agent_type_host,
)
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    RUN_ID_PREFIX,
    PipelineEmitRefused,
    PipelineInputs,
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


def _refuse_foreign_emission(output_path: Path, script: str, session_id: str) -> None:
    """Refuse to overwrite an existing emission whose bytes differ from ours,
    unless its receipt names ``session_id``: re-emitting your own output is
    not a foreign write.

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
    mtime = datetime.fromtimestamp(output_path.stat().st_mtime).isoformat(timespec="seconds")
    raise ForeignEmissionError(
        f"{output_path} already holds a DIFFERENT emission "
        f"(written {mtime}, {len(existing)} bytes; ours is {len(ours)} bytes) "
        "-- refusing to overwrite. Another session emitted this plan against a "
        "different plan state; firing or resuming this path would run a wave "
        "map neither session generated. Coordinate with that session, name a "
        "different output_path, or pass force=true deliberately."
    )


class FiredDriftError(ValueError):
    """Raised by ``guard_against_fired_drift`` when a peer overwrote the
    deterministic emission path after this caller's emit returned but
    before its fire read the bytes back.
    """


def _script_path_under(guarded_path: Path, root: str) -> Optional[str]:
    """``guarded_path`` as the repo-relative forward-slash path
    ``dispatch.terminal_commit`` takes as ``script_path``; None if not under ``root``."""
    try:
        return guarded_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def _terminal_commit_script_path(
    guarded_path: Path, repo_root: Optional[Path], plan_path: str, target_root: str
) -> Optional[str]:
    """Repo-root-relative ``script_path`` for ``dispatch.terminal_commit``.

    ``target_root`` defaults to the output's own parent directory when neither
    the request nor the caller names one, so relativising against it alone
    emits a bare basename that terminal_commit (which resolves against the
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
            "output_path", "target_root", "preamble", "preamble_path", "preamble_sha256",
            "inventory_repo_root", "pipeline", "brief", "scratch_dir", "part", "baton", "deliverable_id",
        )
    ),
    Field("inventory_part", "list"),
    Field("queue", "list"),
    Field("overrides", "dict"),
    Field("writes", "str_list"),
    Field("review_only_rows", "str_list"),
    Field("run_base_sha", "str"),
    Field("flags", "dict"),
    Field("lists", "dict"),
)


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


def _pipeline_scratch_rel(root: Path, scratch_dir: Optional[str], run_id: str) -> str:
    """Repo-relative POSIX scratch dir: the caller's, guarded under ``root``, or the per-run default."""
    if not scratch_dir:
        return f"{RUN_DIR_ROOT}/{run_id}"
    given = Path(scratch_dir)
    guarded = contained_path(given if given.is_absolute() else Path(root) / given, [Path(root)])
    if guarded is None:
        raise PathEscapeError(f"scratch_dir escapes repo root: {scratch_dir!r} not under {Path(root).as_posix()!r}")
    return guarded.relative_to(Path(root).resolve()).as_posix()


def _refuse_inventory_outside_repo(inventory_path: str, repo_root) -> None:
    """A plan row's `spec path` resolves against the inventory's own
    `<repo>/state/mise-inventory/` grandparent; an inventory anywhere else
    expands no plan and emits a script of one-line stubs."""
    inventory = Path(inventory_path).resolve()
    root = Path(repo_root).resolve() if repo_root else _repo_root_for_plan(str(inventory))
    if root is None:
        return
    inventory_home = Path(home_dir(str(root), "mise-inventory"))
    if inventory.parent != inventory_home:
        raise InventoryOutsideRepoError(
            f"inventory {inventory_path!r} is not under {inventory_home}; "
            "plan specs resolve against that directory's repo, so this emission "
            "would carry no plan chunks. Move the record there, or pass --repo-root."
        )


@register_op("dispatch.emit")
def _dispatch_emit(
    params: dict,
    repo_root: Optional[Path] = None,
    *,
    only_review_plans: Optional[frozenset] = None,
) -> dict:
    """JSON-RPC "dispatch.emit" handler.

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
        raw_subjects = params.get("subjects")
        subjects = () if raw_subjects is None else tuple(subjects_from_value(raw_subjects, base=pipeline_root))
        pipeline_ctx = {
            "root": pipeline_root,
            "run_id": run_id,
            "inputs": PipelineInputs(
                brief=brief,
                subjects=subjects,
                scratch_dir=_pipeline_scratch_rel(pipeline_root, params.get("scratch_dir"), run_id),
                flags=dict(params.get("flags") or {}),
                lists=dict(params.get("lists") or {}),
            ),
        }
        if not aliased_param(params, "output_path", "out_path"):
            params = {**params, "output_path": str(pipeline_root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs")}
        repo_root = repo_root or pipeline_root

    if ask or sizing_path:
        if plan_path or inventory_path or queue or profile_name:
            raise SizingPathConflictError(
                "dispatch.emit accepts ask/sizing_path alone, not with plan_path/"
                f"inventory_path/queue/profile (got ask={ask!r}, sizing_path={sizing_path!r})"
            )
        if not sizing_path and not (isinstance(ask, str) and ask):
            raise ValueError("dispatch.emit ask requires a prompt string or sizing_path")
        sizing_rel: Optional[str] = None
        if sizing_path:
            ask_root = _sizing_root(params, repo_root, str(sizing_path))
            sizing_rel = _sizing_rel(ask_root, str(sizing_path))
            ask_baton = _read_baton_ids(
                ask_root, params.get("baton"), params.get("deliverable_id")
            )
            ask_sizing = _gate_sizing_at_emit(
                ask_root, sizing_rel, list(params.get("writes") or []), baton=ask_baton
            )
        else:
            given_root = repo_root or params.get("target_root")
            if not given_root:
                raise ValueError("dispatch.emit ask requires repo_root or target_root")
            ask_root = Path(given_root)
            ask_baton = _read_baton_ids(
                ask_root, params.get("baton"), params.get("deliverable_id")
            )
        run_id = f"ask-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        prompt = ask if isinstance(ask, str) and ask else None
        ask_ctx = {"root": ask_root, "prompt": prompt, "sizing_rel": sizing_rel, "run_id": run_id}
        if ask_baton is not None:
            ask_ctx["baton"] = ask_baton
        if sizing_path:
            ask_ctx.update(ask_sizing)
            receipt_extras = {"batons": ask_sizing["batons"], "uncommitted": ask_sizing["uncommitted"]}
        if not aliased_param(params, "output_path", "out_path"):
            default_out = ask_root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
            default_out.parent.mkdir(parents=True, exist_ok=True)
            params = {**params, "output_path": str(default_out)}
        repo_root = repo_root or ask_root
        if sizing_path:
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
    if not is_queue_route and ask_ctx is None and pipeline_ctx is None:
        if inventory_path:
            _refuse_inventory_outside_repo(
                inventory_path, params.get("inventory_repo_root") or repo_root
            )
            part = params.get("inventory_part")
            resume_skipped: list = []
            spine_text, spine_path = mint_spine(
                inventory_path,
                max_rows=params.get("max_rows") or DEFAULT_MAX_INVENTORY_ROWS,
                part=(int(part[0]), int(part[1])) if part else None,
                skip_landed=bool(params.get("skip_landed")),
                skipped_out=resume_skipped,
            )
            guarded_spine_path = contained_path(
                spine_path, [Path(inventory_path).resolve().parent]
            )
            if guarded_spine_path is None:
                raise PathEscapeError(
                    f"minted spine path escapes its inventory record's directory: "
                    f"{spine_path!r} not under {Path(inventory_path).resolve().parent!r}"
                )
            guarded_spine_path.write_text(spine_text, encoding="utf-8", newline="\n")
            plan_path = str(guarded_spine_path)
            landed_reconciled = reconcile_landed(Path(inventory_path))
            inventory_review_specs = _inventory_review_specs(
                Path(inventory_path), only_review_plans
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
        )
        script = emission.script
        receipt_extras = {**emission.receipt_extras, **(receipt_extras or {})}
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
        script = compose_pipeline_script(
            manifest,
            pipeline_inputs,
            schedule,
            run_id=pipeline_ctx["run_id"],
            agent_type_host=agent_type_host,
        )
        receipt_extras = {
            **(receipt_extras or {}),
            "pipeline": pipeline_name,
            "run_id": pipeline_ctx["run_id"],
            "brief": pipeline_ctx["inputs"].brief,
            "manifest_sha256": manifest.sha256,
            "subjects": [subject_key(s) for s in pipeline_ctx["inputs"].subjects],
        }
        receipt_plan_path = None
    elif ask_ctx is not None:
        from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script

        review_roster_fragment, review_stage_schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
        script = compose_ask_script(
            review_roster_fragment=review_roster_fragment,
            review_stage_schemas=review_stage_schemas,
            repo_root=Path(ask_ctx["root"]).as_posix(),
            prompt=ask_ctx["prompt"],
            sizing_rel=ask_ctx["sizing_rel"],
            run_id=ask_ctx["run_id"],
            session_id=emitting_session_id,
            script_path=_script_path_under(guarded_path, ask_ctx["root"]),
            plan_blitz_args=ask_ctx.get("plan_blitz_args"),
            writes=ask_ctx.get("writes", ()),
            agent_type_host=agent_type_host,
            baton=ask_ctx.get("baton"),
            accept_pending=bool(ask_ctx.get("accept_pending")),
        )
        receipt_plan_path = None
    else:
        # AC22: the plan route loads the roster fragment and DoE's stage
        # schemas itself, through the existing content-root resolution
        # -- never a caller-supplied fragment param, and never a guessed
        # roster on an unresolvable sibling root (refuses instead).
        if not inventory_path:
            _refuse_unapproved_body(plan_path)
        review_roster_fragment, review_stage_schemas = _load_review_inputs(review_route)
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
            script_path=_terminal_commit_script_path(guarded_path, repo_root, plan_path, target_root),
            findings_out=plan_findings,
            landed_rows=frozenset(params.get("landed_rows") or ()),
            review_only_rows=frozenset(params["review_only_rows"]) if params.get("review_only_rows") is not None else None,
            run_base_sha=params.get("run_base_sha"),
            chatty=bool(params.get("chatty")),
            predispatch=bool(inventory_path),
            review_specs=inventory_review_specs,
            credit_rows=rows_backed_before_base,
        )

    check_agent_types_resolve(
        script,
        claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT"),
        agent_type_host=agent_type_host,
    )

    findings = [*run_checks(script), *plan_findings]
    error_count = sum(1 for f in findings if f.severity is Severity.ERROR)
    warn_count = sum(1 for f in findings if f.severity is Severity.WARN)

    # newline="" suppresses the platform line-ending translation Python's text
    # mode applies by default: on Windows that rewrites every "\n" to "\r\n",
    # and a CRLF-carrying .mjs is rejected by the harness Workflow surface that
    # fires it (control characters in the approval payload), making an emitted
    # script unfireable on the platform this repo treats as first-class.
    if not params.get("force"):
        _refuse_foreign_emission(guarded_path, script, emitting_session_id)

    guarded_path.parent.mkdir(parents=True, exist_ok=True)
    guarded_path.write_text(script, encoding="utf-8", newline="")

    # Digested once, reused for both the reply and the receipt -- never a
    # second `read_bytes()` of the file this call just wrote. Computed
    # best-effort, same as the receipt write below: a caller reading the
    # bytes right back off disk failing is exactly as unlikely as the
    # receipt write failing, and this must not turn into a NEW way for an
    # otherwise-successful emit to raise (`_write_emission_receipt`'s own
    # "never fails the emit" contract, which this reuses rather than
    # narrows). `None` here degrades `reply["sha256"]` to `None` too -- a
    # `--fire`/`workflow.fire` caller holding a `None` digest cannot use
    # the fired-drift guard, the same "lost the evidence, not the
    # emission" tradeoff the receipt already makes.
    try:
        script_sha256 = _script_sha256(guarded_path)
    except OSError as exc:
        print(
            f"dispatch.emit: wrote {guarded_path} but could not digest it "
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
    # The receipt outlives stdout: a later reader (a resumed EM, a peer)
    # sees the emit-time findings only if they are recorded here.
    receipt = _write_emission_receipt(
        guarded_path,
        receipt_plan_path,
        params,
        extras={**(receipt_extras or {}), "findings": finding_dicts},
        sha256=script_sha256,
    )

    reply = {
        "path": str(guarded_path),
        "receipt": receipt,
        # The bytes-on-disk digest, same value the receipt's own "sha256"
        # key carries when the receipt wrote successfully. A `--fire`
        # caller (or `workflow.fire`'s `expected` param) compares THIS
        # against the script it is about to fire, never the receipt file,
        # which is best-effort and may be `None`
        # (`_write_emission_receipt`'s own negative-spec).
        "sha256": script_sha256,
        "ok": error_count == 0,
        "findings": finding_dicts,
        "error_count": error_count,
        "warn_count": warn_count,
    }

    if receipt_extras and NOT_ADMITTED_EXTRA_KEY in receipt_extras:
        reply[NOT_ADMITTED_EXTRA_KEY] = receipt_extras[NOT_ADMITTED_EXTRA_KEY]

    if ask_ctx is not None:
        reply["run_id"] = ask_ctx["run_id"]
        if "batons" in ask_ctx:
            reply["batons"] = ask_ctx["batons"]
            reply["uncommitted"] = ask_ctx["uncommitted"]

    if pipeline_ctx is not None:
        reply["run_id"] = pipeline_ctx["run_id"]
        reply["brief"] = pipeline_ctx["inputs"].brief
        reply["scratch_dir"] = pipeline_ctx["inputs"].scratch_dir

    if inventory_path:
        reply["landed_reconciled"] = landed_reconciled
        if params.get("skip_landed"):
            reply["resume_skipped_landed"] = resume_skipped

    if not is_queue_route:
        anchor_root = repo_root or (
            ask_ctx["root"] if plan_path is None else _repo_root_for_plan(plan_path)
        )
        if anchor_root is not None:
            reply["fire_args"] = {"repoRoot": Path(anchor_root).as_posix()}

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


def _inventory_review_specs(inventory: Path, only: Optional[frozenset]) -> list:
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
    from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
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
    handoff_id = read_fm_field(fm, "handoff_id")
    baton_dlv = read_fm_field(fm, "deliverable_id")
    if not handoff_id or handoff_id == "null":
        raise SizingFireRefused([f"baton {rel} carries no handoff_id"])
    if not baton_dlv or baton_dlv == "null":
        raise SizingFireRefused([f"baton {rel} carries a null deliverable_id"])
    if deliverable_id is not None and deliverable_id != baton_dlv:
        raise SizingFireRefused(
            [f"--deliverable-id {deliverable_id!r} differs from baton {rel}'s {baton_dlv!r}"]
        )
    return {"path": rel, "deliverable_id": baton_dlv}


def _gate_sizing_at_emit(
    root: Path, sizing_rel: str, writes: list, baton: Optional[dict] = None
) -> dict:
    """Run the in-run gate and the footprint check before any script is composed.

    Returns ``plan_blitz_args``, ``writes``, ``batons`` and ``uncommitted`` for the emit, plus
    ``baton`` and ``accept_pending`` when they apply; raises ``SizingFireRefused`` on any halt.
    A ``touchpoint`` halt on a sizing whose mode is in ``APM_ADMISSIBLE_MODES`` is non-fatal:
    the script embeds the accept phase and re-gates in-run. In-process only; ``uncommitted``
    is derived from whether the sizing already named a baton, never from git.
    """
    from coordinator_core.ops.dispatch_emit import plan_blitz_args
    from coordinator_core.ops.dispatch_emit.ask_gate import gate
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_TOUCHPOINT
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import paths_outside_repo_root
    from coordinator_core.ops.dispatch_emit.sizing_fire import (
        ARM_M_PLUS,
        ARM_ROADMAP,
        SizingFireRefused,
        SizingHandBack,
        load_sizing,
        resolve_arm,
    )
    from coordinator_core.ops.sizing_acceptance import APM_ADMISSIBLE_MODES
    from coordinator_core.warm.caller_context import resolve_caller_context

    outside = paths_outside_repo_root(writes, root)
    if outside:
        raise SizingFireRefused(
            [f"writes outside repo root {Path(root).as_posix()}: {', '.join(outside)}"]
        )
    sizing = load_sizing(root, sizing_rel)
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
        and sizing.get("interaction_mode") in APM_ADMISSIBLE_MODES
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
    out: dict = {"writes": writes, "batons": [], "uncommitted": []}
    if baton:
        out["baton"] = baton
    if accept_pending:
        out["accept_pending"] = True
    if verdict.arm in (ARM_M_PLUS, ARM_ROADMAP):
        plugin_root = resolve_caller_context().plugin_root
        out["plan_blitz_args"] = plan_blitz_args.resolve(
            plugin_root=Path(plugin_root) if plugin_root else None,
            engine_root=Path(__file__).resolve().parents[3],
            sizing_abs=(Path(root) / sizing_rel).as_posix(),
        )
        if not out["plan_blitz_args"].get("provisionSidecarCli"):
            # plan-blitz refuses every baton without it, so a script emitted here would halt
            # every run with "no ready plan". Fail at emit, never later.
            raise SizingFireRefused(
                [
                    "provisionSidecarCli unresolved: no provision-sidecar launcher under the "
                    "settings home, on PATH, or at <engine>/coordinator/bin/provision-sidecar.py; "
                    "plan-blitz would refuse every baton. Install the coordinator settings home "
                    "or set COORDINATOR_SETTINGS_HOME, then re-emit."
                ]
            )
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


def _sizing_rel(root: Path, sizing_path: str) -> str:
    """``sizing_path`` as a root-relative forward-slash path; a path outside
    ``root`` is returned as given and refused by ``load_sizing`` containment."""
    p = Path(sizing_path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            return sizing_path
    return p.as_posix()

