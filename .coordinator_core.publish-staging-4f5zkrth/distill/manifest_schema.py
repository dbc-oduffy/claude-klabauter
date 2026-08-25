"""
coordinator_core.distill.manifest_schema — schema module for distill-ceremony
scope/disposal/curation manifests (schema_version 1).

Purpose: pure-schema module (pattern: coordinator_core/ops/ceremony/receipt_schema.py)
defining the shape, factory helpers, and structural validation for the JSON
artifacts the distill-ceremony op family (C10-C14) reads and writes:

  - scope-manifest        — distill.scope's Workflow INPUT JSON (run_id, batches,
                             wikiDirs, wikiSlugs, cohort annotations).
  - disposal-manifest      — distill.assemble_disposal_manifest's per-file guard
                             receipts, gains the stamp field-group once
                             distill.stamp_disposal writes it.
  - curation-status        — distill.curation_status's derived ledger
                             (state/ceremony/curation-status.json).

This module owns STRUCTURE and VALIDATION only — it performs no disk I/O and no
business logic (harvest/ripeness/guard computation stays in the op modules that
compose coordinator_core/distill/*, per DEC-5). Every manifest shape below carries
``schema_version`` as its FIRST key; an unknown FORWARD version (newer than this
module knows) is a fail-loud consumption error, a matching (or older, known)
version is silent — mirrors DoE's own disposal-manifest schema_version precedent
(artifact-distillation-harvest.md, itself mirroring DR-082).

Canonical-body sha: the disposal manifest is JSON, not markdown-with-frontmatter,
so "canonical body" is pinned explicitly here (F3):
  (a) the stamp field-group (``disposal_authorized_*``) is EXCLUDED from the
      hashed serialization — otherwise a re-stamp check that compares the sha
      against itself would be circular (stamping would perturb the hash it is
      checked against);
  (b) canonical serialization is ``json.dumps(..., sort_keys=True,
      separators=(",", ":"))`` so two processes hash identical bytes regardless
      of dict insertion order or platform-default separator whitespace.

Negative-spec: this module performs no writes, no subprocess calls, and makes no
PM-authorization or guard-verdict judgment calls — it only shapes and validates
the dicts the op tier assembles and stamps.

Spec backlink: pln-makima-driven-ceremony-redesig-c7fe9a § C9
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "STAMP_FIELDS",
    "VALID_VERDICTS",
    "ManifestSchemaError",
    "make_scope_manifest",
    "validate_scope_manifest",
    "make_guard_receipt",
    "make_disposal_row",
    "make_scan_stats",
    "make_disposal_manifest",
    "validate_disposal_manifest",
    "make_stamp",
    "apply_stamp",
    "stamp_complete",
    "stamp_absent",
    "stamp_partial",
    "make_curation_status",
    "validate_curation_status",
    "canonical_manifest_bytes",
    "compute_manifest_sha",
    "check_schema_version",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
"""Schema version integer for every manifest shape in this module — always the
first key written on disk. Not semver; mirrors receipt_schema.py's convention."""

#: The four-field disposal-authorization stamp, byte-identical in spirit to
#: handoff.stamp_phase's execution_authorized_{by,at,sha,note} convention
#: (DEC-2) — same all-four-or-none posture, same refuse-value-injection intent.
STAMP_FIELDS: tuple[str, ...] = (
    "disposal_authorized_by",
    "disposal_authorized_at",
    "disposal_authorized_sha",
    "disposal_authorized_note",
)

#: Valid values for a per-guard receipt's ``verdict`` field.
VALID_VERDICTS: frozenset[str] = frozenset({"pass", "block"})

#: Required top-level fields for a scope-manifest.
_SCOPE_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "batches",
    "wiki_dirs",
    "wiki_slugs",
    "cohorts",
)

#: Required per-guard-receipt fields.
_GUARD_RECEIPT_REQUIRED: tuple[str, ...] = ("guard", "verdict", "evidence")

#: Required per-file disposal-row fields (retention_reason is OPTIONAL — present
#: only on retained/non-eligible rows; log_row is always present, possibly "").
_DISPOSAL_ROW_REQUIRED: tuple[str, ...] = (
    "path",
    "artifact_class",
    "guards_run",
    "eligible",
    "log_row",
)

#: Required scan_stats fields.
_SCAN_STATS_REQUIRED: tuple[str, ...] = (
    "total_scanned",
    "eligible_count",
    "retained_count",
)

#: Required top-level fields for a disposal-manifest (BEFORE stamping — the
#: STAMP_FIELDS group is additive, written later by distill.stamp_disposal).
_DISPOSAL_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "rows",
    "scan_stats",
    "mass_throttle",
)

#: Required per-artifact fields in a curation-status ledger entry.
_CURATION_ARTIFACT_REQUIRED: tuple[str, ...] = (
    "harvested",
    "ripe",
    "prunable",
    "blocked_by",
    "last_touched",
)

#: Required top-level fields for a curation-status artifact.
_CURATION_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "generated_at",
    "artifacts",
    "unharvested_ripe_count",
    "last_run_id",
    "last_run_age_seconds",
    "prunable",
)


class ManifestSchemaError(Exception):
    """Raised by check_schema_version on an unknown forward schema_version."""


# ---------------------------------------------------------------------------
# scope-manifest (distill.scope Workflow INPUT JSON — C10)
# ---------------------------------------------------------------------------


def make_scope_manifest(
    run_id: str,
    batches: list[list[str]],
    wiki_dirs: list[str],
    wiki_slugs: list[str],
    cohorts: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Return a schema-valid scope-manifest dict.

    ``batches`` is a list of chronological file-path batches (~20-50 files each,
    per C10). ``cohorts`` is an open dict keyed by cohort name (e.g. "harvest",
    "skip", "sidecars" per DEC-4) -> list of rel-posix paths; defaults to {}
    when the caller has none yet (graceful-absent, always key-present).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "batches": [list(batch) for batch in batches],
        "wiki_dirs": list(wiki_dirs),
        "wiki_slugs": list(wiki_slugs),
        "cohorts": dict(cohorts) if cohorts is not None else {},
    }


def validate_scope_manifest(manifest: dict[str, Any]) -> list[str]:
    """Validate a scope-manifest dict. Returns a (possibly empty) error list."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"scope-manifest must be a dict; got {type(manifest).__name__}"]

    for field_name in _SCOPE_REQUIRED:
        if field_name not in manifest:
            errors.append(f"required field missing: {field_name!r}")
    if errors:
        return errors

    if not isinstance(manifest["run_id"], str) or not manifest["run_id"]:
        errors.append("run_id must be a non-empty string")
    if not isinstance(manifest["batches"], list):
        errors.append("batches must be a list")
    else:
        for idx, batch in enumerate(manifest["batches"]):
            if not isinstance(batch, list):
                errors.append(f"batches[{idx}] must be a list")
    if not isinstance(manifest["wiki_dirs"], list):
        errors.append("wiki_dirs must be a list")
    if not isinstance(manifest["wiki_slugs"], list):
        errors.append("wiki_slugs must be a list")
    if not isinstance(manifest["cohorts"], dict):
        errors.append("cohorts must be a dict")
    return errors


# ---------------------------------------------------------------------------
# disposal-manifest (distill.assemble_disposal_manifest — C12)
# ---------------------------------------------------------------------------


def make_guard_receipt(guard: str, verdict: str, evidence: Any = "") -> dict[str, Any]:
    """Return a schema-valid per-guard receipt dict.

    ``verdict`` must be "pass" or "block" (VALID_VERDICTS). ``evidence`` is
    free-form (str or dict) — whatever the guard's own check function returns
    as its detail (mirrors delete_guard.GuardResult.detail).
    """
    return {"guard": guard, "verdict": verdict, "evidence": evidence}


def make_disposal_row(
    path: str,
    artifact_class: str,
    guards_run: list[dict[str, Any]],
    eligible: bool,
    retention_reason: str | None = None,
    log_row: str = "",
) -> dict[str, Any]:
    """Return a schema-valid per-file disposal-manifest row.

    ``retention_reason`` is OPTIONAL (key present only when supplied) — a
    retained (non-eligible) row is expected to carry one (validated below); an
    eligible row normally omits it.
    """
    row: dict[str, Any] = {
        "path": path,
        "artifact_class": artifact_class,
        "guards_run": list(guards_run),
        "eligible": eligible,
        "log_row": log_row,
    }
    if retention_reason is not None:
        row["retention_reason"] = retention_reason
    return row


def make_scan_stats(
    total_scanned: int, eligible_count: int, retained_count: int
) -> dict[str, int]:
    """Return a schema-valid scan_stats dict."""
    return {
        "total_scanned": total_scanned,
        "eligible_count": eligible_count,
        "retained_count": retained_count,
    }


def make_disposal_manifest(
    run_id: str,
    rows: list[dict[str, Any]],
    scan_stats: dict[str, int],
    mass_throttle: bool = False,
) -> dict[str, Any]:
    """Return a schema-valid disposal-manifest dict (pre-stamp).

    ``mass_throttle`` is the manifest-level flag C12 sets when the
    eligible/total ratio (or a zero-total-scanned edge case) crosses its named
    module-constant threshold (F2) — this module only carries the field, the
    threshold logic itself lives in the C12 op module.

    The STAMP_FIELDS group is NOT included here — distill.stamp_disposal
    (C13) adds it additively via apply_stamp() at PM-approval time.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "rows": list(rows),
        "scan_stats": dict(scan_stats),
        "mass_throttle": mass_throttle,
    }


def validate_disposal_manifest(manifest: dict[str, Any]) -> list[str]:
    """Validate a disposal-manifest dict (pre- or post-stamp).

    Structural checks only: required fields present + typed; a retained
    (eligible=False) row MUST carry a non-empty retention_reason (AC3 —
    no bare "eligible: false" without a reason); guards_run must be non-empty
    (AC3 — no bare "eligible: true" without receipts) and each receipt's
    verdict must be one of VALID_VERDICTS. STAMP_FIELDS, when present, are
    validated as all-four-or-none (partial stamps are a schema error).
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"disposal-manifest must be a dict; got {type(manifest).__name__}"]

    for field_name in _DISPOSAL_REQUIRED:
        if field_name not in manifest:
            errors.append(f"required field missing: {field_name!r}")
    if errors:
        return errors

    if not isinstance(manifest["run_id"], str) or not manifest["run_id"]:
        errors.append("run_id must be a non-empty string")
    if not isinstance(manifest["mass_throttle"], bool):
        errors.append("mass_throttle must be a bool")

    scan_stats = manifest["scan_stats"]
    if not isinstance(scan_stats, dict):
        errors.append("scan_stats must be a dict")
    else:
        for field_name in _SCAN_STATS_REQUIRED:
            if field_name not in scan_stats:
                errors.append(f"scan_stats missing field: {field_name!r}")
            elif not isinstance(scan_stats[field_name], int):
                errors.append(f"scan_stats.{field_name} must be an int")

    rows = manifest["rows"]
    if not isinstance(rows, list):
        errors.append("rows must be a list")
    else:
        for idx, row in enumerate(rows):
            errors.extend(_validate_disposal_row(row, idx))

    if errors:
        return errors

    present_stamp_fields = [f for f in STAMP_FIELDS if f in manifest]
    if present_stamp_fields and len(present_stamp_fields) != len(STAMP_FIELDS):
        missing = sorted(set(STAMP_FIELDS) - set(present_stamp_fields))
        errors.append(
            "partial disposal-authorization stamp: present "
            f"{sorted(present_stamp_fields)}, missing {missing} "
            "(all-four-or-none)"
        )

    return errors


def _validate_disposal_row(row: Any, idx: int) -> list[str]:
    """Validate one disposal-manifest row. Returns prefixed error strings."""
    errors: list[str] = []
    if not isinstance(row, dict):
        return [f"rows[{idx}] must be a dict; got {type(row).__name__}"]

    for field_name in _DISPOSAL_ROW_REQUIRED:
        if field_name not in row:
            errors.append(f"rows[{idx}] missing field: {field_name!r}")
    if errors:
        return errors

    if not isinstance(row["path"], str) or not row["path"]:
        errors.append(f"rows[{idx}].path must be a non-empty string")
    if not isinstance(row["artifact_class"], str) or not row["artifact_class"]:
        errors.append(f"rows[{idx}].artifact_class must be a non-empty string")
    if not isinstance(row["eligible"], bool):
        errors.append(f"rows[{idx}].eligible must be a bool")
    if not isinstance(row["log_row"], str):
        errors.append(f"rows[{idx}].log_row must be a string")

    guards_run = row["guards_run"]
    if not isinstance(guards_run, list) or not guards_run:
        errors.append(
            f"rows[{idx}].guards_run must be a non-empty list "
            "(AC3 — no bare eligible without receipts)"
        )
    else:
        for gidx, receipt in enumerate(guards_run):
            if not isinstance(receipt, dict):
                errors.append(f"rows[{idx}].guards_run[{gidx}] must be a dict")
                continue
            for field_name in _GUARD_RECEIPT_REQUIRED:
                if field_name not in receipt:
                    errors.append(
                        f"rows[{idx}].guards_run[{gidx}] missing field: {field_name!r}"
                    )
            if receipt.get("verdict") not in VALID_VERDICTS:
                errors.append(
                    f"rows[{idx}].guards_run[{gidx}].verdict must be one of "
                    f"{sorted(VALID_VERDICTS)}; got {receipt.get('verdict')!r}"
                )

    if row["eligible"] is False:
        reason = row.get("retention_reason")
        if not reason or not isinstance(reason, str):
            errors.append(
                f"rows[{idx}]: retained (eligible=False) row must carry a "
                "non-empty retention_reason"
            )

    return errors


# ---------------------------------------------------------------------------
# Stamp field-group (DEC-2 — mirrors handoff.phase_stamp's execution_authorized_*)
# ---------------------------------------------------------------------------


def make_stamp(by: str, at: str, sha: str, note: str) -> dict[str, str]:
    """Return the four-field disposal_authorized_* stamp dict."""
    return {
        "disposal_authorized_by": by,
        "disposal_authorized_at": at,
        "disposal_authorized_sha": sha,
        "disposal_authorized_note": note,
    }


def apply_stamp(manifest: dict[str, Any], by: str, at: str, sha: str, note: str) -> dict[str, Any]:
    """Return a NEW manifest dict with the stamp field-group applied (additive).

    Does not mutate the input dict. Callers (distill.stamp_disposal, C13) are
    responsible for computing ``sha`` via compute_manifest_sha() over the
    UN-stamped manifest and for the all-four-or-none / idempotent-when-equal /
    refuse-on-drift semantics — this helper only shapes the merge.
    """
    stamped = dict(manifest)
    stamped.update(make_stamp(by, at, sha, note))
    return stamped


def stamp_complete(manifest: dict[str, Any]) -> bool:
    """True if all four STAMP_FIELDS are present and non-empty."""
    return all(manifest.get(f) for f in STAMP_FIELDS)


def stamp_absent(manifest: dict[str, Any]) -> bool:
    """True if none of the four STAMP_FIELDS are present."""
    return not any(f in manifest for f in STAMP_FIELDS)


def stamp_partial(manifest: dict[str, Any]) -> bool:
    """True if SOME but not all STAMP_FIELDS are present — an invalid state."""
    present = [f for f in STAMP_FIELDS if f in manifest]
    return 0 < len(present) < len(STAMP_FIELDS)


# ---------------------------------------------------------------------------
# curation-status artifact (distill.curation_status --emit — C11)
# ---------------------------------------------------------------------------


def make_curation_status(
    run_id: str,
    generated_at: str,
    artifacts: dict[str, dict[str, Any]],
    unharvested_ripe_count: int,
    last_run_id: str | None,
    last_run_age_seconds: float | None,
    prunable: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a schema-valid curation-status dict.

    ``artifacts`` is keyed by rel-posix path -> {harvested, ripe, prunable,
    blocked_by, last_touched}. ``prunable`` is a list of {path, reasons: [...]}
    dicts (the prunable-set-with-reasons view, DEC-1/AC4). ``last_run_id`` /
    ``last_run_age_seconds`` describe the last DISTILL run recorded in the
    canonical distillation log (`state/distillation-log.md`) — NOT the last time
    this artifact itself was emitted (2026-07-23 fix; see
    `coordinator_core.ops.distill_curation_status._last_distill_run`). Both are
    None when the log is absent or has no parseable rows yet (graceful-absent —
    key always present, value may be null).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "artifacts": dict(artifacts),
        "unharvested_ripe_count": unharvested_ripe_count,
        "last_run_id": last_run_id,
        "last_run_age_seconds": last_run_age_seconds,
        "prunable": list(prunable),
    }


def validate_curation_status(manifest: dict[str, Any]) -> list[str]:
    """Validate a curation-status dict. Returns a (possibly empty) error list."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"curation-status must be a dict; got {type(manifest).__name__}"]

    for field_name in _CURATION_REQUIRED:
        if field_name not in manifest:
            errors.append(f"required field missing: {field_name!r}")
    if errors:
        return errors

    if not isinstance(manifest["run_id"], str) or not manifest["run_id"]:
        errors.append("run_id must be a non-empty string")
    if not isinstance(manifest["unharvested_ripe_count"], int):
        errors.append("unharvested_ripe_count must be an int")
    if manifest["last_run_id"] is not None and not isinstance(manifest["last_run_id"], str):
        errors.append("last_run_id must be a string or null")
    age = manifest["last_run_age_seconds"]
    if age is not None and not isinstance(age, (int, float)):
        errors.append("last_run_age_seconds must be a number or null")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict):
        errors.append("artifacts must be a dict")
    else:
        for path, entry in artifacts.items():
            if not isinstance(entry, dict):
                errors.append(f"artifacts[{path!r}] must be a dict")
                continue
            for field_name in _CURATION_ARTIFACT_REQUIRED:
                if field_name not in entry:
                    errors.append(f"artifacts[{path!r}] missing field: {field_name!r}")

    prunable = manifest["prunable"]
    if not isinstance(prunable, list):
        errors.append("prunable must be a list")
    else:
        for idx, entry in enumerate(prunable):
            if not isinstance(entry, dict):
                errors.append(f"prunable[{idx}] must be a dict")
                continue
            if "path" not in entry:
                errors.append(f"prunable[{idx}] missing field: 'path'")
            if "reasons" not in entry:
                errors.append(f"prunable[{idx}] missing field: 'reasons'")
            elif not isinstance(entry["reasons"], list):
                errors.append(f"prunable[{idx}].reasons must be a list")

    return errors


# ---------------------------------------------------------------------------
# Canonical serialization + sha (F3) + schema_version consumption gate
# ---------------------------------------------------------------------------


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Return the canonical byte serialization used for sha computation.

    Excludes the STAMP_FIELDS group (F3 — hashing the stamp would make the
    idempotent-when-equal re-stamp check in distill.stamp_disposal circular:
    stamping would perturb the hash it is checked against). Uses
    ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` so two
    processes on either platform hash identical bytes regardless of dict
    insertion order or platform default separator whitespace.
    """
    body = {k: v for k, v in manifest.items() if k not in STAMP_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_manifest_sha(manifest: dict[str, Any]) -> str:
    """Return the sha256 hex digest of the manifest's canonical body (F3)."""
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def check_schema_version(manifest: dict[str, Any], *, known_version: int = SCHEMA_VERSION) -> None:
    """Fail-loud gate for manifest consumption (schema_version discipline).

    Raises ManifestSchemaError if schema_version is missing, non-int, or
    NEWER than ``known_version`` (an unknown forward version this code
    cannot safely interpret). A matching (or older, already-known) version
    passes silently — no return value, no side effect.
    """
    version = manifest.get("schema_version")
    if version is None:
        raise ManifestSchemaError("manifest missing required field: 'schema_version'")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ManifestSchemaError(
            f"schema_version must be an int; got {type(version).__name__}: {version!r}"
        )
    if version > known_version:
        raise ManifestSchemaError(
            f"manifest schema_version={version} is newer than this module's "
            f"known SCHEMA_VERSION={known_version} — refusing to consume "
            "(unknown-forward-version fail-loud)"
        )
