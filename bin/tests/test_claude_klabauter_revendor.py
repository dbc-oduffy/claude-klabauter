"""
bin/tests/test_claude_klabauter_revendor.py — unit tests for bin/claude-klabauter-revendor-cockpit-contract.py.

Purpose: no-network unit tests for the schema-only cockpit-contract re-vendor script.

Coverage (AC10):
  1. Full-SHA pin written; short-SHA rejected.
  2. Byte-identity mismatch → fail (no pin write).
  3. Consumer-visible delta → refuse without --ack-major.
  4. --ack-major permits proceeding when a consumer-visible delta was detected.
  5. ADDITIVE-OPTIONAL FIELD WIDEN (content_hash-shaped, 2.4.0→2.5.0 case) MUST refuse
     without --ack-major — explicit case, not just placeholder→concrete.
  6. No-delta / minor re-vendor proceeds with no gate at all (--ack-major irrelevant).
  7. Idempotent no-op: already-vendored at the same SHA exits 0 without churn.
  8. Post-vendor run_drift_check() invoked (mocked).
  9. Direction-aware downgrade guard (2026-07-23): refuses a version downgrade
     independent of --ack-major and of _detect_consumer_visible_delta -- including
     a PURE version downgrade with zero shape delta (hole #2), which the shape-delta
     gate alone would let sail through ungated. --allow-downgrade permits it; a
     downgrade that ALSO carries a real shape delta needs BOTH --ack-major and
     --allow-downgrade. Numeric-vs-lexical semver comparison is explicitly covered.

Fixtures: temp fake-clone git repos (real local git; no network); module-level path
constants (``_VENDOR_CONTRACT``, ``PIN_SHA_FILE``) are monkeypatched to isolated tmp
dirs. No network calls. No on-disk sentinel file is used anywhere in this suite — the
gate is inline-only (``--ack-major``).

Negative-spec (2026-07-21): this suite no longer exercises src/ or dist/ — upstream example-doctrine-repo
commit 7cca4d4c deleted the cockpit-contract TS/Zod toolchain, and the script now vendors
schema/ only. Do not reintroduce src/ fixtures, pnpm/node mocks, or a dist/ functional-
verify test here; see the script's module docstring negative-spec.

Spec backlink: docs/plans/2026-07-08-producer-emit-hold-removal-reader-first-consumer-owned.md § C3
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import warnings
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Real local git repos are load-bearing: the script under test pins on real
# 40-char SHAs read via `git rev-parse HEAD` and detects consumer-visible
# deltas by diffing real commits -- a mocked git object model can't produce
# a genuine SHA the pin-write/byte-identity assertions depend on.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Import the revendor script module via importlib (dash in filename).
# conftest.py already inserted the project root onto sys.path.
# ---------------------------------------------------------------------------

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "claude-klabauter-revendor-cockpit-contract.py"

# One-time load; all tests reference the already-exec'd module.
_spec = importlib.util.spec_from_file_location("_revendor_mod", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _fake_completed(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> CompletedProcess:
    """Build a fake subprocess.CompletedProcess for use in _git mocks."""
    cp: CompletedProcess = CompletedProcess.__new__(CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def _git_local(clone: Path, *args: str) -> CompletedProcess:
    """Run a real local git command (no network) inside a fake-clone repo."""
    result = subprocess.run(
        ["git", "-C", str(clone)] + list(args),
        capture_output=True,
        check=False,
    )
    return result


def _init_fake_clone(path: Path) -> None:
    """Initialise a bare-bones git repo for use as a fake example-doctrine-repo clone."""
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.local"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test CI"], check=True, capture_output=True)


def _make_commit(clone: Path) -> str:
    """Stage all and commit; return the resulting 40-char HEAD SHA."""
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "test commit"], check=True, capture_output=True)
    r = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, capture_output=True)
    return r.stdout.decode().strip()


# ---------------------------------------------------------------------------
# Schema content builders
# ---------------------------------------------------------------------------

_V240_SCHEMA = {
    "version": "2.4.0",
    "description": "base v2.4.0 schema",
    "$defs": {
        "Snapshot": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
            },
        },
    },
}

_V250_SCHEMA_WITH_CONTENT_HASH = {
    "version": "2.5.0",
    "description": "v2.5.0 with content_hash optional field widen",
    "$defs": {
        "Snapshot": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                # Additive-optional widen (the 2.4.0→2.5.0 content_hash R5 case).
                "content_hash": {"type": "string"},
            },
        },
    },
}

_V250_PLACEHOLDER_TO_CONCRETE = {
    "version": "2.5.0",
    "description": "v2.5.0 with placeholder→concrete transition",
    "$defs": {
        "Snapshot": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                # Was {} in v2.4.0, now a concrete object shape.
                "extra": {"type": "object", "properties": {"key": {"type": "string"}}},
            },
        },
    },
}

_ENTITY_SCHEMA_V240 = {
    "type": "object",
    "title": "SomeEntity",
    "properties": {
        "id": {"type": "string"},
        "status": {"type": "string"},
    },
}

_ENTITY_SCHEMA_V250_WIDEN = {
    "type": "object",
    "title": "SomeEntity",
    "properties": {
        "id": {"type": "string"},
        "status": {"type": "string"},
        # New additive-optional field — must be treated as requiring-ack (AC6).
        "content_hash": {"type": "string"},
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect _VENDOR_CONTRACT, PIN_SHA_FILE to isolated tmp dirs.

    Returns a dict with 'vendor', 'pin' keys for test convenience.

    Negative-spec (2026-07-21): only creates a schema/ subdir under vendor — src/ and
    dist/ are no longer part of the vendored tree (schema-only re-vendor).

    Review: code-reviewer (2026-07-23 F2) — dropped the `_STATE_DIR` monkeypatch (and
    the 'state' tmp dir it pointed at); the script has no `_STATE_DIR` reader.
    """
    vendor = tmp_path / "vendor" / "cockpit-contract"
    vendor.mkdir(parents=True)
    (vendor / "schema").mkdir()

    pin = vendor / ".doe-ref-pin"

    monkeypatch.setattr(_mod, "_VENDOR_CONTRACT", vendor)
    monkeypatch.setattr(_mod, "PIN_SHA_FILE", pin)

    return {"vendor": vendor, "pin": pin}


@pytest.fixture
def base_fake_clone(tmp_path: Path):
    """Fake example-doctrine-repo clone with v2.4.0 schema committed; returns (clone_path, sha_v240).

    Negative-spec (2026-07-21): no src/ tree — upstream commit 7cca4d4c deleted the
    TS/Zod toolchain; only schema/ is committed in the fake clone.
    """
    clone = tmp_path / "fake-doe-clone"
    clone.mkdir()
    _init_fake_clone(clone)

    doe_schema_dir = clone / "coordinator" / "cockpit-contract" / "schema"
    doe_schema_dir.mkdir(parents=True)

    (doe_schema_dir / "cockpit-contract.schema.json").write_text(
        json.dumps(_V240_SCHEMA, indent=2), encoding="utf-8"
    )
    (doe_schema_dir / "entity.schema.json").write_text(
        json.dumps(_ENTITY_SCHEMA_V240, indent=2), encoding="utf-8"
    )

    sha = _make_commit(clone)
    return clone, sha


@pytest.fixture
def widened_fake_clone(base_fake_clone):
    """Extends base_fake_clone: adds a second commit with the v2.5.0 content_hash widen.

    Returns (clone_path, sha_v240, sha_v250).
    """
    clone, sha_v240 = base_fake_clone
    doe_schema_dir = clone / "coordinator" / "cockpit-contract" / "schema"

    # Overwrite schemas to simulate the content_hash additive-optional widen.
    (doe_schema_dir / "cockpit-contract.schema.json").write_text(
        json.dumps(_V250_SCHEMA_WITH_CONTENT_HASH, indent=2), encoding="utf-8"
    )
    (doe_schema_dir / "entity.schema.json").write_text(
        json.dumps(_ENTITY_SCHEMA_V250_WIDEN, indent=2), encoding="utf-8"
    )

    sha_v250 = _make_commit(clone)
    return clone, sha_v240, sha_v250


# ---------------------------------------------------------------------------
# 1. Full-SHA pin / short-SHA rejection
#    Tests _fetch_and_resolve_sha via mocked _git; no network needed.
# ---------------------------------------------------------------------------

class TestFullShaPin:
    """_fetch_and_resolve_sha must write the full 40-char SHA; short SHA must fail."""

    FULL_SHA = "a" * 40  # 40-char valid hex SHA

    def _make_git_mock(self, fetch_rc: int = 0, revparse_out: str = FULL_SHA) -> Any:
        """Return a mock for _mod._git that drives fetch + rev-parse results."""
        call_count: dict = {"n": 0}

        def _fake_git(clone: Path, *args: str) -> CompletedProcess:
            cmd = args[0] if args else ""
            if cmd == "fetch":
                return _fake_completed(returncode=fetch_rc, stderr=b"mock fetch")
            if cmd == "rev-parse":
                out = (revparse_out + "\n").encode()
                return _fake_completed(returncode=0, stdout=out)
            return _fake_completed(returncode=0)

        return _fake_git

    def test_full_sha_accepted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A 40-char hex SHA returned by rev-parse is accepted and returned verbatim."""
        monkeypatch.setattr(_mod, "_git", self._make_git_mock(revparse_out=self.FULL_SHA))
        result = _mod._fetch_and_resolve_sha(tmp_path / "fake-clone", "refs/tags/cockpit-contract-release")
        assert result == self.FULL_SHA

    def test_short_sha_causes_die(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A 7-char short SHA from rev-parse is rejected with sys.exit (no ambiguity allowed)."""
        short_sha = "abc1234"
        monkeypatch.setattr(_mod, "_git", self._make_git_mock(revparse_out=short_sha))
        with pytest.raises(SystemExit) as exc_info:
            _mod._fetch_and_resolve_sha(tmp_path / "fake-clone", "refs/tags/cockpit-contract-release")
        assert exc_info.value.code != 0

    def test_fetch_failure_causes_die(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A non-zero git fetch exit code causes _die (sys.exit)."""
        monkeypatch.setattr(_mod, "_git", self._make_git_mock(fetch_rc=128))
        with pytest.raises(SystemExit) as exc_info:
            _mod._fetch_and_resolve_sha(tmp_path / "fake-clone", "refs/tags/cockpit-contract-release")
        assert exc_info.value.code != 0

    def test_non_hex_sha_causes_die(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A 40-char string that is not all hex is rejected (e.g. contains 'g')."""
        bad_sha = "g" * 40  # Not valid hex
        monkeypatch.setattr(_mod, "_git", self._make_git_mock(revparse_out=bad_sha))
        with pytest.raises(SystemExit) as exc_info:
            _mod._fetch_and_resolve_sha(tmp_path / "fake-clone", "refs/tags/cockpit-contract-release")
        assert exc_info.value.code != 0

    def test_rev_parse_uses_peeled_commit_ref(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """_fetch_and_resolve_sha MUST call `rev-parse FETCH_HEAD^{commit}`, never a bare
        `rev-parse FETCH_HEAD` — the bare form records the tag-object SHA for an
        annotated tag, not the commit (2026-07-21 regression: the default
        cockpit-contract-release ref advanced to an annotated tag and the bare form
        recorded the tag object, corrupting the pin).
        """
        seen_rev_parse_args: list[tuple] = []

        def _fake_git(clone: Path, *args: str) -> CompletedProcess:
            cmd = args[0] if args else ""
            if cmd == "fetch":
                return _fake_completed(returncode=0, stderr=b"mock fetch")
            if cmd == "rev-parse":
                seen_rev_parse_args.append(args)
                return _fake_completed(returncode=0, stdout=(self.FULL_SHA + "\n").encode())
            return _fake_completed(returncode=0)

        monkeypatch.setattr(_mod, "_git", _fake_git)
        _mod._fetch_and_resolve_sha(tmp_path / "fake-clone", "refs/tags/cockpit-contract-release")

        assert seen_rev_parse_args == [("rev-parse", "FETCH_HEAD^{commit}")], (
            "must peel via FETCH_HEAD^{commit} — a bare 'rev-parse FETCH_HEAD' records "
            "the tag OBJECT sha for an annotated tag, not the commit"
        )


class TestAnnotatedTagPeelsToCommit:
    """Real-git regression test: an annotated tag must resolve to its peeled commit,
    not the tag object SHA. This is the exact case that regressed 2026-07-21 when
    the cockpit-contract-release ref advanced to an annotated tag on origin.
    """

    def test_annotated_tag_resolves_to_commit_not_tag_object(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin-repo"
        origin.mkdir()
        _init_fake_clone(origin)
        (origin / "file.txt").write_text("hello", encoding="utf-8")
        commit_sha = _make_commit(origin)

        tag_r = subprocess.run(
            ["git", "-C", str(origin), "tag", "-a", "my-tag", "-m", "annotated tag"],
            check=True,
            capture_output=True,
        )
        assert tag_r.returncode == 0
        tag_obj_r = subprocess.run(
            ["git", "-C", str(origin), "rev-parse", "my-tag"],
            check=True,
            capture_output=True,
        )
        tag_sha = tag_obj_r.stdout.decode().strip()
        # Sanity: an annotated tag's own SHA differs from the commit it points at.
        assert tag_sha != commit_sha

        clone = tmp_path / "clone-repo"
        subprocess.run(
            ["git", "clone", str(origin), str(clone)],
            check=True,
            capture_output=True,
        )

        resolved = _mod._fetch_and_resolve_sha(clone, "my-tag")

        assert resolved == commit_sha, (
            "_fetch_and_resolve_sha must peel an annotated tag to its underlying "
            "commit SHA, matching doe_drift.probe_freshness_ref's peeling convention"
        )
        assert resolved != tag_sha, (
            "resolved SHA must NOT be the tag-object SHA — recording it would read as "
            "spurious pin drift on tag re-cut and break doe_drift's merge-base ancestry check"
        )


# ---------------------------------------------------------------------------
# 2. Byte-identity mismatch → fail (no pin write)
#    Tests _verify_schema_byte_identity; uses the real fake-clone git repo.
# ---------------------------------------------------------------------------

class TestByteIdentityVerify:
    """AC5: byte-identity verify — mismatch must fail with non-zero exit, no pin write."""

    def test_byte_match_passes(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Byte-identical vendored files cause _verify_schema_byte_identity to pass silently."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]

        # Write vendored schema files with the SAME content as the committed schema.
        _copy_git_show_to_vendor(clone, sha, vendor)

        # Should NOT raise or sys.exit.
        _mod._verify_schema_byte_identity(clone, sha)

    def test_byte_mismatch_fails_no_pin_write(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Vendored file differing from source must fail loud with non-zero exit."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        # Write MATCHING schema first, then corrupt one file.
        _copy_git_show_to_vendor(clone, sha, vendor)
        (vendor / "schema" / "cockpit-contract.schema.json").write_bytes(b"CORRUPTED CONTENT")

        # Pin must not exist before we check the invariant.
        assert not pin.exists(), "Pin should not exist before verify"

        with pytest.raises(SystemExit) as exc_info:
            _mod._verify_schema_byte_identity(clone, sha)
        assert exc_info.value.code != 0

        # Verify the pin was NOT written by _verify_schema_byte_identity itself.
        assert not pin.exists(), "Pin must NOT be written after a byte-identity mismatch"

    def test_missing_vendored_file_fails(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Missing vendored file after copy → fail loud (AC5: vendored file missing)."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]

        # Leave vendored schema/ dir empty — no files copied.
        # _verify_schema_byte_identity will look for the file and fail.
        with pytest.raises(SystemExit) as exc_info:
            _mod._verify_schema_byte_identity(clone, sha)
        assert exc_info.value.code != 0

    def test_stale_file_pruned_after_copy(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """A file in vendor before copy but absent from the incoming ref must be pruned.

        Regression test for Finding 1: _copy_schema must be authoritative
        (not additive) — the vendor set must EXACTLY equal the incoming ref set.
        Also verifies _verify_schema_byte_identity rejects extra vendored files
        (Review: code-reviewer F1 / F7).
        """
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]

        # Pre-populate vendor with both incoming files AND a stale extra file.
        _copy_git_show_to_vendor(clone, sha, vendor)
        stale_file = vendor / "schema" / "stale-not-in-ref.schema.json"
        stale_file.write_text(json.dumps({"stale": True}), encoding="utf-8")
        assert stale_file.exists(), "stale file must exist before copy step"

        # Run the authoritative copy — incoming ref does NOT include the stale file.
        _mod._copy_schema(clone, sha)

        # Stale file must have been pruned.
        assert not stale_file.exists(), (
            "_copy_schema must prune files absent from the incoming ref "
            "(vendor dir must EXACTLY equal the incoming ref set — not additive)"
        )
        # Incoming files must still be present with correct content.
        assert (vendor / "schema" / "cockpit-contract.schema.json").exists()
        assert (vendor / "schema" / "entity.schema.json").exists()

        # _verify_schema_byte_identity must also detect extra vendored files.
        # Place a stale file again to verify the verify step rejects it.
        stale_file.write_text(json.dumps({"stale": True}), encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            _mod._verify_schema_byte_identity(clone, sha)
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# 2b. _verify_dist_functional / pnpm/node dist/ build+verify — REMOVED 2026-07-21.
#     Negative-spec: upstream commit 7cca4d4c deleted the cockpit-contract TS/Zod
#     toolchain; the re-vendor script no longer builds or functionally verifies dist/.
#     Do not reintroduce a TestDistFunctionalVerify-shaped test here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3 & 4. Consumer-visible delta detection + inline MAJOR-delta ack gate
#         Tests _detect_consumer_visible_delta and _enforce_major_delta_gate.
# ---------------------------------------------------------------------------

class TestConsumerVisibleDeltaDetection:
    """AC6 conservative predicate: ANY schema surface change triggers the gate."""

    def test_no_delta_identical_schemas(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Identical vendored and incoming schemas produce (False, []) — no delta."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]

        # Vendored dir matches the committed schemas exactly.
        _copy_git_show_to_vendor(clone, sha, vendor)

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha)
        assert delta is False
        assert surfaces == []

    def test_new_property_detected_as_delta(
        self,
        base_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A new property in the incoming schema vs vendored triggers delta detection."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]

        # Vendored schema missing a field that appears in the incoming schema.
        vendored_schema = dict(_V240_SCHEMA)
        vendored_schema["$defs"] = {
            "Snapshot": {
                "type": "object",
                "properties": {"id": {"type": "string"}},  # missing 'name'
            }
        }
        (vendor / "schema" / "cockpit-contract.schema.json").write_text(
            json.dumps(vendored_schema, indent=2), encoding="utf-8"
        )
        (vendor / "schema" / "entity.schema.json").write_text(
            json.dumps(_ENTITY_SCHEMA_V240, indent=2), encoding="utf-8"
        )

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha)
        assert delta is True
        assert len(surfaces) >= 1

    def test_removed_schema_file_detected_as_delta(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """A schema file present in vendored but absent in incoming is flagged as delta."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]

        # Vendored has entity.schema.json; we'll make the incoming at sha NOT have it.
        # To simulate: copy both schema files, then add an extra one to vendor only.
        _copy_git_show_to_vendor(clone, sha, vendor)
        (vendor / "schema" / "extra-not-in-incoming.schema.json").write_text(
            json.dumps({"type": "object"}), encoding="utf-8"
        )

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha)
        assert delta is True
        assert any("removed" in s.lower() for s in surfaces)


# ---------------------------------------------------------------------------
# 2c. Version-stamp-churn normalization (2026-07-21 review finding).
#     A version bump alone rewrites the top-level `version` field AND the version
#     substring embedded in the top-level `description` field on EVERY schema file.
#     Without normalization the --ack-major gate reports "all N files changed" on
#     every single bump, drowning real shape changes and training the operator to
#     ack blindly. Tests: version-only bump -> no delta; version bump + a real
#     shape change -> delta detected, and ONLY the real surface is listed.
# ---------------------------------------------------------------------------

@pytest.fixture
def version_only_bump_fake_clone(base_fake_clone):
    """Extends base_fake_clone: adds a second commit that bumps ONLY the version
    stamp (top-level ``version`` field + the version substring in the top-level
    ``description``) — no $defs/property change of any kind.

    Returns (clone_path, sha_v240, sha_v241).
    """
    clone, sha_v240 = base_fake_clone
    doe_schema_dir = clone / "coordinator" / "cockpit-contract" / "schema"

    version_only_schema = dict(_V240_SCHEMA)
    version_only_schema["version"] = "2.4.1"
    version_only_schema["description"] = "base v2.4.1 schema"
    (doe_schema_dir / "cockpit-contract.schema.json").write_text(
        json.dumps(version_only_schema, indent=2), encoding="utf-8"
    )
    # entity.schema.json is untouched — no shape change anywhere in this commit.

    sha_v241 = _make_commit(clone)
    return clone, sha_v240, sha_v241


class TestVersionStampChurnNormalization:
    """AC6 refinement: the delta predicate is conservative on SHAPE, not on the
    version stamp that rewrites on every single bump regardless of shape.
    """

    def test_version_only_bump_produces_no_delta(
        self,
        version_only_bump_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """A pure version-stamp bump (top-level version + description substring,
        nothing else) must NOT be reported as a consumer-visible delta — --ack-major
        must not be required for a version-only re-vendor.
        """
        clone, sha_v240, sha_v241 = version_only_bump_fake_clone
        vendor = isolated_paths["vendor"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha_v241)
        assert delta is False, (
            "pure version-stamp churn (top-level version + description substring) "
            "must not require --ack-major"
        )
        assert surfaces == []

    def test_version_bump_plus_real_shape_change_lists_only_real_surfaces(
        self,
        widened_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """A version bump ACCOMPANIED by a real shape change (content_hash additive
        widen, in both cockpit-contract.schema.json and entity.schema.json) must still
        be detected — and the reported surfaces must be exactly the two files with a
        real shape change, not padded with spurious version-only noise.
        """
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha_v250)
        assert delta is True
        assert len(surfaces) == 2, f"expected exactly 2 real-shape-change surfaces, got: {surfaces}"
        assert any("cockpit-contract.schema.json" in s for s in surfaces)
        assert any("entity.schema.json" in s for s in surfaces)


class TestMajorDeltaAckGate:
    """Inline MAJOR-delta ack gate correctness — refuse without --ack-major, permit with it.

    No on-disk sentinel file is involved anywhere in this suite (retired primitive).
    """

    def test_refuses_without_ack_major(
        self,
        isolated_paths: dict,
    ) -> None:
        """Delta detected + --ack-major NOT passed → sys.exit with remediation text."""
        with pytest.raises(SystemExit) as exc_info:
            _mod._enforce_major_delta_gate(["cockpit-contract.schema.json ($defs changed)"], ack_major=False)
        assert exc_info.value.code != 0

    def test_passes_with_ack_major(
        self,
        isolated_paths: dict,
    ) -> None:
        """Delta detected + --ack-major passed → gate proceeds (no exit)."""
        # Must NOT raise.
        _mod._enforce_major_delta_gate(["cockpit-contract.schema.json ($defs changed)"], ack_major=True)

    def test_remediation_text_includes_detected_surfaces(
        self,
        isolated_paths: dict,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Refusal message names the detected delta surfaces so the operator can review them.

        Review: code-reviewer (F4) — the test name promised the rendered message was
        checked; the body previously only asserted a non-zero exit. Now captures stderr
        and asserts the surface string is actually present in the printed remediation text.
        """
        with pytest.raises(SystemExit):
            _mod._enforce_major_delta_gate(["entity.schema.json (new property)"], ack_major=False)

        captured = capsys.readouterr()
        assert "entity.schema.json (new property)" in captured.err, (
            "detected surface must appear verbatim in the printed remediation text (stderr)"
        )


# ---------------------------------------------------------------------------
# 5. Additive-optional field widen (content_hash, 2.4.0→2.5.0) — explicit case
#    AC10: "this case must be explicit so the test suite encodes the conservative
#    predicate, not just the placeholder→concrete idiom."
# ---------------------------------------------------------------------------

class TestAdditiveOptionalWidenRefuses:
    """AC10 explicit requirement: content_hash-shaped additive widen MUST refuse without ack."""

    def test_additive_widen_detected_as_delta(
        self,
        widened_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """incoming v2.5.0 with content_hash widen vs vendored v2.4.0 → delta detected."""
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]

        # Vendored dir holds v2.4.0 (without content_hash).
        _copy_git_show_to_vendor(clone, sha_v240, vendor)

        # Incoming is the v2.5.0 SHA with the additive content_hash field.
        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha_v250)
        assert delta is True, (
            "ADDITIVE-OPTIONAL FIELD WIDEN (content_hash-shaped) must be detected as a "
            "consumer-visible delta requiring ack — not silently accepted (AC6 / AC10)"
        )
        assert len(surfaces) >= 1

    def test_additive_widen_refuses_without_ack_major(
        self,
        widened_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Delta detected + --ack-major NOT passed → _enforce_major_delta_gate refuses."""
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha_v250)
        assert delta is True

        with pytest.raises(SystemExit) as exc_info:
            _mod._enforce_major_delta_gate(surfaces, ack_major=False)
        assert exc_info.value.code != 0

    def test_additive_widen_permitted_with_ack_major(
        self,
        widened_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Delta detected + --ack-major passed → gate passes (re-vendor may proceed)."""
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)

        delta, surfaces = _mod._detect_consumer_visible_delta(clone, sha_v250)
        assert delta is True

        # Must NOT raise — --ack-major explicitly acknowledges the detected delta.
        _mod._enforce_major_delta_gate(surfaces, ack_major=True)


# ---------------------------------------------------------------------------
# 6. Idempotent no-op
#    Tests _is_already_vendored: returns True when pin + content match, False otherwise.
# ---------------------------------------------------------------------------

class TestIdempotentNoOp:
    """AC9: re-running at the same ref with unchanged bundle returns True (no-op exit 0)."""

    def test_already_vendored_returns_true(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Matching pin SHA + byte-identical files → _is_already_vendored returns True."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        # Populate vendored dir with exact bytes from the fake clone.
        _copy_git_show_to_vendor(clone, sha, vendor)
        # Write the correct full SHA to the pin file.
        pin.write_text(sha + "\n", encoding="utf-8")

        result = _mod._is_already_vendored(clone, sha)
        assert result is True, "Already-matching vendor + pin must return True (no-op)"

    def test_different_sha_returns_false(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Pin SHA differs from the requested SHA → not idempotent → returns False."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha, vendor)
        pin.write_text("0" * 40 + "\n", encoding="utf-8")  # wrong SHA

        result = _mod._is_already_vendored(clone, sha)
        assert result is False

    def test_missing_pin_returns_false(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """No pin file on disk → not idempotent → returns False."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha, vendor)
        assert not pin.exists()

        result = _mod._is_already_vendored(clone, sha)
        assert result is False

    def test_content_mismatch_returns_false(
        self,
        base_fake_clone,
        isolated_paths: dict,
    ) -> None:
        """Pin SHA matches but vendored file bytes differ → re-vendor required → False."""
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha, vendor)
        pin.write_text(sha + "\n", encoding="utf-8")

        # Corrupt a vendored file after copying.
        (vendor / "schema" / "cockpit-contract.schema.json").write_bytes(b"TAMPERED")

        result = _mod._is_already_vendored(clone, sha)
        assert result is False


# ---------------------------------------------------------------------------
# 6b. _write_pin cosmetic display-path safety
#     PIN_SHA_FILE.relative_to(_REPO_ROOT) is a display-only computation; it must
#     never raise even when PIN_SHA_FILE lives outside _REPO_ROOT.
# ---------------------------------------------------------------------------

class TestWritePinDisplayPathSafety:
    """_write_pin must never raise ValueError from the cosmetic relative_to() log line."""

    def test_write_pin_out_of_tree_path_does_not_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PIN_SHA_FILE outside _REPO_ROOT: _write_pin must not raise, and the SHA
        must still be written to disk (the pin write itself is unaffected by the
        cosmetic display-path fallback).
        """
        out_of_tree_pin = tmp_path / "out-of-tree" / ".doe-ref-pin"
        out_of_tree_pin.parent.mkdir(parents=True)

        unrelated_repo_root = tmp_path / "unrelated-repo-root"
        unrelated_repo_root.mkdir()

        monkeypatch.setattr(_mod, "PIN_SHA_FILE", out_of_tree_pin)
        monkeypatch.setattr(_mod, "_REPO_ROOT", unrelated_repo_root)

        sha = "b" * 40

        _mod._write_pin(sha)  # Must NOT raise ValueError.

        assert out_of_tree_pin.exists()
        assert out_of_tree_pin.read_text(encoding="utf-8").strip() == sha


# ---------------------------------------------------------------------------
# 7. Post-vendor run_drift_check() invoked
#    Tests _post_vendor_drift_check: mocks run_drift_check; verifies it is called.
# ---------------------------------------------------------------------------

class TestPostVendorDriftCheckInvoked:
    """AC8: _post_vendor_drift_check must call run_drift_check(); failure paths tested."""

    def test_drift_check_invoked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """run_drift_check() is called with the doe_clone argument (AC8)."""
        calls: list = []

        def _mock_drift_check(doe_clone=None, **_: Any) -> None:
            calls.append(doe_clone)

        monkeypatch.setattr(_mod, "run_drift_check", _mock_drift_check)

        fake_clone = tmp_path / "fake-clone"
        _mod._post_vendor_drift_check(fake_clone)

        assert len(calls) == 1, "run_drift_check must be invoked exactly once"
        assert calls[0] == fake_clone

    def test_drift_error_causes_die(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """DriftError raised by run_drift_check() propagates as sys.exit (AC8)."""
        from coordinator_core.ops.emit.doe_drift import DriftError

        def _mock_drift_check(doe_clone=None, **_: Any) -> None:
            raise DriftError("simulated drift: pinned version lags min_supported")

        monkeypatch.setattr(_mod, "run_drift_check", _mock_drift_check)

        with pytest.raises(SystemExit) as exc_info:
            _mod._post_vendor_drift_check(tmp_path / "fake-clone")
        assert exc_info.value.code != 0

    def test_drift_warning_causes_die(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Unexpected DriftWarning from run_drift_check() → _die (non-zero exit)."""
        from coordinator_core.ops.emit.doe_drift import DriftWarning

        def _mock_drift_check(doe_clone=None, **_: Any) -> None:
            warnings.warn("simulated pin-absent warning", DriftWarning, stacklevel=2)

        monkeypatch.setattr(_mod, "run_drift_check", _mock_drift_check)

        with pytest.raises(SystemExit) as exc_info:
            _mod._post_vendor_drift_check(tmp_path / "fake-clone")
        assert exc_info.value.code != 0

    def test_clean_drift_check_passes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """run_drift_check() returning cleanly → _post_vendor_drift_check does not exit."""
        def _mock_drift_check(doe_clone=None, **_: Any) -> None:
            pass  # Green — no exception, no warning.

        monkeypatch.setattr(_mod, "run_drift_check", _mock_drift_check)

        # Must NOT raise.
        _mod._post_vendor_drift_check(tmp_path / "fake-clone")


# ---------------------------------------------------------------------------
# 8. main()-level orchestration: dry-run-before-gate ordering + no-delta ungated flow
#    (Review: code-reviewer F3 — no test previously drove main() itself; a prior review
#    already caught one regression (F2) where the gate fired before the --dry-run report,
#    and only a main()-level test can guard the wiring order going forward.)
# ---------------------------------------------------------------------------

class TestMainOrchestration:
    """main()-level integration: dry-run-before-gate ordering; no-delta fully-ungated flow."""

    def _patch_common(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clone: Path,
        sha: str,
    ) -> None:
        """Wire main() up to a fake clone/sha without touching network or pnpm."""
        monkeypatch.setattr(_mod, "resolve_doe_clone", lambda: clone)
        monkeypatch.setattr(_mod, "_fetch_and_resolve_sha", lambda _clone, _ref: sha)
        monkeypatch.setattr(_mod, "run_drift_check", lambda **_: None)

    def test_dry_run_with_major_delta_exits_before_gate(
        self,
        widened_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--dry-run + MAJOR delta detected + no --ack-major -> exits 0, gate never invoked,
        no pin written.

        Guards the exact regression class the inline code comment (main(), Step 3/dry-run
        block) says a prior review finding (F2) already had to fix once: the gate must not
        fire before the --dry-run report, and --dry-run must never reach the gate at all.
        """
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)
        self._patch_common(monkeypatch, clone, sha_v250)

        gate_calls: list = []
        monkeypatch.setattr(
            _mod,
            "_enforce_major_delta_gate",
            lambda *a, **k: gate_calls.append((a, k)),
        )

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py", "--dry-run"])

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()

        assert exc_info.value.code == 0, "--dry-run with a MAJOR delta must exit 0, not refuse"
        assert gate_calls == [], "_enforce_major_delta_gate must NOT be invoked under --dry-run"
        assert not pin.exists(), "--dry-run must never write the pin"

    def test_no_delta_full_flow_never_invokes_gate(
        self,
        base_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A no-delta full (non-dry-run) flow proceeds straight through copy/verify/pin/
        drift-check with zero ack-gate logic invoked anywhere.
        """
        clone, sha = base_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        # Vendored dir already matches the incoming ref exactly -> no delta.
        _copy_git_show_to_vendor(clone, sha, vendor)
        self._patch_common(monkeypatch, clone, sha)
        # _write_pin logs PIN_SHA_FILE.relative_to(_REPO_ROOT) purely for cosmetic
        # display; isolated_paths repoints PIN_SHA_FILE under tmp_path, which is not
        # a subpath of the real _REPO_ROOT, so _REPO_ROOT must be repointed too.
        monkeypatch.setattr(_mod, "_REPO_ROOT", isolated_paths["vendor"].parent)

        gate_calls: list = []
        monkeypatch.setattr(
            _mod,
            "_enforce_major_delta_gate",
            lambda *a, **k: gate_calls.append((a, k)),
        )

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py"])

        _mod.main()  # Must NOT sys.exit with a non-zero code, must NOT raise.

        assert gate_calls == [], "no-delta flow must never invoke the ack-gate logic"
        assert pin.exists(), "no-delta full flow must write the pin"
        assert pin.read_text(encoding="utf-8").strip() == sha


# ---------------------------------------------------------------------------
# 9. Direction-aware downgrade guard (2026-07-23) — _parse_semver / _detect_downgrade /
#    _read_vendored_version / _enforce_downgrade_gate unit coverage, plus main()-level
#    integration coverage for the two load-bearing holes: an acked downgrade must still
#    refuse without --allow-downgrade (--ack-major does not satisfy it), and a PURE
#    version downgrade with zero shape delta must refuse even though
#    _detect_consumer_visible_delta reports no delta at all (hole #2 — the wider hole).
# ---------------------------------------------------------------------------

class TestParseSemver:
    """_parse_semver: strict numeric MAJOR.MINOR.PATCH tuple parsing."""

    def test_parses_valid_semver(self) -> None:
        assert _mod._parse_semver("2.17.0") == (2, 17, 0)

    def test_rejects_non_three_part(self) -> None:
        assert _mod._parse_semver("2.17") is None
        assert _mod._parse_semver("2.17.0.1") is None

    def test_rejects_non_numeric_parts(self) -> None:
        assert _mod._parse_semver("2.17.0-rc1") is None
        assert _mod._parse_semver("banana") is None
        assert _mod._parse_semver("v2.17.0") is None

    def test_numeric_not_lexical_ordering(self) -> None:
        """10.0.0 must compare GREATER than 9.0.0 numerically -- a lexical string
        compare would get this backwards ('10.0.0' < '9.0.0' lexically).
        """
        assert _mod._parse_semver("10.0.0") > _mod._parse_semver("9.0.0")


class TestDetectDowngrade:
    """_detect_downgrade: direction-aware numeric comparison, independent of shape delta.

    Review: code-reviewer (2026-07-23 F1) — _detect_downgrade now returns a
    DowngradeStatus tri-state, not a bare bool; assertions below compare against the
    enum members instead of True/False.
    """

    def test_lower_incoming_is_downgrade(self) -> None:
        assert (
            _mod._detect_downgrade("3.1.0", "2.21.0", allow_downgrade=False)
            is _mod.DowngradeStatus.DOWNGRADE
        )

    def test_higher_incoming_is_not_downgrade(self) -> None:
        assert (
            _mod._detect_downgrade("2.21.0", "3.1.0", allow_downgrade=False)
            is _mod.DowngradeStatus.NOT_DOWNGRADE
        )

    def test_equal_versions_not_a_downgrade(self) -> None:
        assert (
            _mod._detect_downgrade("2.4.0", "2.4.0", allow_downgrade=False)
            is _mod.DowngradeStatus.NOT_DOWNGRADE
        )

    def test_no_currently_vendored_version_proceeds(self) -> None:
        """current_version=None (greenfield vendor) must never be flagged a downgrade."""
        assert (
            _mod._detect_downgrade(None, "1.0.0", allow_downgrade=False)
            is _mod.DowngradeStatus.NOT_DOWNGRADE
        )

    def test_numeric_not_lexical_comparison(self) -> None:
        """9.0.0 -> 10.0.0 is an UPGRADE numerically; a lexical string compare would
        wrongly read '10.0.0' < '9.0.0' and flag this as a downgrade.
        """
        assert (
            _mod._detect_downgrade("9.0.0", "10.0.0", allow_downgrade=False)
            is _mod.DowngradeStatus.NOT_DOWNGRADE
        )

    def test_numeric_not_lexical_comparison_reverse(self) -> None:
        """10.0.0 -> 9.0.0 IS a real downgrade -- the numeric tuple must catch this
        even though the raw strings compare the 'right' way by accident here.
        """
        assert (
            _mod._detect_downgrade("10.0.0", "9.0.0", allow_downgrade=False)
            is _mod.DowngradeStatus.DOWNGRADE
        )

    def test_unparseable_current_version_fails_loud(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _mod._detect_downgrade("not-a-semver", "2.4.0", allow_downgrade=False)
        assert exc_info.value.code != 0

    def test_unparseable_incoming_version_fails_loud(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _mod._detect_downgrade("2.4.0", "not-a-semver", allow_downgrade=False)
        assert exc_info.value.code != 0

    def test_unparseable_version_with_allow_downgrade_proceeds_with_warning(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """2026-07-23 review fix: --allow-downgrade must genuinely reach this path --
        it previously could not (the parameter did not exist), leaving the operator
        stuck in a refuse-loop with no working escape hatch.
        """
        result = _mod._detect_downgrade("not-a-semver", "2.4.0", allow_downgrade=True)
        assert result is _mod.DowngradeStatus.UNVERIFIABLE_OVERRIDDEN, (
            "unverifiable direction + --allow-downgrade must proceed, not die"
        )

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "cannot determine re-vendor direction" in captured.out
        assert "--allow-downgrade" in captured.out

    def test_unparseable_version_with_ack_major_alone_still_dies(self) -> None:
        """--ack-major must NOT leak into the direction gate -- only allow_downgrade
        (and dry_run, for diagnostic purposes) may bypass the fail-loud path.
        """
        with pytest.raises(SystemExit) as exc_info:
            _mod._detect_downgrade("not-a-semver", "2.4.0", allow_downgrade=False)
        assert exc_info.value.code != 0

    def test_unparseable_version_under_dry_run_proceeds_with_warning(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """Follow-on fix: --dry-run must not die on this path either -- it is exactly
        the diagnostic tool an operator would reach for to investigate this condition.
        """
        result = _mod._detect_downgrade(
            "not-a-semver", "2.4.0", allow_downgrade=False, dry_run=True
        )
        assert result is _mod.DowngradeStatus.UNVERIFIABLE_OVERRIDDEN, (
            "unverifiable direction under --dry-run must proceed, not die"
        )

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "cannot determine re-vendor direction" in captured.out
        assert "--dry-run makes no writes" in captured.out


class TestReadVendoredVersion:
    """_read_vendored_version: greenfield / present / unreadable-version cases."""

    def test_missing_vendored_file_returns_none(self, isolated_paths: dict) -> None:
        assert _mod._read_vendored_version() is None

    def test_present_version_returned(self, isolated_paths: dict) -> None:
        vendor = isolated_paths["vendor"]
        (vendor / "schema" / "cockpit-contract.schema.json").write_text(
            json.dumps({"version": "2.4.0", "description": "x"}), encoding="utf-8"
        )
        assert _mod._read_vendored_version() == "2.4.0"

    def test_missing_version_field_returns_none(self, isolated_paths: dict) -> None:
        """A vendored file present but with no `version` field is greenfield-shaped
        (nothing to compare), NOT a fail-loud case.
        """
        vendor = isolated_paths["vendor"]
        (vendor / "schema" / "cockpit-contract.schema.json").write_text(
            json.dumps({"description": "x"}), encoding="utf-8"
        )
        assert _mod._read_vendored_version() is None


class TestEnforceDowngradeGate:
    """_enforce_downgrade_gate: refuse without --allow-downgrade, permit with it."""

    def test_refuses_without_allow_downgrade(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _mod._enforce_downgrade_gate("3.1.0", "2.21.0", allow_downgrade=False)
        assert exc_info.value.code != 0

    def test_passes_with_allow_downgrade(self) -> None:
        _mod._enforce_downgrade_gate("3.1.0", "2.21.0", allow_downgrade=True)  # must not raise

    def test_remediation_text_includes_versions(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit):
            _mod._enforce_downgrade_gate("3.1.0", "2.21.0", allow_downgrade=False)
        captured = capsys.readouterr()
        assert "3.1.0" in captured.err
        assert "2.21.0" in captured.err
        assert "--allow-downgrade" in captured.err


@pytest.fixture
def version_downgrade_fake_clone(base_fake_clone):
    """Extends base_fake_clone (committed at v2.4.0): adds a second commit that ONLY
    lowers the version stamp to v2.0.0 (top-level ``version`` field + the version
    substring in the top-level ``description``) -- no $defs/property change of any
    kind. Simulates hole #2: a pure version downgrade with zero shape delta, which
    ``_detect_consumer_visible_delta`` alone would report as `delta_detected is False`.

    Returns (clone_path, sha_v240, sha_v200).
    """
    clone, sha_v240 = base_fake_clone
    doe_schema_dir = clone / "coordinator" / "cockpit-contract" / "schema"

    version_downgrade_schema = dict(_V240_SCHEMA)
    version_downgrade_schema["version"] = "2.0.0"
    version_downgrade_schema["description"] = "base v2.0.0 schema"
    (doe_schema_dir / "cockpit-contract.schema.json").write_text(
        json.dumps(version_downgrade_schema, indent=2), encoding="utf-8"
    )
    # entity.schema.json is untouched -- no shape change anywhere in this commit.

    sha_v200 = _make_commit(clone)
    return clone, sha_v240, sha_v200


@pytest.fixture
def unparseable_incoming_version_fake_clone(base_fake_clone):
    """Extends base_fake_clone (committed at v2.4.0): adds a second commit whose
    top-level ``version`` field is NOT a parseable semver string. Direction is
    unverifiable against this ref -- used to exercise the fail-loud / --allow-downgrade
    override / --dry-run diagnostic-only paths at the main()-integration level.

    Returns (clone_path, sha_v240, sha_garbled).
    """
    clone, sha_v240 = base_fake_clone
    doe_schema_dir = clone / "coordinator" / "cockpit-contract" / "schema"

    garbled_schema = dict(_V240_SCHEMA)
    garbled_schema["version"] = "not-a-semver"
    garbled_schema["description"] = "garbled version schema"
    (doe_schema_dir / "cockpit-contract.schema.json").write_text(
        json.dumps(garbled_schema, indent=2), encoding="utf-8"
    )

    sha_garbled = _make_commit(clone)
    return clone, sha_v240, sha_garbled


class TestDowngradeGuardMainIntegration:
    """main()-level integration: the downgrade guard is independent of the shape-delta
    gate and of --ack-major, and refuses a pure version downgrade even when
    _detect_consumer_visible_delta reports no delta at all.
    """

    def _patch_common(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clone: Path,
        sha: str,
        isolated_paths: dict,
    ) -> None:
        """Wire main() up to a fake clone/sha without touching network or pnpm."""
        monkeypatch.setattr(_mod, "resolve_doe_clone", lambda: clone)
        monkeypatch.setattr(_mod, "_fetch_and_resolve_sha", lambda _clone, _ref: sha)
        monkeypatch.setattr(_mod, "run_drift_check", lambda **_: None)
        # _write_pin logs PIN_SHA_FILE.relative_to(_REPO_ROOT) purely for cosmetic
        # display; isolated_paths repoints PIN_SHA_FILE under tmp_path, which is not
        # a subpath of the real _REPO_ROOT, so _REPO_ROOT must be repointed too.
        monkeypatch.setattr(_mod, "_REPO_ROOT", isolated_paths["vendor"].parent)

    def test_pure_version_downgrade_no_shape_delta_refused(
        self,
        version_downgrade_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC (hole #2): incoming has an OLDER version but IDENTICAL shape vs vendored
        -- _detect_consumer_visible_delta reports no delta at all, yet the downgrade
        guard must still refuse. This is the most important case in this suite.
        """
        clone, sha_v240, sha_v200 = version_downgrade_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)  # currently vendored: 2.4.0
        self._patch_common(monkeypatch, clone, sha_v200, isolated_paths)  # incoming: 2.0.0

        # Sanity check: no shape delta at all for this pair (hole #2 setup).
        delta_detected, _surfaces = _mod._detect_consumer_visible_delta(clone, sha_v200)
        assert delta_detected is False, "fixture must reproduce a PURE version downgrade"

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py"])

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0
        assert not pin.exists(), "refused downgrade must not write the pin"

    def test_pure_version_downgrade_permitted_with_allow_downgrade(
        self,
        version_downgrade_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same pure-version-downgrade setup as above, but with --allow-downgrade
        passed -- no shape delta exists here, so --ack-major is not needed.
        """
        clone, sha_v240, sha_v200 = version_downgrade_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)
        self._patch_common(monkeypatch, clone, sha_v200, isolated_paths)

        monkeypatch.setattr(
            sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py", "--allow-downgrade"]
        )

        _mod.main()  # must NOT raise / sys.exit non-zero

        assert pin.exists()
        assert pin.read_text(encoding="utf-8").strip() == sha_v200

    def test_downgrade_with_shape_delta_refused_by_ack_major_alone(
        self,
        widened_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC: an acked downgrade must still refuse. Currently-vendored is v2.5.0 (with
        the content_hash widen); incoming is the OLDER v2.4.0 (without it) -- this is
        BOTH a downgrade AND a real shape delta (field removed). --ack-major alone
        (reviewing the shape) must NOT satisfy the direction-aware downgrade gate.
        """
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v250, vendor)  # currently vendored: 2.5.0
        self._patch_common(monkeypatch, clone, sha_v240, isolated_paths)  # incoming: 2.4.0 (older)

        monkeypatch.setattr(
            sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py", "--ack-major"]
        )

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0
        assert not pin.exists(), "--ack-major alone must not satisfy the downgrade gate"

    def test_downgrade_with_shape_delta_refused_by_allow_downgrade_alone(
        self,
        widened_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Mirror of test_downgrade_with_shape_delta_refused_by_ack_major_alone
        (2026-07-23 review finding F4): only --allow-downgrade passed (no --ack-major)
        against the same downgrade-plus-shape-delta fixture. main() enforces the
        major-delta gate BEFORE the downgrade gate, so this must refuse specifically
        at the --ack-major gate -- pinning the gate ORDER against a future reorder of
        the two `if` blocks in main().
        """
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v250, vendor)  # currently vendored: 2.5.0
        self._patch_common(monkeypatch, clone, sha_v240, isolated_paths)  # incoming: 2.4.0 (older)

        monkeypatch.setattr(
            sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py", "--allow-downgrade"]
        )

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0
        assert not pin.exists(), "--allow-downgrade alone must not satisfy the major-delta gate"

        captured = capsys.readouterr()
        assert "Consumer-visible (MAJOR / shape-changing) delta detected" in captured.err
        assert "Downgrade detected: the currently-vendored" not in captured.err

    def test_downgrade_with_shape_delta_permitted_with_both_flags(
        self,
        widened_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same acked-downgrade-plus-shape-delta setup, but BOTH --ack-major and
        --allow-downgrade are passed -- both flags are required together to land it.
        """
        clone, sha_v240, sha_v250 = widened_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v250, vendor)
        self._patch_common(monkeypatch, clone, sha_v240, isolated_paths)

        monkeypatch.setattr(
            sys,
            "argv",
            ["claude-klabauter-revendor-cockpit-contract.py", "--ack-major", "--allow-downgrade"],
        )

        _mod.main()  # must NOT raise / sys.exit non-zero

        assert pin.exists()
        assert pin.read_text(encoding="utf-8").strip() == sha_v240

    def test_upgrade_unaffected_no_flags_needed(
        self,
        version_only_bump_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ordinary upgrade (v2.4.0 -> v2.4.1, no shape change) must proceed with
        NO flags at all -- the downgrade guard must not fire on non-downgrades.
        """
        clone, sha_v240, sha_v241 = version_only_bump_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)
        self._patch_common(monkeypatch, clone, sha_v241, isolated_paths)

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py"])

        _mod.main()  # must NOT raise / sys.exit non-zero

        assert pin.exists()
        assert pin.read_text(encoding="utf-8").strip() == sha_v241

    def test_dry_run_reports_downgrade_without_writing(
        self,
        version_downgrade_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """--dry-run on a pure version downgrade reports the downgrade + from->to
        versions + the --allow-downgrade requirement, and writes nothing.
        """
        clone, sha_v240, sha_v200 = version_downgrade_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)
        self._patch_common(monkeypatch, clone, sha_v200, isolated_paths)

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py", "--dry-run"])

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Downgrade detected: True" in captured.out
        assert "2.4.0" in captured.out
        assert "2.0.0" in captured.out
        assert not pin.exists()

    def test_dry_run_with_unparseable_version_exits_zero_not_dies(
        self,
        unparseable_incoming_version_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Follow-on fix: --dry-run must not die when direction is unverifiable --
        it is the diagnostic tool an operator would reach for to investigate this
        exact condition. Must exit 0, report the unverifiable-direction warning, and
        write nothing (no --allow-downgrade needed for a dry-run to complete).
        """
        clone, sha_v240, sha_garbled = unparseable_incoming_version_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)  # currently vendored: 2.4.0
        self._patch_common(monkeypatch, clone, sha_garbled, isolated_paths)

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py", "--dry-run"])

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code == 0, "--dry-run must exit 0 even when direction is unverifiable"

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "cannot determine re-vendor direction" in captured.out
        assert "--dry-run makes no writes" in captured.out
        assert not pin.exists()

    def test_pure_unparseable_version_no_flags_dies_no_write(
        self,
        unparseable_incoming_version_fake_clone,
        isolated_paths: dict,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Main()-level coverage for the most safety-critical branch of the whole guard
        (2026-07-23 review finding F3): no --allow-downgrade, no --dry-run, direction
        unverifiable -- must fail loud through main() and write nothing. Every other
        branch of this guard (pure downgrade refuse/allow, dry-run downgrade report,
        dry-run unparseable override) already has main()-level coverage; only this,
        the actual fail-loud exit, was unit-tested at the _detect_downgrade level only.
        """
        clone, sha_v240, sha_garbled = unparseable_incoming_version_fake_clone
        vendor = isolated_paths["vendor"]
        pin = isolated_paths["pin"]

        _copy_git_show_to_vendor(clone, sha_v240, vendor)  # currently vendored: 2.4.0
        self._patch_common(monkeypatch, clone, sha_garbled, isolated_paths)

        monkeypatch.setattr(sys, "argv", ["claude-klabauter-revendor-cockpit-contract.py"])

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0
        assert not pin.exists(), "unverifiable direction with no override must not write the pin"

        captured = capsys.readouterr()
        assert "Cannot determine re-vendor direction" in captured.err


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _copy_git_show_to_vendor(clone: Path, sha: str, vendor: Path) -> None:
    """Copy schema/ subtree bytes from the fake-clone at <sha> to <vendor>.

    Uses the same real git-show calls that the script itself uses, so the bytes
    are guaranteed to match for idempotent/byte-identity tests.

    Negative-spec (2026-07-21): does NOT copy src/ — schema/ is the only vendored
    subtree (upstream commit 7cca4d4c deleted the TS/Zod toolchain).
    """
    _doe_schema_rel = "coordinator/cockpit-contract/schema"

    # Use git ls-tree to enumerate files, then git show for bytes.
    r = _git_local(clone, "ls-tree", "-r", "--name-only", sha, _doe_schema_rel)
    if r.returncode != 0:
        return
    prefix = _doe_schema_rel.rstrip("/") + "/"
    for line in r.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        rel_name = line[len(prefix):]
        if not rel_name:
            continue
        show_r = _git_local(clone, "show", f"{sha}:{_doe_schema_rel}/{rel_name}")
        if show_r.returncode != 0:
            continue
        dest = vendor / "schema" / rel_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(show_r.stdout)
