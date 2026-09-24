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
                                     repo (DoE-claude) than the one whose
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

    session_id (str, optional)    — recorded verbatim in the provenance
                                     receipt. Absent, the fleet-canonical
                                     ``session.core.resolve_session_id``
                                     ladder answers (bound request identity,
                                     ``COORDINATOR_SESSION_ID``,
                                     ``CLAUDE_SESSION_ID``,
                                     ``CLAUDE_CODE_SESSION_ID``); absent all
                                     of those, the empty string. NEVER
                                     minted — see ``_receipt_session_id``.

Reply fields:
    {"path": "<written path>", "ok": bool,
     "findings": [{"severity","code","message","line"?}, ...],
     "error_count": int, "warn_count": int,
     "receipt": "<written receipt path>" | None,
     "fire_args": {"repoRoot": "<posix root>"} | absent}
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
    DoE-claude's wrapper CLI ``coordinator/bin/emit-dispatch-workflow.py``
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
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.emit import emit_script, resolve_agent_type_host
from coordinator_core.ops.dispatch_emit.inventory_mint import mint_spine
from coordinator_core.ops.dispatch_emit.queue_emit import QueuePathEscapeError, emit_queue_script
from coordinator_core.session.core import resolve_session_id
from coordinator_core.ops._param_alias import aliased_param, spellings


# Generator-provenance: writes the emitted script to a caller-supplied,
# path-guarded output_path -- no fixed target, purely caller-named.
GENERATES = []


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


def _refuse_foreign_emission(output_path: Path, script: str) -> None:
    """Refuse to overwrite an existing emission whose bytes differ from ours.

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
    mtime = datetime.fromtimestamp(output_path.stat().st_mtime).isoformat(timespec="seconds")
    raise ForeignEmissionError(
        f"{output_path} already holds a DIFFERENT emission "
        f"(written {mtime}, {len(existing)} bytes; ours is {len(ours)} bytes) "
        "-- refusing to overwrite. Another session emitted this plan against a "
        "different plan state; firing or resuming this path would run a wave "
        "map neither session generated. Coordinate with that session, name a "
        "different output_path, or pass force=true deliberately."
    )


def emission_receipt_path(guarded_script_path: Path) -> Path:
    """The provenance sidecar's path for an ALREADY-GUARDED script path.

    ``<script>.mjs`` -> ``<script>.mjs.emitted.json``, beside the script: the
    script's own path is the only key the emitter and the fire-leg verifier
    share, so a registry keyed on it would be a second thing to keep in step
    with a file that already exists. Same rule as DoE-claude's
    ``emit-dispatch-workflow.py :: emission_receipt_path`` — the two producers
    must agree on where the sidecar lands, not just on what is in it.

    Negative-spec:
      - Does NOT accept the caller's raw ``output_path``. The argument is the
        value ``contained_path`` returned; ``with_name`` on an unguarded input
        would place a write outside ``target_root``, which is the escape the
        guard exists to stop.
      - Does NOT create directories or probe the tree — pure path arithmetic.
    """
    return guarded_script_path.with_name(guarded_script_path.name + ".emitted.json")


def _script_sha256(guarded_script_path: Path) -> str:
    """Digest of the script's RAW BYTES as they landed on disk.

    Negative-spec: NEVER ``read_text()``. Universal-newline decoding collapses
    a CRLF ``.mjs`` to LF, so a text-mode digest disagrees with the verifier on
    a file nobody edited. The DoE-side consumer hashes ``read_bytes()``
    (``emit-dispatch-workflow.py :: script_sha256``); anything writing a
    receipt has to hash the same way or it writes a digest that reads as
    tampering.
    """
    return hashlib.sha256(guarded_script_path.read_bytes()).hexdigest()


def _receipt_session_id(params: dict) -> str:
    """Who to record as the emitter: the caller's claim, or the fleet-canonical
    resolution, or "".

    Precedence: an explicit ``session_id`` param, then
    ``coordinator_core.session.core.resolve_session_id`` — the fleet's ONE
    session-identity resolver, which is the bound per-request identity
    ContextVar, then ``COORDINATOR_SESSION_ID``, then ``CLAUDE_SESSION_ID``,
    then ``CLAUDE_CODE_SESSION_ID``, then "".

    Reusing that resolver rather than reading an env var here is the whole
    point: a narrow read is what produced the bug this function exists to fix.
    A receipt written from a ``CLAUDE_SESSION_ID``-only read carried
    ``"session_id": ""`` on a host where ``CLAUDE_CODE_SESSION_ID`` was the
    populated tier (measured 2026-09-17, this container), and the fire gate
    then REFUSED the emission — "its receipt names session ; this session is
    7f8efcbc". Any fourth private copy of the ladder re-opens exactly that
    hole the next time a tier is added.

    Negative-spec:
      - Does NOT invent, mint, derive or uuid-generate an id. A phantom session
        id in a provenance record is worse than an absent one: it attributes an
        emission to a session that never ran, which is the false attribution
        the receipt exists to prevent. All tiers unset yields "", and the
        receipt says so honestly.
      - Does NOT re-derive the tier list. ``SESSION_ENV_PRECEDENCE`` lives in
        ``session.core`` and is read through ``resolve_session_id``, never
        copied.
    """
    claimed = params.get("session_id")
    if claimed:
        return str(claimed)
    return resolve_session_id() or ""


def _write_emission_receipt(
    guarded_script_path: Path,
    plan_path: Optional[str],
    params: dict,
    extras: Optional[dict] = None,
) -> Optional[str]:
    """Write the provenance sidecar. Best-effort: never fails the emit.

    The script is the deliverable; the receipt is evidence ABOUT it. An
    unwritable receipt location (read-only directory, a directory sitting where
    the sidecar goes, a full disk) must return the same successful verdict as
    before this op wrote receipts at all — losing the evidence is a degradation,
    losing the emission is a regression. The failure is narrated on stderr and
    surfaced as a ``None`` ``receipt`` key in the reply, so it is visible rather
    than silent.

    ``plan_path`` is ``None`` on the queue route -- ``"plan"`` is then written
    as ``null``, and ``extras`` (``queue_emit.QueueEmission.receipt_extras``)
    is merged in under the same serialisation, never a forked one (module
    docstring § The receipt is a property of emitting).

    Returns the receipt path as a string, or ``None`` if it could not be
    written.

    Negative-spec:
      - Does NOT re-raise. Every ``OSError`` (and an unserialisable value) is
        swallowed after narration.
      - Does NOT mutate or re-read the script. On a ``force`` overwrite the
        sidecar is rewritten whole, so its ``sha256`` names the bytes now on
        disk rather than a superseded emission's.
      - Does NOT let ``extras`` shadow the receipt's own core keys
        (``sha256``/``session_id``/``emitted_at``/``plan``) -- those are set
        first and ``extras`` is merged in after, but ``queue_emit`` never
        emits those key names in its own extras dict, so no collision occurs
        in practice.
    """
    receipt_path = emission_receipt_path(guarded_script_path)
    try:
        receipt = {
            "sha256": _script_sha256(guarded_script_path),
            "session_id": _receipt_session_id(params),
            "emitted_at": datetime.now().isoformat(timespec="seconds"),
            "plan": Path(plan_path).name if plan_path else None,
        }
        if extras:
            receipt.update(extras)
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except (OSError, TypeError, ValueError) as exc:
        print(
            f"dispatch.emit: wrote {guarded_script_path} but could not write its "
            f"provenance receipt {receipt_path} ({exc}). The script stands; it "
            "carries no emission provenance.",
            file=sys.stderr,
        )
        return None
    return str(receipt_path)


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

    Mirrors DoE-claude's wrapper CLI ``emit-dispatch-workflow.py :: restamp``
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
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return receipt


@register_op("dispatch.emit")
def _dispatch_emit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.emit" handler.

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
        force (bool, optional, default False): overwrite an ``output_path``
            that already holds a different session's emission. Off by
            default -- see ``ForeignEmissionError``.
        session_id (str, optional): emitter identity recorded in the
            provenance receipt; falls back to the fleet-canonical
            ``session.core.resolve_session_id`` ladder, then to "". Never
            minted -- see ``_receipt_session_id``.

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
    plan_path = aliased_param(params, "plan_path", "plan")
    inventory_path = params.get("inventory_path")
    queue = params.get("queue")
    profile_name = params.get("profile")
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

    if not is_queue_route:
        if inventory_path:
            spine_text, spine_path = mint_spine(inventory_path)
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

        if not plan_path:
            raise ValueError(f"dispatch.emit requires param: {spellings('plan_path', 'plan')}")

    output_path = aliased_param(params, "output_path", "out_path")
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
        claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT"),
    )

    receipt_extras: Optional[dict] = None
    receipt_plan_path: Optional[str] = plan_path

    if is_queue_route:
        if not queue:
            raise ValueError("dispatch.emit queue route requires param: queue")
        if not profile_name:
            raise ValueError("dispatch.emit queue route requires param: profile")
        profile_dir = params.get("profile_dir")
        if not profile_dir:
            raise ValueError("dispatch.emit queue route requires param: profile_dir")

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
        )
        script = emission.script
        receipt_extras = emission.receipt_extras
        receipt_plan_path = None
    else:
        script = emit_script(
            plan_path,
            name=params.get("name"),
            description=params.get("description"),
            repo_root=repo_root or _repo_root_for_plan(plan_path),
            session_id=emitting_session_id,
            agent_type_host=agent_type_host,
        )

    findings = run_checks(script)
    error_count = sum(1 for f in findings if f.severity is Severity.ERROR)
    warn_count = sum(1 for f in findings if f.severity is Severity.WARN)

    # newline="" suppresses the platform line-ending translation Python's text
    # mode applies by default: on Windows that rewrites every "\n" to "\r\n",
    # and a CRLF-carrying .mjs is rejected by the harness Workflow surface that
    # fires it (control characters in the approval payload), making an emitted
    # script unfireable on the platform this repo treats as first-class.
    if not params.get("force"):
        _refuse_foreign_emission(guarded_path, script)

    guarded_path.write_text(script, encoding="utf-8", newline="")

    receipt = _write_emission_receipt(
        guarded_path, receipt_plan_path, params, extras=receipt_extras
    )

    reply = {
        "path": str(guarded_path),
        "receipt": receipt,
        "ok": error_count == 0,
        "findings": [
            {
                "severity": f.severity.value,
                "code": f.code,
                "message": f.message,
                **({"line": f.line} if f.line is not None else {}),
            }
            for f in findings
        ],
        "error_count": error_count,
        "warn_count": warn_count,
    }

    if not is_queue_route:
        anchor_root = repo_root or _repo_root_for_plan(plan_path)
        if anchor_root is not None:
            reply["fire_args"] = {"repoRoot": Path(anchor_root).as_posix()}

    return reply
