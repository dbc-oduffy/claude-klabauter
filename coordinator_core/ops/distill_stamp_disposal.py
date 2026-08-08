"""
coordinator_core.ops.distill_stamp_disposal — JSON-RPC "distill.stamp_disposal"
operation (C13 — PM disposal-authorization stamp, DEC-2).

Purpose: writes the four-field ``disposal_authorized_{by,at,sha,note}`` stamp
group onto an already-assembled disposal-manifest (C12's
``distill.assemble_disposal_manifest`` output), in place, at the SAME path C12
wrote to (``state/scratch/artifact-distillation/<run_id>/disposal-manifest.json``)
— DR-228 § D3's "create, then in-place stamp" write shape. This is the one
PM-approval-time write in the disposal tier (DEC-2, plan § Negative spec): the
stamp is written only when an operator supplies ``by``/``at``/``note`` — never
inferred, never defaulted, never auto-triggered by any other op in this family.

DEC-2 — mirrors ``handoff.stamp_phase`` (``coordinator_core/ops/
handoff_phase_stamp.py``), not a new invention: same all-four-or-none field
group, same refuse-value-injection posture. The one structural difference:
``handoff.stamp_phase`` reads its four fields OFF a cited plan's frontmatter
(plan_path is the sole v1 value-source); this op computes ONE of its four
fields itself (``disposal_authorized_sha``) rather than reading it from
anywhere — sha is NEVER an accepted param (see Negative-spec below), because a
caller-supplied sha would let a caller assert an arbitrary binding between
stamp and manifest body, defeating the sha-drift detection this stamp exists
to provide (C9's ``compute_manifest_sha``, over the manifest's canonical body
per C9's F3 pinning — the STAMP_FIELDS group itself is EXCLUDED from that
hashed body, verified directly against ``manifest_schema.canonical_manifest_bytes``
by this op's own tests, so a re-stamp of an otherwise-unchanged manifest can
never perturb the hash it is checked against).

Idempotency / re-stamp semantics (DEC-2, full-target-state convergence —
mirrors handoff.stamp_phase's D1 posture, not "is the stamp field present?"):
  - absent stamp -> fresh stamp write (by/at/note from params, sha computed).
  - complete stamp, all four fields equal the (params..., computed-sha) target
    -> idempotent no-op (``applied: False``, no write).
  - complete stamp, ANY of by/at/note differs from the params -> refuse
    (fail-loud, no write) — "refuses re-stamp with different values" (plan body).
  - complete stamp, by/at/note all equal the params but the FRESHLY COMPUTED
    sha differs from the stored ``disposal_authorized_sha`` -> refuse
    (sha-drift detection) — the manifest body changed after it was stamped
    (e.g. a re-run of assemble_disposal_manifest, or a hand-edit), so the
    stamp no longer authorizes the ON-DISK content; re-stamping over drift
    would silently rebind an old PM approval to new content.
  - partial stamp (some but not all of the four fields present) on disk is a
    schema violation (manifest_schema.validate_disposal_manifest already
    flags this) — this op's read path fails loud on it via that same
    validator rather than attempting to "complete" a corrupt stamp.

Write shape: read-modify-write, in place, atomic (mkstemp + os.replace) over
the SAME manifest file C12 created — never a new file, never a rename. NOT a
locked_rmw/frontmatter mutation (the manifest is whole-file JSON, not
markdown+frontmatter) — this module rolls its own minimal RMW: read text,
json.loads, validate, decide, json.dumps the new whole body, atomic replace.
MUTATING (DR-208 fail-closed: any file write disqualifies COMPUTE_ONLY;
five-question affirmation block cites DR-228 § D2b(vi) — see
coordinator_core/authz/classification.py).

Negative-spec (mirrors handoff_phase_stamp.py's negative-spec section):
  - Does NOT accept ``disposal_authorized_sha`` (or any STAMP_FIELDS name) as
    an operator param — sha is ALWAYS computed by this op via
    ``manifest_schema.compute_manifest_sha``, never read off a caller-supplied
    value. A caller that passes any STAMP_FIELDS-named param gets a fail-loud
    refusal (refuse-injection), not a silently-ignored extra key.
  - Does NOT run any delete_guard check, guard re-evaluation, or delete —
    those are C12 (assemble) and C14 (apply)'s jobs respectively; this op
    only reads the already-assembled manifest and writes the stamp group.
  - Does NOT git-commit — DR-228 § D3: "assemble_disposal_manifest and
    stamp_disposal do not commit at all."
  - Does NOT create a new manifest — refuses (fail-loud) if the target
    manifest is absent on disk; this op stamps an EXISTING C12 output only.
  - Does NOT make the PM's disposal decision — the caller (a human-driven
    ceremony invocation, never an autonomous op chain) supplies by/at/note at
    explicit PM-approval time; this op has no default identity/timestamp/note.

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C13
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D2b(vi), D3
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator_core.distill import manifest_render as _render
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id

__all__ = [
    "StampDisposalResult",
    "manifest_path_for_run",
    "load_disposal_manifest",
    "stamp_disposal_manifest",
    "write_stamped_manifest",
]


def manifest_path_for_run(worktree_root: Path, run_id: str) -> Path:
    """Return the well-known disposal-manifest path for ``run_id`` — the SAME
    path C12's ``write_disposal_manifest`` writes to. This op never mints a
    different location; it stamps C12's own output in place."""
    return (
        worktree_root
        / "state"
        / "scratch"
        / "artifact-distillation"
        / run_id
        / "disposal-manifest.json"
    )


class DisposalStampError(Exception):
    """Raised on any fail-loud condition in the stamp read/decide/write path
    (manifest absent, schema-invalid, injected sha param, value-mismatch
    re-stamp, sha-drift). Callers (the op handler) translate this to a
    JSON-RPC error; library callers may catch it directly."""


def load_disposal_manifest(manifest_path: Path) -> dict[str, Any]:
    """Read + parse + schema-validate the disposal-manifest at ``manifest_path``.

    Raises DisposalStampError if the file is absent, not valid JSON, fails
    schema_version consumption (manifest_schema.check_schema_version), or
    fails structural validation (manifest_schema.validate_disposal_manifest —
    this also catches an on-disk partial stamp, since that validator already
    rejects a present-but-incomplete STAMP_FIELDS group).
    """
    if not manifest_path.is_file():
        raise DisposalStampError(
            f"disposal-manifest not found on disk: {manifest_path} — "
            "distill.stamp_disposal stamps an EXISTING distill.assemble_disposal_manifest "
            "output only; it never creates one."
        )
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DisposalStampError(f"cannot read disposal-manifest: {manifest_path}: {exc}") from exc

    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DisposalStampError(f"disposal-manifest is not valid JSON: {manifest_path}: {exc}") from exc

    try:
        _schema.check_schema_version(manifest)
    except _schema.ManifestSchemaError as exc:
        raise DisposalStampError(str(exc)) from exc

    errors = _schema.validate_disposal_manifest(manifest)
    if errors:
        raise DisposalStampError(
            f"disposal-manifest at {manifest_path} failed schema validation: {'; '.join(errors)}"
        )
    return manifest


@dataclass(frozen=True)
class StampDisposalResult:
    """Result of stamp_disposal_manifest: the manifest dict AFTER the decision
    (may be byte-identical to the input on an idempotent no-op), whether a
    write is actually required, and the computed sha (always present, even on
    a no-op, so callers can report it)."""

    manifest: dict[str, Any]
    applied: bool
    message: str
    computed_sha: str


def stamp_disposal_manifest(
    manifest: dict[str, Any],
    *,
    by: str,
    at: str,
    note: str,
) -> StampDisposalResult:
    """Decide + apply the disposal_authorized_* stamp over ``manifest`` (pure —
    no I/O). Returns a StampDisposalResult; raises DisposalStampError on any
    refuse condition (value-mismatch re-stamp, sha-drift).

    ``by``/``at``/``note`` are the three operator-supplied fields (PM-approval
    time). sha is NEVER a parameter here — always computed via
    manifest_schema.compute_manifest_sha over ``manifest``'s canonical body
    (STAMP_FIELDS-excluded, per C9/F3), so it reflects the CURRENT on-disk
    manifest body regardless of any stamp already present.
    """
    computed_sha = _schema.compute_manifest_sha(manifest)

    if _schema.stamp_absent(manifest):
        stamped = _schema.apply_stamp(manifest, by=by, at=at, sha=computed_sha, note=note)
        return StampDisposalResult(
            manifest=stamped,
            applied=True,
            message="disposal-manifest stamped (fresh)",
            computed_sha=computed_sha,
        )

    # stamp_partial is already excluded by load_disposal_manifest's schema
    # validation gate (validate_disposal_manifest rejects a present-but-
    # incomplete STAMP_FIELDS group) — by the time we're here the manifest is
    # either stamp_absent (handled above) or stamp_complete.
    assert _schema.stamp_complete(manifest), (
        "unreachable: manifest is neither stamp_absent nor stamp_complete after "
        "schema validation — validate_disposal_manifest should have rejected a "
        "partial stamp before this function was ever called"
    )

    existing = {field: manifest[field] for field in _schema.STAMP_FIELDS}
    intended = _schema.make_stamp(by=by, at=at, sha=computed_sha, note=note)

    if existing == intended:
        return StampDisposalResult(
            manifest=manifest,
            applied=False,
            message="disposal-manifest already stamped with these exact values — no-op",
            computed_sha=computed_sha,
        )

    # Value-mismatch vs. sha-drift are distinguishable and get distinct
    # messages: sha-drift means the manifest BODY changed since the existing
    # stamp was written (the by/at/note the caller is re-asserting may be
    # identical); a plain value-mismatch means the caller is asserting a
    # DIFFERENT by/at/note against an unchanged body. Both are refused —
    # neither is silently accepted or silently overwritten.
    if existing["disposal_authorized_sha"] != computed_sha:
        raise DisposalStampError(
            "refusing re-stamp: manifest body sha has DRIFTED since it was "
            f"stamped (stamped sha={existing['disposal_authorized_sha']!r}, "
            f"current computed sha={computed_sha!r}) — the manifest content "
            "changed after PM approval; re-assemble and get a fresh PM stamp, "
            "never re-stamp over drifted content"
        )
    raise DisposalStampError(
        "refusing re-stamp: manifest already carries a disposal_authorized_* "
        f"stamp with different values (existing={existing!r}, "
        f"requested by/at/note -> {intended!r}) — all-four-or-none, "
        "idempotent-when-equal, refuse-on-mismatch (DEC-2)"
    )


def write_stamped_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    """Atomically overwrite ``manifest_path`` with ``manifest`` (mkstemp +
    os.replace — create-or-full-rewrite only, same discipline as C12's
    write_disposal_manifest; this is the "in-place stamp" half of DR-228 §
    D3's "create, then in-place stamp" write shape)."""
    run_dir = manifest_path.parent
    body = json.dumps(manifest, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(manifest_path))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@register_op("distill.stamp_disposal")
def _handler(params: dict, repo_root: Path | None = None) -> dict:
    """distill.stamp_disposal handler.

    Params:
        run_id (str, REQUIRED) — locates the disposal-manifest at
            state/scratch/artifact-distillation/<run_id>/disposal-manifest.json
            (the well-known path C12's distill.assemble_disposal_manifest wrote
            to — this op never accepts a bare manifest_path, to keep the
            run_id the single addressing key across the whole disposal tier).
        by (str, REQUIRED) — operator identity (PM-approval-time param).
        at (str, REQUIRED) — ISO timestamp (PM-approval-time param).
        note (str, REQUIRED) — free-text note; also the ONLY channel by which
            a caller acknowledges a manifest-level mass_throttle flag (C12/F2)
            before C14's apply_disposal will honor a throttled manifest — this
            op itself does not enforce that acknowledgment (C14's job), it
            only carries whatever note the caller supplies.

    Refuse-injection: any of the four STAMP_FIELDS names
        (disposal_authorized_by/at/sha/note) supplied as a TOP-LEVEL param
        other than the plain by/at/note above — most notably
        ``disposal_authorized_sha`` or a bare ``sha`` — is a fail-loud
        ValueError. sha is ALWAYS computed by this op (see module docstring).

    repo_root (injected by ipc.dispatch_message): git_common_dir of the
    originating worktree (_OP_KEY_SCOPE="common_dir"). Derives
    main_worktree_root(repo_root). Fails loud when repo_root is None (no
    silent meta-repo fallback, matching C12/distill.scope's AC5 precedent).

    Returns a dict with keys:
        run_id                  (str)
        disposal_manifest_path  (str)  — rel-posix path to the (possibly
                                          rewritten) manifest.
        applied                 (bool) — True if a write happened.
        message                 (str)
        disposal_authorized_sha (str)  — always present, even on a no-op.
        rendered_manifest       (str)  — markdown preview (§ 1b) of the
                                          manifest as loaded, rendered BEFORE
                                          the stamp decision/write.
    """
    if repo_root is None:
        raise ValueError(
            "distill.stamp_disposal requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the "
            "JSON-RPC envelope. No silent fallback to meta-repo."
        )

    run_id = params.get("run_id")
    if not run_id:
        raise ValueError("distill.stamp_disposal requires an explicit run_id param.")

    by = params.get("by")
    at = params.get("at")
    note = params.get("note")
    missing = [name for name, value in (("by", by), ("at", at), ("note", note)) if not value]
    if missing:
        raise ValueError(
            f"distill.stamp_disposal requires non-empty params: {missing} "
            "(by/at/note are the PM-approval-time operator identity fields; "
            "sha is never a param — it is always computed)."
        )

    injected = [name for name in ("sha", *_schema.STAMP_FIELDS) if name in params]
    if injected:
        raise ValueError(
            "distill.stamp_disposal refuses caller-supplied sha/stamp-field "
            f"params: {sorted(injected)} — disposal_authorized_sha is ALWAYS "
            "computed by this op via manifest_schema.compute_manifest_sha; a "
            "caller-supplied sha would defeat sha-drift detection."
        )

    worktree_root = main_worktree_root(repo_root)
    manifest_path = manifest_path_for_run(worktree_root, run_id)

    try:
        manifest = load_disposal_manifest(manifest_path)
        # Rendered BEFORE the stamp decision/write (§ 1b) — this is the
        # human-legible preview of exactly what is about to be authorized,
        # computed from the manifest as loaded rather than any post-stamp
        # state (STAMP_FIELDS never affect rendering either way).
        rendered_manifest = _render.render_disposal_manifest(manifest)
        result = stamp_disposal_manifest(manifest, by=by, at=at, note=note)
    except DisposalStampError as exc:
        raise ValueError(str(exc)) from exc

    if result.applied:
        write_stamped_manifest(manifest_path, result.manifest)

    return {
        "run_id": run_id,
        "disposal_manifest_path": rel_id(manifest_path, worktree_root),
        "applied": result.applied,
        "message": result.message,
        "disposal_authorized_sha": result.computed_sha,
        "rendered_manifest": rendered_manifest,
    }
