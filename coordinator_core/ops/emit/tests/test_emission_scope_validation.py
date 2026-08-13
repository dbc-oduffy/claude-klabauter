"""Unit tests for the emission-scope projection-validation pass in validate.py.

Purpose: verify ``validate._validate_emission_scope`` (D25 §6 — cockpit-contract v2.14.0
``ScopedEmission`` standalone projection type) constructs an EPHEMERAL {scope, repo,
provenance} projection per per-repo record and validates THAT projection, never the
record itself. Records must stay byte-identical at every version — this is not merely a
below-gate no-op; the mechanism never mutates a record, period. Version gate: below
(2, 14, 0) the whole walk is skipped (no records even read for this purpose); at
>= 2.14.0 the walk activates and fails loud on a malformed repo/provenance.

No golden-fixture dependency — all envelopes are synthetic in-memory objects, mirroring
the idiom in test_content_hash_stamp.py (injected schema_version strings). Note: the
vendored pin in THIS repo state is 2.14.0 (C2 landed) — the vendored
``emission-scope.schema.json`` is present on disk, so tests that reach a valid per-repo
projection exercise the real jsonschema-backed path when ``jsonschema`` happens to be
installed, not a pure structural-fallback-in-isolation. Tests that specifically pin down
degrade-branch or structural-fallback-only behavior monkeypatch
``VENDOR_EMISSION_SCOPE_SCHEMA`` explicitly rather than relying on ambient vendor-tree
absence.

Spec backlink: pln-claude-klabauter-emission-scope-conforma-1f0dbb § C1, D25 §6.
"""

from __future__ import annotations

import copy

import pytest

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.envelope import _empty_skeleton
from coordinator_core.ops.emit.validate import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_provenance(repo: str = "test/repo") -> dict:
    """A structurally valid provenance-envelope dict (per the vendored schema shape)."""
    return {
        "source_kind": "local_fs",
        "repo": repo,
        "ref": None,
        "path": "some/record.md",
        "observed_at": "2026-07-13T00:00:00Z",
        "derivation": "parsed",
        "entity_anchor": None,
    }


def _make_record(repo: str = "test/repo", provenance: dict | None = None) -> dict:
    """Minimal per-repo work-state record carrying both `repo` and `provenance`."""
    return {
        "id": "test-record",
        "repo": repo,
        "provenance": provenance if provenance is not None else _valid_provenance(repo),
    }


def _envelope_with_handoff(record: dict) -> dict:
    envelope = _empty_skeleton("2.13.0", "2026-07-13T00:00:00Z", "test-host")
    envelope["handoffs"].append(record)
    return envelope


# ---------------------------------------------------------------------------
# (a) Byte-identity — records never mutated, at 2.13.0 AND injected 2.14.0.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schema_version", ["2.13.0", "2.14.0"])
def test_records_byte_identical_before_and_after(schema_version: str) -> None:
    """No `scope` key (or any other mutation) ever appears on a record, at any version."""
    record = _make_record()
    envelope = _envelope_with_handoff(record)
    before = copy.deepcopy(envelope)

    validate._validate_emission_scope(envelope, ctx=None, schema_version=schema_version)

    assert envelope == before, "envelope must be byte-identical after the validation pass"
    assert "scope" not in envelope["handoffs"][0], "no `scope` key must ever be stamped onto a record"


def test_narrative_views_untouched() -> None:
    """narrative_views stays null/deferred (D25 §5) — never read or written by this pass."""
    record = _make_record()
    envelope = _envelope_with_handoff(record)
    assert envelope["narrative_views"] is None

    validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")

    assert envelope["narrative_views"] is None


# ---------------------------------------------------------------------------
# (b) At injected 2.14.0, every per-repo record's projection validates.
# ---------------------------------------------------------------------------

def test_valid_per_repo_record_passes_at_2_14_0() -> None:
    """A well-formed per-repo record's ephemeral projection validates without raising."""
    record = _make_record()
    envelope = _envelope_with_handoff(record)

    # Must not raise.
    validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


def test_nested_dotpath_backlogs_bug_is_reached() -> None:
    """Records under a nested dot-path (backlogs.bug) are walked and validated too."""
    envelope = _empty_skeleton("2.13.0", "2026-07-13T00:00:00Z", "test-host")
    envelope["backlogs"]["bug"].append(_make_record())

    # Must not raise — nested dot-path record is reached and validates cleanly.
    validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


# ---------------------------------------------------------------------------
# (c) Malformed repo/provenance: fail-loud at >= 2.14.0, silent no-op at 2.13.0.
# ---------------------------------------------------------------------------

def test_malformed_provenance_fails_loud_at_2_14_0() -> None:
    """A record with a malformed provenance (missing required keys) raises ValidationError."""
    bad_provenance = {"source_kind": "local_fs"}  # missing repo/ref/path/observed_at/derivation
    record = _make_record(provenance=bad_provenance)
    envelope = _envelope_with_handoff(record)

    with pytest.raises(ValidationError):
        validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


def test_missing_repo_is_silently_skipped_not_a_hard_fail() -> None:
    """A record missing `repo` entirely is not a per-repo record — skipped, not raised.

    Per the task spec, the walk only considers records carrying BOTH repo and
    provenance; a record missing `repo` falls outside this pass's scope entirely (it is
    simply not walked as a per-repo candidate), consistent with _stamp_content_hash's own
    graceful-skip-if-absent posture.
    """
    record = {"id": "test-record", "provenance": _valid_provenance()}
    envelope = _envelope_with_handoff(record)

    # Must not raise — record has no `repo`, so it is not considered a per-repo candidate.
    validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


def test_malformed_record_is_noop_at_2_13_0() -> None:
    """The same malformed record that fails loud at 2.14.0 is a silent no-op at 2.13.0."""
    bad_provenance = {"source_kind": "local_fs"}
    record = _make_record(provenance=bad_provenance)
    envelope = _envelope_with_handoff(record)

    # Must NOT raise — below the (2, 14, 0) gate, the pass reads nothing.
    validate._validate_emission_scope(envelope, ctx=None, schema_version="2.13.0")


def test_empty_repo_string_is_silently_skipped_at_2_14_0() -> None:
    """A record with repo == '' (present but empty) is not walked — silently skipped, not raised."""
    record = _make_record(repo="")
    envelope = _envelope_with_handoff(record)

    # An empty-string repo means the record is not walked as carrying a "non-empty str"
    # repo (per the task spec's `repo (non-empty str)` predicate) — treated the same as
    # a missing repo: skipped, not raised.
    validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


# ---------------------------------------------------------------------------
# (d) ref-null-iff-source_kind conditional — the schema's `allOf` rule (Review:
# code-reviewer — slice2-F2 coverage gap).
# ---------------------------------------------------------------------------

def test_ref_none_with_pointer_source_kind_fails_loud_at_2_14_0() -> None:
    """source_kind requiring a real ref (e.g. git_commit) with ref=None fails loud."""
    bad_provenance = {
        "source_kind": "git_commit",
        "repo": "test/repo",
        "ref": None,  # violates: git_commit MUST carry a non-null ref.
        "path": "some/record.md",
        "observed_at": "2026-07-13T00:00:00Z",
        "derivation": "raw",
    }
    record = _make_record(provenance=bad_provenance)
    envelope = _envelope_with_handoff(record)

    with pytest.raises(ValidationError):
        validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


def test_ref_non_null_with_no_pointer_source_kind_fails_loud_at_2_14_0() -> None:
    """source_kind requiring a null ref (e.g. local_fs) with a non-null ref fails loud."""
    bad_provenance = {
        "source_kind": "local_fs",
        "repo": "test/repo",
        "ref": {"branch": "main", "sha": "abc123"},  # violates: local_fs MUST have ref=None.
        "path": "some/record.md",
        "observed_at": "2026-07-13T00:00:00Z",
        "derivation": "parsed",
    }
    record = _make_record(provenance=bad_provenance)
    envelope = _envelope_with_handoff(record)

    with pytest.raises(ValidationError):
        validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


# ---------------------------------------------------------------------------
# (e) Compiled-validator equivalence proof (perf fix — jsonschema validator is now
# compiled+cached ONCE via ``_compiled_scoped_emission_validator`` instead of
# ``jsonschema.validate`` recompiling the schema on every record). This proves the
# jsonschema-ONLY branch (beyond what ``_provenance_shape_is_valid``'s structural check
# enforces) still actually runs and still rejects a bad record post-caching — a caching
# bug that silently skipped validation would look like "faster + still green" without
# this case, since every OTHER failing-projection test above is already caught by the
# structural check before jsonschema is ever reached.
# ---------------------------------------------------------------------------

def test_jsonschema_only_ref_shape_violation_fails_loud_at_2_14_0() -> None:
    """A non-null `ref` missing the required `sha` key passes the structural check (which
    only checks null-vs-non-null, not `ref`'s nested shape) but must still fail the
    jsonschema-backed check against the compiled validator's `{branch, sha}` object shape —
    proving the compiled/cached validator (not just the structural fallback) is still live
    and rejects invalid records after the per-record recompile was removed.
    """
    bad_provenance = {
        "source_kind": "git_commit",
        "repo": "test/repo",
        "ref": {"branch": "main"},  # missing required `sha` — invalid under the schema's
                                    # {branch, sha} object shape, but non-null so the
                                    # structural ref-null-iff-source_kind check passes.
        "path": "some/record.md",
        "observed_at": "2026-07-13T00:00:00Z",
        "derivation": "raw",
        "entity_anchor": None,
    }
    record = _make_record(provenance=bad_provenance)
    envelope = _envelope_with_handoff(record)

    with pytest.raises(ValidationError):
        validate._validate_emission_scope(envelope, ctx=None, schema_version="2.14.0")


def test_compiled_validator_is_reused_across_calls() -> None:
    """The compiled validator for the real vendored schema path is cached, not rebuilt.

    Calling ``_compiled_scoped_emission_validator`` twice with the same schema path must
    return the IDENTICAL object (``is``, not just ``==``) — proving the lru_cache memo is
    actually hit on the second call, which is the whole point of the perf fix.
    """
    first = validate._compiled_scoped_emission_validator(validate.VENDOR_EMISSION_SCOPE_SCHEMA)
    second = validate._compiled_scoped_emission_validator(validate.VENDOR_EMISSION_SCOPE_SCHEMA)
    assert first is second


# ---------------------------------------------------------------------------
# Degrade branches in _validate_scoped_emission_per_repo (Review: code-reviewer —
# slice2-F3 coverage gap): ImportError degrade and malformed-schema-file degrade.
# ---------------------------------------------------------------------------

def test_degrades_to_structural_check_when_schema_file_is_malformed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A malformed (non-JSON) vendored schema file degrades to the structural check."""
    bad_schema_path = tmp_path / "emission-scope.schema.json"
    bad_schema_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(validate, "VENDOR_EMISSION_SCOPE_SCHEMA", bad_schema_path)

    # A structurally VALID projection still passes — degrade path, not a hard fail.
    projection = {
        "scope": "per-repo",
        "repo": "test/repo",
        "provenance": _valid_provenance(),
    }
    assert validate._validate_scoped_emission_per_repo(projection) is True


def test_structural_fallback_governs_when_vendor_schema_file_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When the vendored schema path does not exist, the pure structural check governs."""
    absent_path = tmp_path / "does-not-exist" / "emission-scope.schema.json"
    monkeypatch.setattr(validate, "VENDOR_EMISSION_SCOPE_SCHEMA", absent_path)

    valid_projection = {
        "scope": "per-repo",
        "repo": "test/repo",
        "provenance": _valid_provenance(),
    }
    assert validate._validate_scoped_emission_per_repo(valid_projection) is True

    # The structural check's ref-conditional still catches an inconsistent pairing even
    # with the vendor schema file absent — proving the structural fallback carries real
    # teeth on its own, not just key-presence.
    inconsistent_provenance = {
        "source_kind": "git_commit",
        "repo": "test/repo",
        "ref": None,
        "path": "some/record.md",
        "observed_at": "2026-07-13T00:00:00Z",
        "derivation": "raw",
    }
    inconsistent_projection = {
        "scope": "per-repo",
        "repo": "test/repo",
        "provenance": inconsistent_provenance,
    }
    assert validate._validate_scoped_emission_per_repo(inconsistent_projection) is False


# ---------------------------------------------------------------------------
# Version gate — malformed/short version strings are treated as below-gate no-ops.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_version", ["", "notasemver", "2.14", "2"])
def test_version_gate_malformed_versions_are_noop(bad_version: str) -> None:
    """Malformed or short version strings must be treated as below the gate (no-op)."""
    bad_provenance = {"source_kind": "local_fs"}
    record = _make_record(provenance=bad_provenance)
    envelope = _envelope_with_handoff(record)

    # Must NOT raise, even though the provenance is malformed — the version gate short-
    # circuits before any record is read.
    validate._validate_emission_scope(envelope, ctx=None, schema_version=bad_version)
