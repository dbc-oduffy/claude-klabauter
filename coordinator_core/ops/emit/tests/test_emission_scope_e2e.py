"""AC6 end-to-end conformance test for the emission-scope projection-validation pass.

Complements the unit-level ``test_emission_scope_validation.py`` (synthetic in-memory
envelopes) with a WIRED emit over a fixture repo through the live vendored bundle. With the
2.14.0 re-vendor landed (C2), ``envelope.build`` runs ``validate._validate_emission_scope``
for real — so a successful ``emit()`` is itself a conformance proof (the pass fails loud on
any non-validating per-repo projection). The explicit assertions below make the three D25 §6
guarantees checkable rather than implicit:

  (i)   every emitted record is byte-identical to a scope-free record — no ``scope`` key (or
        any other mutation) is ever written onto a record (records are NOT ScopedEmission
        instances; ScopedEmission is a standalone projection type).
  (ii)  the ephemeral ``{scope, repo, provenance}`` projection of every per-repo record
        validates against ScopedEmission's per-repo case.
  (iii) ``narrative_views`` stays null/untouched (D25 §5 — no producer, meaning undefined).

A guard assertion pins ``read_schema_version() >= (2, 14, 0)`` so this test fails loud rather
than false-greening if it is ever run against a pre-2.14.0 pin (where the pass no-ops and (ii)
would be vacuous).

Spec backlink: pln-claude-klabauter-emission-scope-conforma-1f0dbb § C4 / AC6, D25 §6.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import emit


_SLUG = "fixture-owner/emission-scope-e2e"


def _make_ctx(repo_root: Path, slug: str = _SLUG) -> EmitContext:
    """Minimal EmitContext for a fixture repo (no live git remote required).

    Mirrors the factory in test_per_repo_emission_integration.py — central_state_root =
    repo_root/state per the 2026-07-07 per-repo cutover.
    """
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch="main",
        git_sha="0000000011111111222222223333333344444444",
        git_sha_short="00000000",
        observed_at="2026-07-13T10:00:00Z",
        hostname="fixture.emission-scope.e2e",
        repo_name=slug,
    )


def _version_tuple() -> "tuple[int, ...]":
    return tuple(int(x) for x in validate.read_schema_version().split("."))


def _iter_records(envelope: dict):
    """Yield (dotpath, record) for every SECMAP record array present in the envelope.

    Uses validate's own SECMAP dot-path set + read semantics, so the walk matches exactly
    what the validation pass walks (excludes malformed_records / narrative_views / scalars).
    """
    for dotpath in validate._record_dotpaths_from_secmap():
        try:
            records = validate._get_dotpath_value(envelope, dotpath)
        except KeyError:
            # Section absent from this envelope -- nothing to yield, not a test failure.
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict):
                yield dotpath, record


def _deep_has_scope_key(obj) -> bool:
    """True if a ``scope`` key appears anywhere in the nested structure."""
    if isinstance(obj, dict):
        if "scope" in obj:
            return True
        return any(_deep_has_scope_key(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_deep_has_scope_key(v) for v in obj)
    return False


@pytest.mark.usefixtures("requires_vendor_pin")
class TestEmissionScopeConformanceE2E:
    """End-to-end: a real emit over a fixture repo conforms to D25 §6 emission-scope."""

    def _emit(self, tmp_path: Path) -> dict:
        repo = tmp_path / "emission-scope-e2e-repo"
        (repo / "state").mkdir(parents=True)
        out = tmp_path / "emission.json"
        emit(_make_ctx(repo), out=out)
        return json.loads(out.read_text())

    def test_gate_is_active_at_current_pin(self) -> None:
        """Guard against false-green: the vendored pin must be >= 2.14.0 for (ii) to bite."""
        assert _version_tuple() >= (2, 14, 0), (
            f"emission-scope e2e requires a >= 2.14.0 vendored pin (got "
            f"{validate.read_schema_version()!r}); below 2.14.0 the validation pass no-ops "
            "and the per-repo projection assertion is vacuous."
        )

    def test_emit_succeeds_and_records_carry_no_scope_key(self, tmp_path: Path) -> None:
        """(i) A successful emit at 2.14.0 proves conformance; no record carries a scope key."""
        data = self._emit(tmp_path)
        for dotpath, record in _iter_records(data):
            assert "scope" not in record, (
                f"record at {dotpath!r} carries a 'scope' key — records must stay "
                "byte-identical; ScopedEmission is a standalone projection, not an entity key."
            )

    def test_per_repo_projections_validate(self, tmp_path: Path) -> None:
        """(ii) Every per-repo record's ephemeral projection validates against ScopedEmission."""
        data = self._emit(tmp_path)
        checked = 0
        checked_dotpaths: set[str] = set()
        for dotpath, record in _iter_records(data):
            repo = record.get("repo")
            provenance = record.get("provenance")
            if not (isinstance(repo, str) and repo and isinstance(provenance, dict)):
                continue
            projection = {"scope": "per-repo", "repo": repo, "provenance": provenance}
            assert validate._validate_scoped_emission_per_repo(projection), (
                f"per-repo projection for record at {dotpath!r} (repo={repo!r}) failed "
                "ScopedEmission per-repo validation"
            )
            checked += 1
            checked_dotpaths.add(dotpath)
        # coordinator_roots always self-reports at least one per-repo record (see
        # sections/coordinator_roots.py:collect) — that is the floor this fixture-repo
        # emit is guaranteed to hit even with no handoffs/plans/lessons on disk. Any
        # OTHER SECMAP dot-path checked here is a bonus signal from this specific fixture
        # shape, not a guaranteed floor — checked_dotpaths documents which ones actually
        # contributed so a reader doesn't have to re-derive the guarantee from
        # coordinator_roots.py.
        assert checked >= 1, (
            f"expected at least one per-repo record to validate; checked_dotpaths="
            f"{checked_dotpaths!r}"
        )

    def test_narrative_views_untouched(self, tmp_path: Path) -> None:
        """(iii) narrative_views stays null (D25 §5 — no producer, no speculative keying)."""
        data = self._emit(tmp_path)
        assert data.get("narrative_views") is None, (
            f"narrative_views must stay null; got {data.get('narrative_views')!r}"
        )

    def test_no_scope_key_anywhere_in_envelope(self, tmp_path: Path) -> None:
        """Belt-and-suspenders on (i): no 'scope' key appears anywhere in the emission."""
        data = self._emit(tmp_path)
        assert not _deep_has_scope_key(data), (
            "a 'scope' key leaked into the emission — the projection-validation pass must "
            "never write scope back onto any record or envelope node."
        )
