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

# Review: code-reviewer (F6) — load-bearing negative-spec: AheadOfReleaseWarning must NOT
# subclass DriftWarning.  The re-vendor post-check kills on DriftWarning but only logs plain
# UserWarning; a regression in the class hierarchy would silently break that gate.
assert not issubclass(AheadOfReleaseWarning, DriftWarning), (
    "AheadOfReleaseWarning must not subclass DriftWarning — load-bearing for re-vendor post-check"
)

# Reuse the EXISTING normalizer and typed sentinels from the shared normalizers module.
# AC_NORMALIZER: these are the shared AC5-PROVENANCE oracles.
# Review: code-reviewer (F2) — import from normalizers (production module) rather than
# cross-importing from test_emit_parity (test leaf); avoids pytest collection-isolation
# breakage and makes the surface available for future runtime callers.
from coordinator_core.ops.emit.normalizers import (
    _normalize,
    _TS_SENTINEL,
    _SHA_SENTINEL,
    _ID_SENTINEL,
)


# ---------------------------------------------------------------------------
# Helpers — skip guard for live DoE clone
# ---------------------------------------------------------------------------

def _live_doe_clone() -> Optional[Path]:
    """Return the live DoE clone path or None if unavailable on this machine."""
    try:
        return resolve_doe_clone()
    except DoeResolveError:
        return None


_DOE_AVAILABLE = _live_doe_clone() is not None


# ---------------------------------------------------------------------------
# AC_Q-b: fixture read from DoE clone, not co-vendored
# ---------------------------------------------------------------------------

class TestFixtureResolution:
    """AC_Q-b: fixture lives in the DoE clone, not co-vendored in claude-klabauter's _vendor/."""

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_fixture_read_from_doe_clone(self) -> None:
        """Fixture is read from repos.doe_claude (not claude-klabauter's _vendor/)."""
        doe_clone = resolve_doe_clone()
        fixture = read_doe_fixture(doe_clone)

        assert isinstance(fixture, dict), "fixture must be a dict"
        assert "contract_version" in fixture, "fixture must carry contract_version"
        assert "min_supported_contract_version" in fixture, (
            "fixture must carry min_supported_contract_version (CD-2)"
        )

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_fixture_not_in_vendor_tree(self) -> None:
        """The fixture MUST NOT be co-vendored in claude-klabauter's _vendor/ tree.

        Co-vendoring would re-create the co-vendor blindness DR-210 forbids:
        both the pin and the fixture would drift together silently.
        """
        from coordinator_core.ops.emit.validate import _VENDOR_CONTRACT
        co_vendored = _VENDOR_CONTRACT / "conformance" / "emission-conformance.json"
        assert not co_vendored.exists(), (
            f"Fixture must NOT be co-vendored at {co_vendored} — "
            "it must be read from the DoE clone at check-time (DR-210 anti-drift invariant)."
        )

    def test_fixture_parse_failure_raises_doe_resolve_error(self, tmp_path: Path) -> None:
        """Malformed JSON in the fixture raises DoeResolveError, not a bare JSONDecodeError."""
        bad_fixture = tmp_path / "coordinator/cockpit-contract/conformance"
        bad_fixture.mkdir(parents=True)
        (bad_fixture / "emission-conformance.json").write_text("{bad json", encoding="utf-8")

        with pytest.raises(DoeResolveError, match="not valid JSON"):
            read_doe_fixture(tmp_path)

    def test_fixture_absent_raises_doe_resolve_error(self, tmp_path: Path) -> None:
        """Absent fixture file raises DoeResolveError with guidance."""
        with pytest.raises(DoeResolveError, match="not found"):
            read_doe_fixture(tmp_path)


# ---------------------------------------------------------------------------
# AC_Q-a-band: version-band gate (min_supported, not equality)
# ---------------------------------------------------------------------------

class TestVersionBand:
    """AC_Q-a-band: gate semantics — min_supported_contract_version, not equality."""

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
        """Pinned == min_supported → PASS."""
        check_version_band("2.5.0", self._make_fixture("2.5.0", "2.5.0"))

    def test_pinned_above_min_supported_passes(self) -> None:
        """Pinned > min_supported → PASS."""
        check_version_band("2.6.0", self._make_fixture("2.6.0", "2.5.0"))

    def test_contract_version_bump_alone_does_not_fail(self) -> None:
        """Critical: contract_version advances, min_supported stays trailing → no fail.

        This is the reader-first invariant. DoE bumps contract_version first and raises
        min_supported only after the re-vendor window closes.  claude-klabauter must NOT fail
        during the window (equality-fail-loud is architecturally incompatible with reader-first).
        """
        # Simulate: DoE bumps contract_version to 2.6.0 but leaves min_supported at 2.5.0.
        # claude-klabauter's pin is 2.5.0 — MUST pass.
        check_version_band("2.5.0", self._make_fixture("2.6.0", "2.5.0"))  # must not raise

    def test_min_supported_past_pin_fails_loud(self) -> None:
        """min_supported_contract_version > pinned → DriftError (re-vendor required).

        After the re-vendor window closes DoE raises min_supported.  A still-at-2.5.0
        claude-klabauter pin must fail loud — the grace window has closed.
        """
        with pytest.raises(DriftError, match="below the DoE min_supported_contract_version"):
            check_version_band("2.5.0", self._make_fixture("2.6.0", "2.6.0"))

    def test_significantly_lagging_pin_fails_loud(self) -> None:
        """Pin lagging multiple versions also fails loud."""
        with pytest.raises(DriftError):
            check_version_band("2.3.0", self._make_fixture("2.6.0", "2.5.0"))

    def test_missing_min_supported_warns_not_raises(self) -> None:
        """Missing min_supported_contract_version in fixture → WARN, not error.

        DoE commitment: always publish the field.  Absent means DoE has not yet;
        we warn rather than breaking claude-klabauter (graceful degradation).
        """
        fixture = {"contract_version": "2.5.0"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            check_version_band("2.5.0", fixture)
        assert any("min_supported_contract_version" in str(w.message) for w in caught), (
            "Missing min_supported_contract_version must produce a warning"
        )

    def test_patch_version_comparison_correct(self) -> None:
        """Patch component comparison is numeric, not lexicographic (2.5.10 > 2.5.9)."""
        # 2.5.10 > 2.5.9: should pass (pinned >= min_supported)
        check_version_band("2.5.10", self._make_fixture("2.5.10", "2.5.9"))
        # 2.5.9 < 2.5.10: should fail
        with pytest.raises(DriftError):
            check_version_band("2.5.9", self._make_fixture("2.5.10", "2.5.10"))


# ---------------------------------------------------------------------------
# AC_Q-a-sha: freshness-ref probe + SHA mismatch / ref-absent paths
# ---------------------------------------------------------------------------

class TestFreshnessRef:
    """AC_Q-a-sha: cockpit-contract-release ref probe; fail loud on SHA mismatch."""

    def test_ref_absent_graceful_no_exception(self, tmp_path: Path) -> None:
        """AC_REF_ABSENT: ref absent on DoE origin → WARN, no DriftError.

        Live constraint: DoE has NOT published refs/tags/cockpit-contract-release at
        strang-02 pickup.  This is the expected path in CI and on live machines until
        DoE publishes the ref.  Must NEVER hard-fail on ref absence.
        """
        # Write a real pin SHA so we get past the 'pin absent' early exit.
        pin_sha = "aabbccddeeff00112233445566778899aabbccdd"

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=pin_sha,
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=None,  # ref absent on origin
            ),
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                check_freshness(doe_clone=tmp_path)  # must not raise

        assert any("ref" in str(w.message).lower() for w in caught), (
            "Ref-absent path must emit a warning about the missing ref"
        )

    def test_sha_mismatch_raises_drift_error(self, tmp_path: Path) -> None:
        """AC_Q-a-sha: SHA mismatch → DriftError (re-vendor required).

        Simulates a synthetic SHA advance on origin: the pin records an old SHA but
        the current DoE origin returns a different (newer) SHA.  Explicitly exercises
        the "pin behind tag" (False) path — not the indeterminate-via-non-git-tmp_path
        path that the previous mock gap inadvertently tested.

        Review: code-reviewer (F3) — add _tag_is_ancestor_of_pin mock so the test
        exercises the explicit pin-behind path (return_value=False) rather than the
        indeterminate path that fires when tmp_path is not a git repo.  Removes the
        real subprocess call and makes the test name accurate.
        """
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
                return_value=False,  # explicit pin-behind path → DriftError
            ),
        ):
            with pytest.raises(DriftError, match="SHA mismatch"):
                check_freshness(doe_clone=tmp_path)

    def test_pin_ahead_of_tag_warns_not_raises(self, tmp_path: Path) -> None:
        """Pin AHEAD of release tag → AheadOfReleaseWarning, NOT DriftError.

        Reader-first window: claude-klabauter re-vendors v2.6.0 while the tag still points to
        v2.5.0.  origin_sha != pin_sha, but the tag IS an ancestor of the pin (pin AHEAD).
        Must emit AheadOfReleaseWarning and NOT raise DriftError.

        Negative-spec: AheadOfReleaseWarning must NOT be a DriftWarning — the re-vendor
        post-check must not die on it.
        """
        pin_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"  # v2.6.0 pin
        origin_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # v2.5.0 tag (ancestor)

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
                return_value=True,  # tag IS ancestor of pin → pin AHEAD
            ),
        ):
            with pytest.warns(AheadOfReleaseWarning):
                check_freshness(doe_clone=tmp_path)  # must NOT raise

    def test_pin_behind_tag_raises(self, tmp_path: Path) -> None:
        """Pin BEHIND release tag → DriftError with 'SHA mismatch'.

        The ancestry helper returns False (tag is NOT ancestor of pin → pin is behind).
        Must raise DriftError preserving the 'SHA mismatch' message text.
        """
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
                return_value=False,  # not ancestor → pin is behind
            ),
        ):
            with pytest.raises(DriftError, match="SHA mismatch"):
                check_freshness(doe_clone=tmp_path)

    def test_ancestry_indeterminate_raises(self, tmp_path: Path) -> None:
        """Indeterminate ancestry → DriftError with 'SHA mismatch' (fail-loud safe default).

        When _tag_is_ancestor_of_pin returns None (git error, commits absent, etc.)
        the module cannot prove the pin is ahead, so it falls through to DriftError.
        """
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
                return_value=None,  # indeterminate → treat as drift
            ),
        ):
            with pytest.raises(DriftError, match="SHA mismatch"):
                check_freshness(doe_clone=tmp_path)

    def test_sha_match_passes(self, tmp_path: Path) -> None:
        """SHA match → PASS (no exception, no warning)."""
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
            check_freshness(doe_clone=tmp_path)  # must not raise

    def test_pin_absent_sentinel_skips_freshness(self, tmp_path: Path) -> None:
        """PIN_SHA_FILE containing ABSENT + ref absent on origin → WARN/skip, no error.

        State (A): both the pin file says ABSENT and the origin has no ref yet.
        Must emit a warning and not raise — the expected path during the initial
        strang-02 window before DoE publishes the ref.

        Review: code-reviewer (F1) — updated to mock probe_freshness_ref returning None
        (ref absent on origin) so the test isolates state (A) cleanly; check_freshness
        now probes origin even when pin is ABSENT.
        """
        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=None,  # sentinel → None from _read_pin_sha
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=None,  # ref also absent on origin (state A)
            ),
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                check_freshness(doe_clone=tmp_path)  # must not raise

        assert caught, "Pin-absent + ref-absent-on-origin path must emit a warning"
        # Must NOT emit a DriftWarning (that's state B — ref present but pin ABSENT).
        drift_warnings = [w for w in caught if issubclass(w.category, DriftWarning)]
        assert not drift_warnings, (
            "State (A) — ref also absent on origin — must NOT emit DriftWarning; "
            f"got: {[str(w.message) for w in drift_warnings]}"
        )

    def test_pin_absent_but_ref_now_on_origin_emits_drift_warning(
        self, tmp_path: Path
    ) -> None:
        """Pin ABSENT but origin now has the ref → DriftWarning, no DriftError.

        State (B): DoE published the cockpit-contract-release ref but
        ``bin/claude-klabauter-revendor-cockpit-contract.py`` has not been re-run so the pin file
        still contains ABSENT.  The SHA-mismatch gate is disabled; a loud, distinct
        DriftWarning (not DriftError) demands action.

        Rationale for WARN vs ERROR: a hard DriftError the instant DoE publishes would
        break claude-klabauter before bin/claude-klabauter-revendor-cockpit-contract.py can run.  The
        DriftWarning category is
        distinguishable from routine warnings so callers can escalate it in test suites.

        Review: code-reviewer (F1) — exercises the "ref now present on origin but pin
        still ABSENT" transition; mocks probe to return a real SHA.
        """
        origin_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 40 hex chars

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift._read_pin_sha",
                return_value=None,  # pin still ABSENT
            ),
            patch(
                "coordinator_core.ops.emit.doe_drift.probe_freshness_ref",
                return_value=origin_sha,  # ref NOW present on origin (state B)
            ),
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                check_freshness(doe_clone=tmp_path)  # must NOT raise DriftError

        # Must emit exactly one DriftWarning (the loud, distinct signal).
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
        """_read_pin_sha returns None for the ABSENT sentinel (ref not yet published)."""
        pin_file = tmp_path / ".doe-ref-pin"
        pin_file.write_text(_PIN_ABSENT_SENTINEL + "\n", encoding="utf-8")
        monkeypatch.setattr(
            "coordinator_core.ops.emit.doe_drift.PIN_SHA_FILE",
            pin_file,
        )
        assert _read_pin_sha() is None

    def test_read_pin_sha_real_sha_returns_it(self, tmp_path: Path, monkeypatch) -> None:
        """_read_pin_sha returns the stored SHA when the file contains a real SHA."""
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        pin_file = tmp_path / ".doe-ref-pin"
        pin_file.write_text(sha + "\n", encoding="utf-8")
        monkeypatch.setattr(
            "coordinator_core.ops.emit.doe_drift.PIN_SHA_FILE",
            pin_file,
        )
        assert _read_pin_sha() == sha

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_probe_freshness_ref_live_ref_absent_returns_none(self) -> None:
        """Live test: DoE origin returns None (ref not yet published) → no exception.

        This is the expected result at strang-02 pickup (live constraint).
        If the ref IS published, this test passes anyway (None or a SHA, no exception).
        """
        doe_clone = resolve_doe_clone()
        # Must not raise — either None (ref absent) or a SHA string.
        result = probe_freshness_ref(doe_clone)
        assert result is None or isinstance(result, str), (
            f"probe_freshness_ref must return None or a SHA string; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Direct unit tests of _tag_is_ancestor_of_pin exit-code branches (Finding 4)
# ---------------------------------------------------------------------------


class TestTagIsAncestorOfPin:
    """Direct unit tests of _tag_is_ancestor_of_pin exit-code → return-value mapping.

    Review: code-reviewer (F4) — no test previously called _tag_is_ancestor_of_pin
    directly; all coverage was via check_freshness (which mocks the function at the
    call site).  These tests validate the subprocess handling logic — the highest-risk
    surface in the diff — without hitting the filesystem.

    Exit-code contract:
      0   → True  (merge-base says tag IS ancestor of pin → pin AHEAD)
      1   → False (not ancestor → pin behind or diverged)
      ≥ 2 → None  (git error, commits absent, not-a-commit SHA, etc.)
      FileNotFoundError → None (git not installed)
      TimeoutExpired    → None (live after F2 adds timeout=30)
    """

    _DOE_CLONE = Path("/tmp/fake-doe-clone")
    _TAG_SHA = "aaaa" * 10
    _PIN_SHA = "bbbb" * 10

    def test_returncode_0_returns_true(self) -> None:
        """Exit 0 → True: tag IS an ancestor of pin (pin AHEAD of tag)."""
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is True

    def test_returncode_1_returns_false(self) -> None:
        """Exit 1 → False: not an ancestor (pin behind tag or diverged)."""
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=1),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is False

    def test_returncode_2_returns_none(self) -> None:
        """Exit 2 → None (indeterminate: git error, e.g. commits absent from clone)."""
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=2),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None

    def test_returncode_128_returns_none(self) -> None:
        """Exit 128 → None (fatal git error, e.g. annotated tag-object SHA passed to merge-base).

        This was the pre-F1 failure mode: ls-remote without --dereference returned a
        tag-object SHA; merge-base errors with 128 ('Not a commit') → None → DriftError,
        silently breaking AheadOfReleaseWarning for annotated tags.
        """
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            return_value=MagicMock(returncode=128),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None

    def test_file_not_found_returns_none(self) -> None:
        """FileNotFoundError (git not installed) → None."""
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            side_effect=FileNotFoundError("git executable not found"),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None

    def test_timeout_expired_returns_none(self) -> None:
        """TimeoutExpired → None.

        Live after F2 adds timeout=30; previously a dead except branch because
        subprocess.TimeoutExpired is only raised when timeout= is passed.
        """
        with patch(
            "coordinator_core.ops.emit.doe_drift.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "merge-base"], 30),
        ):
            assert _tag_is_ancestor_of_pin(self._DOE_CLONE, self._TAG_SHA, self._PIN_SHA) is None


# ---------------------------------------------------------------------------
# AC_NORMALIZER: provenance normalizer — reuse _normalize from strang-01
# ---------------------------------------------------------------------------

class TestProvenanceNormalizerReuse:
    """AC_NORMALIZER: normalize-then-compare using _normalize from test_emit_parity.

    The normalizer is NOT duplicated here — we import the existing helper
    (established by strang-01's facade seam tests).  These tests verify that it
    correctly handles the DoE fixture's AC5-PROVENANCE paths so both sides of the
    conformance comparison can be scrubbed identically.
    """

    def _make_record_with_live_provenance(self) -> dict:
        """Record with 'live' runtime-varying provenance fields."""
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
                "repo": "some/repo",   # will be normalized (field 4)
                "ref": {
                    "branch": "work/test",  # normalized (field 3)
                    "sha": "abc123" * 6 + "abcd",  # normalized (field 2) — 40 hex chars
                },
                "path": "state/test.md",  # must NOT be normalized
                "observed_at": "2026-07-05T00:00:00Z",  # normalized (field 1)
                "derivation": "parsed",  # must NOT be normalized
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
    def test_doe_fixture_normalizes_consistently(self) -> None:
        """DoE fixture normalizes to a consistent state (no runtime-varying residue).

        The DoE fixture already uses sentinel values for volatile fields (it was
        generated against a frozen corpus).  After normalization it must be equal to
        itself — i.e., normalizing a pre-normalized fixture is idempotent.
        """
        fixture = read_doe_fixture()
        normalized_once = _normalize(fixture)
        normalized_twice = _normalize(normalized_once)
        assert normalized_once == normalized_twice, (
            "_normalize must be idempotent: normalizing an already-normalized fixture "
            "must produce the same result"
        )


# ---------------------------------------------------------------------------
# AC_DRIFT_CHECK: drift fails loud when pinned version lags min_supported
# ---------------------------------------------------------------------------

class TestRunDriftCheck:
    """AC_DRIFT_CHECK: run_drift_check fail-louds on synthetic version lag."""

    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_drift_check_passes_with_current_pin(self) -> None:
        """Current vendored pin (2.5.0) passes the version-band gate against DoE fixture."""
        # This also exercises the full resolution path (registry → fixture → version check).
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            fixture = run_drift_check()  # must not raise

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
        # Write synthetic fixture to tmp_path at the expected relative path.
        fixture_dir = tmp_path / "coordinator/cockpit-contract/conformance"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "emission-conformance.json").write_text(
            json.dumps(synthetic_fixture), encoding="utf-8"
        )

        with (
            patch(
                "coordinator_core.ops.emit.doe_drift.check_freshness",
                return_value=None,  # skip freshness in this unit test
            ),
        ):
            with pytest.raises(DriftError, match="below the DoE min_supported_contract_version"):
                run_drift_check(pinned_version="2.5.0", doe_clone=tmp_path)

    def test_drift_check_passes_when_pinned_above_min_supported(self, tmp_path: Path) -> None:
        """Pinned version > min_supported → PASS even when contract_version is higher."""
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


# ---------------------------------------------------------------------------
# AC_CONFORMANCE: conformance via strang-01 strangled path (not raw bash)
# ---------------------------------------------------------------------------

@pytest.mark.real_home  # live-tree parity oracle: resolves the real coordinator root
class TestConformanceViaStrang01Path:
    """AC_CONFORMANCE: conformance runs against the strang-01 facade (envelope.emit()).

    Proves the SEAM conforms:
      - The strang-01 path (envelope.emit() via EmitContext) is used — not raw bash.
      - The output schema_version satisfies the DoE version-band gate.
      - Normalize-then-compare uses the shared _normalize helper on both sides.
      - Full C2 entity coverage: all entity types from the DoE fixture are structurally
        present in the strang-01 output (no missing entity type keys).
    """

    @pytest.mark.usefixtures("requires_vendor_pin")
    def test_strang01_output_passes_version_band_synthetic(self, tmp_path: Path) -> None:
        """Synthetic-DoE variant: strang-01 path conforms against a fixture in tmp_path.

        CI regression net — runs WITHOUT a live DoE clone (gated on requires_vendor_pin
        only, NOT _DOE_AVAILABLE).  Writes a synthetic fixture with a low
        min_supported_contract_version to tmp_path, runs envelope.emit() (strang-01
        strangled path), and verifies the output schema_version satisfies the version band.

        Does NOT prove conformance against DoE's actual fixture, but DOES prove:
        (a) the strang-01 path (envelope.emit()) is exercised and produces schema_version,
        (b) check_version_band fires correctly on the strang-01 output.

        Review: code-reviewer (F3) — AC_CONFORMANCE had zero CI coverage because all
        TestConformanceViaStrang01Path tests required _DOE_AVAILABLE.  This synthetic
        variant closes that gap: it is the CI regression net for the strang-01 seam.
        """
        from coordinator_core.ops.emit import envelope as _envelope
        from coordinator_core.ops.emit.tests.test_emit_parity import build_emit_context

        # Synthetic fixture with a conservatively low min_supported so any real schema_version
        # produced by strang-01 satisfies the gate.
        synthetic_fixture = {
            "contract_version": "2.5.0",
            "min_supported_contract_version": "1.0.0",
            "handoffs": [],
            "malformed_records": {},
        }

        # Run strang-01 strangled path (envelope.emit() — NOT raw bash).
        ctx = build_emit_context()
        out = tmp_path / "strang01-synthetic.json"
        _envelope.emit(ctx, out=out)
        strang01_output = json.loads(out.read_text())

        # Prove the seam conforms: schema_version >= min_supported.
        strang01_version = strang01_output.get("schema_version", "")
        assert strang01_version, "strang-01 output must carry schema_version"

        # check_version_band uses the same gate logic as the production drift-check.
        check_version_band(strang01_version, synthetic_fixture)  # must not raise

    @pytest.mark.usefixtures("requires_vendor_pin")
    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_strang01_output_passes_version_band(self, tmp_path: Path) -> None:
        """Strang-01 strangled path (envelope.emit()) output satisfies DoE version band.

        Loads DoE fixture → extracts min_supported → runs envelope.emit() (strang-01
        path) → verifies the output schema_version >= min_supported.

        This test FAILS if a raw bash call is substituted for envelope.emit() — the
        seam is the Python envelope stack, not the bash oracle.
        """
        from coordinator_core.ops.emit import envelope as _envelope
        from coordinator_core.ops.emit.tests.test_emit_parity import build_emit_context

        # Load DoE fixture for version-band reference.
        doe_clone = resolve_doe_clone()
        fixture = read_doe_fixture(doe_clone)
        min_supported = fixture["min_supported_contract_version"]

        # Run strang-01 strangled path (NOT raw bash).
        ctx = build_emit_context()
        out = tmp_path / "strang01-output.json"
        _envelope.emit(ctx, out=out)
        strang01_output = json.loads(out.read_text())

        # Prove the seam conforms: schema_version >= min_supported.
        strang01_version = strang01_output.get("schema_version", "")
        assert strang01_version, "strang-01 output must carry schema_version"

        # check_version_band uses the same gate logic as the production drift-check.
        check_version_band(strang01_version, fixture)  # must not raise

    @pytest.mark.usefixtures("requires_vendor_pin")
    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_strang01_output_has_c2_entity_types(self, tmp_path: Path) -> None:
        """Strang-01 output covers the same C2 entity types as the DoE fixture (structural).

        The DoE fixture is a round-trip over full C2 entity coverage.  The strang-01
        output (from the frozen fixture tree) must have all the same top-level entity
        array keys — proving the facade produces schema-compatible output structure.
        """
        from coordinator_core.ops.emit import envelope as _envelope
        from coordinator_core.ops.emit.tests.test_emit_parity import build_emit_context

        doe_clone = resolve_doe_clone()
        doe_fixture = read_doe_fixture(doe_clone)

        # C2 entity type keys from the DoE fixture (top-level list-valued keys, excluding metadata).
        _SCALAR_KEYS = {
            "schema_version", "contract_version", "min_supported_contract_version",
            "emitted_at", "emitted_by_machine", "narrative_views", "REPO_NAME",
            "observed_at", "computed_as_of",
        }
        doe_entity_keys = {
            k for k, v in doe_fixture.items()
            if isinstance(v, (list, dict)) and k not in _SCALAR_KEYS and k != "malformed_records"
        }

        # Run strang-01 strangled path.
        ctx = build_emit_context()
        out = tmp_path / "strang01-c2.json"
        _envelope.emit(ctx, out=out)
        strang01_output = json.loads(out.read_text())

        strang01_entity_keys = {
            k for k, v in strang01_output.items()
            if isinstance(v, (list, dict)) and k not in _SCALAR_KEYS and k != "malformed_records"
        }

        missing = doe_entity_keys - strang01_entity_keys
        assert not missing, (
            f"Strang-01 output is missing C2 entity type keys present in DoE fixture: {missing}.  "
            "The strang-01 facade must produce all entity types the DoE fixture covers."
        )

    @pytest.mark.usefixtures("requires_vendor_pin")
    @pytest.mark.skipif(not _DOE_AVAILABLE, reason="DoE clone not available on this machine")
    def test_both_sides_normalize_identically(self, tmp_path: Path) -> None:
        """Normalize-then-compare: _normalize applied to strang-01 and DoE fixture is idempotent.

        The key assertion: after normalization, both outputs carry the SAME sentinels
        for AC5-PROVENANCE fields.  This proves the shared normalizer is consistent
        across the two comparison sides.
        """
        from coordinator_core.ops.emit import envelope as _envelope
        from coordinator_core.ops.emit.tests.test_emit_parity import build_emit_context

        doe_clone = resolve_doe_clone()
        doe_fixture = read_doe_fixture(doe_clone)

        ctx = build_emit_context()
        out = tmp_path / "strang01-normalize.json"
        _envelope.emit(ctx, out=out)
        strang01_output = json.loads(out.read_text())

        # Normalize both sides.
        norm_doe = _normalize(doe_fixture)
        norm_strang01 = _normalize(strang01_output)

        # Both normalized dicts must NOT contain any live provenance values.
        def _check_no_live_values(d: dict, path: str = "") -> list[str]:
            """Return paths where a live provenance value survived normalization."""
            violations = []
            if isinstance(d, dict):
                for k, v in d.items():
                    sub_path = f"{path}.{k}" if path else k
                    if k == "observed_at" and isinstance(v, str) and v != _TS_SENTINEL:
                        violations.append(f"{sub_path}={v!r}")
                    elif k == "sha" and isinstance(v, str) and v != _SHA_SENTINEL and len(v) == 40:
                        violations.append(f"{sub_path}={v!r}")
                    # Review: code-reviewer (F6) — drop the "/" in v heuristic; check
                    # v != _ID_SENTINEL alone.  The old heuristic silently passed
                    # unnormalized branches that contain no slash (e.g. "main", "master").
                    elif k == "branch" and isinstance(v, str) and v != _ID_SENTINEL:
                        violations.append(f"{sub_path}={v!r}")
                    else:
                        violations.extend(_check_no_live_values(v, sub_path))
            elif isinstance(d, list):
                for i, item in enumerate(d):
                    violations.extend(_check_no_live_values(item, f"{path}[{i}]"))
            return violations

        doe_violations = _check_no_live_values(norm_doe)
        strang01_violations = _check_no_live_values(norm_strang01)

        assert not doe_violations, (
            f"DoE fixture still has live provenance values after normalization: {doe_violations}"
        )
        assert not strang01_violations, (
            f"Strang-01 output still has live provenance values after normalization: "
            f"{strang01_violations}"
        )
