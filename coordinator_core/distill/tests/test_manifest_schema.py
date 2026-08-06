"""
coordinator_core.distill.tests.test_manifest_schema

Tests for the distill-ceremony scope/disposal/curation manifest schema module
(schema_version 1).

Coverage:
  (a) schema_version_shape   — SCHEMA_VERSION is an int, first key on every make_* output
  (b) scope_roundtrip        — make_scope_manifest -> json dump/load -> validate clean
  (c) disposal_roundtrip     — make_disposal_manifest -> json dump/load -> validate clean
  (d) curation_roundtrip     — make_curation_status -> json dump/load -> validate clean
  (e) disposal_ac3_receipts  — eligible row with empty guards_run is a validation error
  (f) disposal_ac3_reason    — retained (eligible=False) row without retention_reason errors
  (g) disposal_verdict_enum  — an out-of-enum verdict is a validation error
  (h) stamp_sha_stability    — sha is IDENTICAL before/after a stamp-field-only edit (F3)
  (i) stamp_sha_drift        — sha CHANGES when a body field changes
  (j) stamp_all_or_none      — partial stamp (some but not all fields) is a validation error
  (k) stamp_helpers          — stamp_complete/stamp_absent/stamp_partial classify correctly
  (l) canonical_bytes_stable — canonical_manifest_bytes is stable under dict key-order shuffle
  (m) version_forward_fail   — check_schema_version raises on a newer-than-known version
  (n) version_match_silent   — check_schema_version is a silent no-op on a matching version
  (o) version_missing_fail   — check_schema_version raises when schema_version is absent
  (p) missing_field_errors   — each validate_* catches a missing top-level required field

Spec backlink:
  coordinator_core/distill/manifest_schema.py
  docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C9
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.distill.manifest_schema import (
    SCHEMA_VERSION,
    STAMP_FIELDS,
    ManifestSchemaError,
    apply_stamp,
    canonical_manifest_bytes,
    check_schema_version,
    compute_manifest_sha,
    make_curation_status,
    make_disposal_manifest,
    make_disposal_row,
    make_guard_receipt,
    make_scan_stats,
    make_scope_manifest,
    stamp_absent,
    stamp_complete,
    stamp_partial,
    validate_curation_status,
    validate_disposal_manifest,
    validate_scope_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _sample_scope_manifest() -> dict:
    return make_scope_manifest(
        run_id="2026-07-23-10h00",
        batches=[["docs/plans/a.md", "docs/plans/b.md"], ["docs/plans/c.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=["docs/wiki/some-guide.md"],
        cohorts={"harvest": ["docs/plans/a.md"], "sidecars": ["docs/plans/a-check.md"]},
    )


def _sample_disposal_manifest() -> dict:
    eligible_row = make_disposal_row(
        path="docs/plans/old-plan.md",
        artifact_class="plan",
        guards_run=[
            make_guard_receipt("active_reference", "pass", "no refs in docs/ tasks/ archive/"),
            make_guard_receipt("commitment_closure", "pass", "no open commitments"),
        ],
        eligible=True,
        log_row="- docs/plans/old-plan.md -> DISTILLED, wiki (run: 2026-07-23-10h00)",
    )
    retained_row = make_disposal_row(
        path="docs/plans/live-plan.md",
        artifact_class="plan",
        guards_run=[make_guard_receipt("active_reference", "block", "referenced by state/handoffs/x.md")],
        eligible=False,
        retention_reason="active-reference guard blocked",
        log_row="",
    )
    scan_stats = make_scan_stats(total_scanned=2, eligible_count=1, retained_count=1)
    return make_disposal_manifest(
        run_id="2026-07-23-10h00",
        rows=[eligible_row, retained_row],
        scan_stats=scan_stats,
        mass_throttle=False,
    )


def _sample_curation_status() -> dict:
    return make_curation_status(
        run_id="2026-07-23-10h00",
        generated_at="2026-07-23T10:00:00Z",
        artifacts={
            "docs/plans/a.md": {
                "harvested": True,
                "ripe": False,
                "prunable": True,
                "blocked_by": [],
                "last_touched": "2026-07-20T00:00:00Z",
            },
        },
        unharvested_ripe_count=3,
        last_run_id="2026-07-22-23h38",
        last_run_age_seconds=3600.0,
        prunable=[{"path": "docs/plans/a.md", "reasons": ["harvested", "actioned"]}],
    )


# ---------------------------------------------------------------------------
# (a) schema_version_shape
# ---------------------------------------------------------------------------


def test_schema_version_is_int_and_first_key():
    assert isinstance(SCHEMA_VERSION, int)
    for manifest in (_sample_scope_manifest(), _sample_disposal_manifest(), _sample_curation_status()):
        keys = list(manifest.keys())
        assert keys[0] == "schema_version"
        assert manifest["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# (b)-(d) roundtrip
# ---------------------------------------------------------------------------


def test_scope_manifest_roundtrip():
    manifest = _sample_scope_manifest()
    reloaded = json.loads(json.dumps(manifest))
    assert validate_scope_manifest(reloaded) == []


def test_disposal_manifest_roundtrip():
    manifest = _sample_disposal_manifest()
    reloaded = json.loads(json.dumps(manifest))
    assert validate_disposal_manifest(reloaded) == []


def test_curation_status_roundtrip():
    manifest = _sample_curation_status()
    reloaded = json.loads(json.dumps(manifest))
    assert validate_curation_status(reloaded) == []


# ---------------------------------------------------------------------------
# (e)-(g) AC3 structural teeth
# ---------------------------------------------------------------------------


def test_disposal_eligible_row_requires_nonempty_guards_run():
    manifest = _sample_disposal_manifest()
    manifest["rows"][0]["guards_run"] = []
    errors = validate_disposal_manifest(manifest)
    assert any("guards_run" in e and "non-empty" in e for e in errors)


def test_disposal_retained_row_requires_retention_reason():
    manifest = _sample_disposal_manifest()
    del manifest["rows"][1]["retention_reason"]
    errors = validate_disposal_manifest(manifest)
    assert any("retention_reason" in e for e in errors)


def test_disposal_guard_receipt_verdict_enum():
    manifest = _sample_disposal_manifest()
    manifest["rows"][0]["guards_run"][0]["verdict"] = "maybe"
    errors = validate_disposal_manifest(manifest)
    assert any("verdict" in e for e in errors)


# ---------------------------------------------------------------------------
# (h)-(k) stamp field-group + sha (F3)
# ---------------------------------------------------------------------------


def test_stamp_field_only_edit_preserves_sha():
    manifest = _sample_disposal_manifest()
    sha_before = compute_manifest_sha(manifest)
    stamped = apply_stamp(manifest, by="dónal", at="2026-07-23T10:05:00Z", sha=sha_before, note="approved")
    sha_after = compute_manifest_sha(stamped)
    assert sha_before == sha_after
    assert stamped["disposal_authorized_sha"] == sha_before


def test_body_field_edit_changes_sha():
    manifest = _sample_disposal_manifest()
    sha_before = compute_manifest_sha(manifest)
    mutated = dict(manifest)
    mutated["mass_throttle"] = True
    sha_after = compute_manifest_sha(mutated)
    assert sha_before != sha_after


def test_partial_stamp_is_validation_error():
    manifest = _sample_disposal_manifest()
    manifest["disposal_authorized_by"] = "dónal"
    # only one of the four fields present
    errors = validate_disposal_manifest(manifest)
    assert any("all-four-or-none" in e for e in errors)


def test_stamp_classification_helpers():
    manifest = _sample_disposal_manifest()
    assert stamp_absent(manifest)
    assert not stamp_complete(manifest)
    assert not stamp_partial(manifest)

    manifest["disposal_authorized_by"] = "dónal"
    assert stamp_partial(manifest)
    assert not stamp_absent(manifest)
    assert not stamp_complete(manifest)

    full = apply_stamp(
        _sample_disposal_manifest(), by="dónal", at="2026-07-23T10:05:00Z", sha="abc123", note="approved"
    )
    assert stamp_complete(full)
    assert not stamp_absent(full)
    assert not stamp_partial(full)


# ---------------------------------------------------------------------------
# (l) canonical bytes stability under key-order shuffle
# ---------------------------------------------------------------------------


def test_canonical_bytes_stable_under_key_order():
    manifest = _sample_disposal_manifest()
    shuffled = {k: manifest[k] for k in reversed(list(manifest.keys()))}
    assert canonical_manifest_bytes(manifest) == canonical_manifest_bytes(shuffled)


# ---------------------------------------------------------------------------
# (m)-(o) schema_version consumption gate
# ---------------------------------------------------------------------------


def test_check_schema_version_raises_on_forward_version():
    manifest = _sample_disposal_manifest()
    manifest["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ManifestSchemaError):
        check_schema_version(manifest)


def test_check_schema_version_silent_on_match():
    manifest = _sample_disposal_manifest()
    assert check_schema_version(manifest) is None  # no raise, no return value


def test_check_schema_version_raises_on_missing():
    manifest = _sample_disposal_manifest()
    del manifest["schema_version"]
    with pytest.raises(ManifestSchemaError):
        check_schema_version(manifest)


# ---------------------------------------------------------------------------
# (p) missing top-level required field caught by each validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder,validator,field_to_drop",
    [
        (_sample_scope_manifest, validate_scope_manifest, "wiki_slugs"),
        (_sample_disposal_manifest, validate_disposal_manifest, "scan_stats"),
        (_sample_curation_status, validate_curation_status, "unharvested_ripe_count"),
    ],
)
def test_missing_required_field_is_caught(builder, validator, field_to_drop):
    manifest = builder()
    del manifest[field_to_drop]
    errors = validator(manifest)
    assert any(field_to_drop in e for e in errors)
