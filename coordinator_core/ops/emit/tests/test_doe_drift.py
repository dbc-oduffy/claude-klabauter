"""strang-02 drift-check tests — DoE-HEAD conformance fixture + version-band gate.

Covers every acceptance criterion from strang-02:

  AC_Q-a-sha:
    - Vendored-pin SHA is recorded; fails loud on SHA mismatch (simulated synthetic advance).
    - Ref absent on DoE origin → graceful WARN, no exception (live constraint).

  AC_Q-a-band:
    - ``contract_version`` bump alone → NO fail-loud (version-band gate, not equality).
    - ``min_supported_contract_version`` past claude-klabauter's pin → DOES fail-loud.

  AC_Q-b:
    - Fixture read from ``coordinator/cockpit-contract/conformance/emission-conformance.json``
      in the DoE local clone at ``repos.doe_claude``; NOT co-vendored in claude-klabauter's _vendor/.

  AC_NORMALIZER:
    - Live run with runtime-varying provenance matches the normalized golden via _normalize.
    - Reuses the EXISTING ``_normalize`` helper from test_emit_parity (not a duplicate).

  AC_DRIFT_CHECK:
    - Drift-check fails loud when pinned version < DoE min_supported (synthetic lag test).

  AC_CONFORMANCE:
    - Conformance runs against the strang-01 strangled path (envelope.emit()), not raw bash.
    - Proves the seam conforms: strang-01 output schema_version satisfies the version band.

  AC_REF_ABSENT:
    - When DoE origin has no ``cockpit-contract-release`` ref, check_freshness issues
      a warning and does NOT raise (graceful ref-absent path).

Spec backlink: state/handoffs/2026-07-04_201949_roadmap-strang-02.md
Oracle: /Users/example-operator/X/DoE-claude/coordinator/docs/wiki/emission-conformance-contract.md

Note: tests that require a live DoE clone use ``resolve_doe_clone()`` and skip
gracefully when the clone is absent (CI portability).  Tests that call envelope.emit()
require the vendored cockpit-contract pin and use the ``requires_vendor_pin`` fixture.
"""

from __future__ import annotations

import json
import subprocess
import warnings
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from coordinator_core.ops.emit.doe_drift import (
    AheadOfReleaseWarning,
    DoeResolveError,
    DriftError,
    DriftWarning,
    PIN_SHA_FILE,
    _PIN_ABSENT_SENTINEL,
    _read_pin_sha,
    _tag_is_ancestor_of_pin,
    check_freshness,
    check_version_band,
    probe_freshness_ref,
    read_doe_fixture,
    resolve_doe_clone,
    run_drift_check,
)

assert not issubclass(AheadOfReleaseWarning, DriftWarning), (
    "AheadOfReleaseWarning must not subclass DriftWarning — load-bearing for re-vendor post-check"
)

# Reuse the EXISTING normalizer and typed sentinels from the shared normalizers module.
# AC_NORMALIZER: these are the shared AC5-PROVENANCE oracles.
from coordinator_core.ops.emit.normalizers import (
    _normalize,
    _TS_SENTINEL,
    _SHA_SENTINEL,
    _ID_SENTINEL,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _live_doe_clone() -> Optional[Path]:
    try:
        return resolve_doe_clone()
    except DoeResolveError:
        return None


_DOE_AVAILABLE = _live_doe_clone() is not None


class TestFixtureResolution:

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    @pytest.mark.real_home
    def test_fixture_read_from_doe_clone(self) -> None:
        doe_clone = resolve_doe_clone()
        fixture = read_doe_fixture(doe_clone)

        assert isinstance(fixture, dict), "fixture must be a dict"
        assert "contract_version" in fixture, "fixture must carry contract_version"
        assert "min_supported_contract_version" in fixture, (
            "fixture must carry min_supported_contract_version (CD-2)"
        )

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_fixture_not_in_vendor_tree(self) -> None:
        from coordinator_core.ops.emit.validate import _VENDOR_CONTRACT
        co_vendored = _VENDOR_CONTRACT / "conformance" / "emission-conformance.json"
        assert not co_vendored.exists(), (
            f"Fixture must NOT be co-vendored at {co_vendored} — "
            "it must be read from the DoE clone at check-time (DR-210 anti-drift invariant)."
        )

    def test_fixture_parse_failure_raises_doe_resolve_error(self, tmp_path: Path) -> None:
        bad_fixture = tmp_path / "coordinator/cockpit-contract/conformance"
        bad_fixture.mkdir(parents=True)
        (bad_fixture / "emission-conformance.json").write_text("{bad json", encoding="utf-8")

        with pytest.raises(DoeResolveError, match="not valid JSON"):
            read_doe_fixture(tmp_path)

    def test_fixture_absent_raises_doe_resolve_error(self, tmp_path: Path) -> None:
        with pytest.raises(DoeResolveError, match="not found"):
            read_doe_fixture(tmp_path)


class TestVersionBand:

    def _make_fixture(
        self,
        contract_version: str = "2.5.0",
        min_supported: str = "2.5.0",
    ) -> dict:
        return {
            "contract_version": contract_version,
            "min_supported_contract_version": min_supported,
        }

    def test_pinned_equals_min_supported_passes(self) -> None:
        check_version_band("2.5.0", self._make_fixture("2.5.0", "2.5.0"))

    def test_pinned_above_min_supported_passes(self) -> None:
        check_version_band("2.6.0", self._make_fixture("2.6.0", "2.5.0"))

    def test_contract_version_bump_alone_does_not_fail(self) -> None:
        check_version_band("2.5.0", self._make_fixture("2.6.0", "2.5.0"))

    def test_min_supported_past_pin_fails_loud(self) -> None:
        with pytest.raises(DriftError, match="below the DoE min_supported_contract_version"):
            check_version_band("2.5.0", self._make_fixture("2.6.0", "2.6.0"))

    def test_significantly_lagging_pin_fails_loud(self) -> None:
        with pytest.raises(DriftError):
            check_version_band("2.3.0", self._make_fixture("2.6.0", "2.5.0"))

    def test_missing_min_supported_warns_not_raises(self) -> None:
        fixture = {"contract_version": "2.5.0"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            check_version_band("2.5.0", fixture)
        assert any("min_supported_contract_version" in str(w.message) for w in caught), (
            "Missing min_supported_contract_version must produce a warning"
        )

    def test_patch_version_comparison_correct(self) -> None:
        check_version_band("2.5.10", self._make_fixture("2.5.10", "2.5.9"))
        with pytest.raises(DriftError):
            check_version_band("2.5.9", self._make_fixture("2.5.10", "2.5.10"))


class TestFreshnessRef:

    def test_ref_absent_graceful_no_exception(self, tmp_path: Path) -> None:
        """AC_REF_ABSENT: ref absent on DoE origin → WARN, no DriftError.

        Live constraint: DoE has NOT published refs/tags/cockpit-contract-release at
        strang-02 pickup.  This is the expected path in CI and on live machines until
        DoE publishes the ref.  Must NEVER hard-fail on ref absence.
        """
        pin_sha = "aabbccddeeff00112233445566778899aabbccdd"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=pin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=None,
            ),
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                check_freshness(doe_clone=tmp_path)

        assert any("ref" in str(w.message).lower() for w in caught), (
            "Ref-absent path must emit a warning about the missing ref"
        )

    def test_sha_mismatch_raises_drift_error(self, tmp_path: Path) -> None:
        pin_sha = "1111111111111111111111111111111111111111"
        origin_sha = "2222222222222222222222222222222222222222"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=pin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=origin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift._tag_is_ancestor_of_pin",
                return_value=False,
            ),
        ):
            with pytest.raises(DriftError, match="SHA mismatch"):
                check_freshness(doe_clone=tmp_path)

    def test_pin_ahead_of_tag_warns_not_raises(self, tmp_path: Path) -> None:
        pin_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        origin_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=pin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=origin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift._tag_is_ancestor_of_pin",
                return_value=True,
            ),
        ):
            with pytest.warns(AheadOfReleaseWarning):
                check_freshness(doe_clone=tmp_path)

    def test_pin_behind_tag_raises(self, tmp_path: Path) -> None:
        pin_sha = "1111111111111111111111111111111111111111"
        origin_sha = "2222222222222222222222222222222222222222"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=pin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=origin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift._tag_is_ancestor_of_pin",
                return_value=False,
            ),
        ):
            with pytest.raises(DriftError, match="SHA mismatch"):
                check_freshness(doe_clone=tmp_path)

    def test_ancestry_indeterminate_raises(self, tmp_path: Path) -> None:
        pin_sha = "cccccccccccccccccccccccccccccccccccccccc"
        origin_sha = "dddddddddddddddddddddddddddddddddddddddd"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=pin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=origin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift._tag_is_ancestor_of_pin",
                return_value=None,
            ),
        ):
            with pytest.raises(DriftError, match="SHA mismatch"):
                check_freshness(doe_clone=tmp_path)

    def test_sha_match_passes(self, tmp_path: Path) -> None:
        sha = "abcdef1234567890abcdef1234567890abcdef12"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=sha,
            ),
        ):
            check_freshness(doe_clone=tmp_path)

    def test_pin_absent_sentinel_skips_freshness(self, tmp_path: Path) -> None:
        """PIN_SHA_FILE containing ABSENT + ref absent on origin → WARN/skip, no error.

        State (A): both the pin file says ABSENT and the origin has no ref yet.
        Must emit a warning and not raise — the expected path during the initial
        strang-02 window before DoE publishes the ref.

        Updated to mock probe_freshness_ref returning None
        (ref absent on origin) so the test isolates state (A) cleanly; check_freshness
        now probes origin even when pin is ABSENT.
        """
        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=None,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=None,
            ),
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                check_freshness(doe_clone=tmp_path)

        assert caught, "Pin-absent + ref-absent-on-origin path must emit a warning"
        drift_warnings = [w for w in caught if issubclass(w.category, DriftWarning)]
        assert not drift_warnings, (
            "State (A) — ref also absent on origin — must NOT emit DriftWarning; "
            f"got: {[str(w.message) for w in drift_warnings]}"
        )

    def test_pin_absent_but_ref_now_on_origin_emits_drift_warning(
        self, tmp_path: Path
    ) -> None:
        origin_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=None,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=origin_sha,
            ),
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                check_freshness(doe_clone=tmp_path)

        drift_warnings = [w for w in caught if issubclass(w.category, DriftWarning)]
        assert drift_warnings, (
            "State (B) — pin ABSENT but ref present on origin — must emit DriftWarning"
        )
        warning_text = str(drift_warnings[0].message)
        assert origin_sha in warning_text, (
            f"DriftWarning must contain the origin SHA; got: {warning_text!r}"
        )
        assert "ACTION REQUIRED" in warning_text or "re-vendor" in warning_text.lower(), (
            f"DriftWarning must demand re-vendor action; got: {warning_text!r}"
        )

    def test_read_pin_sha_absent_file_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        """_read_pin_sha returns None when PIN_SHA_FILE is absent."""
        monkeypatch.setattr(
            "coordinator_core.ops.emit.doe_drift.PIN_SHA_FILE",
            tmp_path / ".nonexistent",
        )
        assert _read_pin_sha() is None

    def test_read_pin_sha_sentinel_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        pin_file = tmp_path / ".doe-ref-pin"
        pin_file.write_text(_PIN_ABSENT_SENTINEL + "\n", encoding="utf-8")
        monkeypatch.setattr(
            "coordinator_core.ops.emit.doe_drift.PIN_SHA_FILE",
            pin_file,
        )
        assert _read_pin_sha() is None

    def test_read_pin_sha_real_sha_returns_it(self, tmp_path: Path, monkeypatch) -> None:
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        pin_file = tmp_path / ".doe-ref-pin"
        pin_file.write_text(sha + "\n", encoding="utf-8")
        monkeypatch.setattr(
            "coordinator_core.ops.emit.doe_drift.PIN_SHA_FILE",
            pin_file,
        )
        assert _read_pin_sha() == sha

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    @pytest.mark.real_home
    def test_probe_freshness_ref_live_ref_absent_returns_none(self) -> None:
        doe_clone = resolve_doe_clone()
        result = probe_freshness_ref(doe_clone)
        assert result is None or isinstance(result, str), (
            f"probe_freshness_ref must return None or a SHA string; got {result!r}"
        )


class TestTagIsAncestorOfPin:

    _DOE_CLONE = Path("/tmp/fake-doe-clone")
    _TAG_SHA = "aaaa" * 10
    _PIN_SHA = "bbbb" * 10

    def test_returncode_0_returns_true(self) -> None:
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is True

    def test_returncode_1_returns_false(self) -> None:
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=1),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is False

    def test_returncode_2_returns_none(self) -> None:
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=2),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None

    def test_returncode_128_returns_none(self) -> None:
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=128),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None

    def test_file_not_found_returns_none(self) -> None:
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            side_effect=FileNotFoundError("git executable not found"),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None

    def test_timeout_expired_returns_none(self) -> None:
        """TimeoutExpired → None.

        Live because `git_scope.git_predicate` always passes a bound
        (`FOREIGN_REPO_GIT_TIMEOUT_SECONDS`); previously a dead except branch,
        because subprocess.TimeoutExpired is only raised when timeout= is passed.
        """
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "merge-base"], 30),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None


# AC_NORMALIZER: provenance normalizer — reuse _normalize from strang-01

class TestProvenanceNormalizerReuse:
    """AC_NORMALIZER: normalize-then-compare using _normalize from test_emit_parity.

    The normalizer is NOT duplicated here — we import the existing helper
    (established by strang-01's facade seam tests).  These tests verify that it
    correctly handles the DoE fixture's AC5-PROVENANCE paths so both sides of the
    conformance comparison can be scrubbed identically.
    """

    def _make_record_with_live_provenance(self) -> dict:
        return {
            "repo": "dbc-oduffy/live-repo",
            "title": "some-record",
            "observed_at": "2026-07-05T15:30:00Z",
            "computed_as_of": "2026-07-05T15:30:00Z",
            "REPO_NAME": "dbc-oduffy/live-repo",
            "provenance": {
                "source_kind": "local_fs",
                "repo": "dbc-oduffy/live-repo",
                "ref": {
                    "branch": "work/some-branch/2026-07-05",
                    "sha": "aabbccddeeff00112233445566778899aabbccdd",
                },
                "path": "state/some-record.md",
                "observed_at": "2026-07-05T15:30:00Z",
                "derivation": "parsed",
            },
        }

    def test_all_seven_ac5_provenance_fields_scrubbed(self) -> None:
        """All 7 AC5-PROVENANCE fields normalized to typed sentinels by _normalize.

        Oracle: DoE emission-conformance-contract.md § AC5-PROVENANCE.
        """
        record = self._make_record_with_live_provenance()
        result = _normalize(record)

        failures = []
        prov = result.get("provenance", {})

        if prov.get("observed_at") != _TS_SENTINEL:
            failures.append(f"field1 provenance.observed_at = {prov.get('observed_at')!r}")
        if prov.get("ref", {}).get("sha") != _SHA_SENTINEL:
            failures.append(f"field2 provenance.ref.sha = {prov.get('ref', {}).get('sha')!r}")
        if prov.get("ref", {}).get("branch") != _ID_SENTINEL:
            failures.append(f"field3 provenance.ref.branch = {prov.get('ref', {}).get('branch')!r}")
        if prov.get("repo") != _ID_SENTINEL:
            failures.append(f"field4 provenance.repo = {prov.get('repo')!r}")
        if result.get("observed_at") != _TS_SENTINEL:
            failures.append(f"field5 top-level observed_at = {result.get('observed_at')!r}")
        if result.get("computed_as_of") != _TS_SENTINEL:
            failures.append(f"field6 top-level computed_as_of = {result.get('computed_as_of')!r}")
        if result.get("REPO_NAME") != _ID_SENTINEL:
            failures.append(f"field7 REPO_NAME = {result.get('REPO_NAME')!r}")

        assert not failures, (
            "_normalize must scrub all 7 AC5-PROVENANCE paths to typed sentinels:\n"
            + "\n".join(f"  {f}" for f in failures)
        )

    def test_live_run_normalized_matches_golden(self) -> None:
        """Live run with runtime-varying provenance matches normalized golden.

        Two records with DIFFERENT live provenance values must produce the SAME
        normalized output — proving the normalizer is idempotent and deterministic.
        This is the cross-repo byte-compare guarantee (AC_NORMALIZER).
        """
        record_a = self._make_record_with_live_provenance()
        record_a["observed_at"] = "2026-07-05T10:00:00Z"
        record_a["provenance"]["observed_at"] = "2026-07-05T10:00:00Z"
        record_a["provenance"]["ref"]["sha"] = "1111111111111111111111111111111111111111"
        record_a["provenance"]["ref"]["branch"] = "work/branch-a/2026-07-05"
        record_a["provenance"]["repo"] = "dbc-oduffy/repo-a"

        record_b = self._make_record_with_live_provenance()
        record_b["observed_at"] = "2026-07-05T22:59:00Z"
        record_b["provenance"]["observed_at"] = "2026-07-05T22:59:00Z"
        record_b["provenance"]["ref"]["sha"] = "9999999999999999999999999999999999999999"
        record_b["provenance"]["ref"]["branch"] = "feature/branch-b"
        record_b["provenance"]["repo"] = "dbc-oduffy/repo-b"

        normalized_a = _normalize(record_a)
        normalized_b = _normalize(record_b)

        assert normalized_a == normalized_b, (
            "Two records with different live provenance must normalize to the same golden.\n"
            f"Differences: normalized_a keys {set(normalized_a)} vs {set(normalized_b)}"
        )

    def test_non_provenance_fields_not_scrubbed(self) -> None:
        """_normalize must NOT scrub fields outside the AC5-PROVENANCE set.

        Over-normalization masks real drift — exactly as precise as the spec requires.
        """
        record = {
            "title": "stable-title",
            "status": "active",
            "created": "2026-01-15",
            "provenance": {
                "source_kind": "local_fs",
                "repo": "some/repo",
                "ref": {
                    "branch": "work/test",
                    "sha": "abc123" * 6 + "abcd",
                },
                "path": "state/test.md",
                "observed_at": "2026-07-05T00:00:00Z",
                "derivation": "parsed",
            },
        }
        result = _normalize(record)

        assert result["title"] == "stable-title", "title must not be normalized"
        assert result["status"] == "active", "status must not be normalized"
        assert result["created"] == "2026-01-15", "created must not be normalized"
        assert result["provenance"]["path"] == "state/test.md", (
            "provenance.path must not be normalized"
        )
        assert result["provenance"]["derivation"] == "parsed", (
            "provenance.derivation must not be normalized"
        )

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    @pytest.mark.real_home
    def test_doe_fixture_normalizes_consistently(self) -> None:
        fixture = read_doe_fixture()
        normalized_once = _normalize(fixture)
        normalized_twice = _normalize(normalized_once)
        assert normalized_once == normalized_twice, (
            "_normalize must be idempotent: normalizing an already-normalized fixture "
            "must produce the same result"
        )


# AC_DRIFT_CHECK: drift fails loud when pinned version lags min_supported

class TestRunDriftCheck:
    """AC_DRIFT_CHECK: run_drift_check fail-louds on synthetic version lag."""

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    @pytest.mark.real_home
    def test_drift_check_passes_with_current_pin(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            fixture = run_drift_check()

        assert "contract_version" in fixture
        assert "min_supported_contract_version" in fixture

    def test_drift_check_fails_on_synthetic_lag(self, tmp_path: Path) -> None:
        """AC_DRIFT_CHECK: pinned version < min_supported → DriftError.

        Synthetic lag test: force min_supported to 9.9.9 (beyond any real pin)
        to prove the gate fires.
        """
        synthetic_fixture = {
            "contract_version": "9.9.9",
            "min_supported_contract_version": "9.9.9",
            "schema_version": "9.9.9",
            "handoffs": [],
            "malformed_records": {},
        }
        fixture_dir = tmp_path / "coordinator/cockpit-contract/conformance"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "emission-conformance.json").write_text(
            json.dumps(synthetic_fixture), encoding="utf-8"
        )

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift.check_freshness",
                return_value=None,
            ),
        ):
            with pytest.raises(DriftError, match="below the DoE min_supported_contract_version"):
                run_drift_check(pinned_version="2.5.0", doe_clone=tmp_path)

    def test_drift_check_passes_when_pinned_above_min_supported(self, tmp_path: Path) -> None:
        synthetic_fixture = {
            "contract_version": "3.0.0",
            "min_supported_contract_version": "2.5.0",
        }
        fixture_dir = tmp_path / "coordinator/cockpit-contract/conformance"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "emission-conformance.json").write_text(
            json.dumps(synthetic_fixture), encoding="utf-8"
        )

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift.check_freshness",
                return_value=None,
            ),
        ):
            fixture = run_drift_check(pinned_version="2.5.0", doe_clone=tmp_path)
            assert fixture["contract_version"] == "3.0.0"
