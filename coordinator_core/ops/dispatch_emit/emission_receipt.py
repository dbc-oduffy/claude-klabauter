"""coordinator_core.ops.dispatch_emit.emission_receipt -- the emission receipt's path, digest and
writer, and the review-input loader every emit route shares.

A leaf on purpose: `reverify_delivery` writes the same receipt and loads the same review
inputs, and importing them from `op` would put `op`'s whole import closure behind every importer
of `reverify_delivery` (`review_stamp`, `review_mint.share_stages`).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.emit import NoReviewStageError
from coordinator_core.ops.review_mint import op as review_mint_op
from coordinator_core.ops.review_mint.roster import RosterFragmentError, require_emit_route
from coordinator_core.session.core import resolve_session_id

# Generator-provenance: writes the receipt beside a caller-supplied, path-guarded script path --
# no fixed target, purely caller-named (same disposition as dispatch_emit/op.py).
GENERATES = []


def _load_review_inputs(route: str) -> tuple:
    """Return ``(fragment, stage_schemas)`` for ``route``; raise
    ``NoReviewStageError`` when either input is unloadable or the fragment's
    ``required_for_emit`` omits the route."""
    try:
        fragment = review_mint_op.load_fragment()
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise NoReviewStageError(f"review roster fragment unloadable ({exc})") from exc
    try:
        stage_schemas = review_mint_op.load_stage_schemas()
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise NoReviewStageError(str(exc)) from exc
    try:
        require_emit_route(fragment, route)
    except RosterFragmentError as exc:
        raise NoReviewStageError(str(exc)) from exc
    return fragment, stage_schemas


def emission_receipt_path(guarded_script_path: Path) -> Path:
    """The provenance sidecar's path for an ALREADY-GUARDED script path.

    ``<script>.mjs`` -> ``<script>.mjs.emitted.json``, beside the script: the
    script's own path is the only key the emitter and the fire-leg verifier
    share, so a registry keyed on it would be a second thing to keep in step
    with a file that already exists. Same rule as coordinator-content-repo's
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
    sha256: Optional[str] = None,
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

    ``sha256``, when supplied, is used verbatim instead of a second
    ``_script_sha256`` call -- the caller (``_dispatch_emit``) already
    computed it once for its own reply (``"sha256"`` key, the digest a
    ``--fire``/``workflow.fire`` caller checks against before firing) and
    passes that same value through rather than reading the file twice.
    ``None`` (every pre-existing caller) computes it here, unchanged from
    before this parameter existed.

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
            "sha256": sha256 if sha256 is not None else _script_sha256(guarded_script_path),
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
