"""Type-enumeration tests for coordinator_core.ops.docgen.type_enum (C3, AC4).

Covers:
  - synthetic-manifest unit tests (no live oracle required): known_types()/
    doc_types()/queue_types()/sidecar_types()/schema_name_for()/is_offerable()
    correctly reconstruct the manifest's own ``_reconstruction`` formula, and
    ManifestReadError fires loud on a missing/malformed manifest.
  - AC4 conformance: known_types() is set-EQUAL (not merely a subset) to
    coordinator-doc-new's own post-union _KNOWN_TYPES, reconstructed here by
    loading bin/lib/coordinator_registry.py directly (resolved
    repo-root-relative — this module migrated in-repo via example-doctrine-repo commit
    b644d5a9) and applying the SAME local "run-report" union the CLI itself
    applies — never hand-copying a second literal type list to compare
    against. The manifest half of this comparison
    (``coordinator/schemas/coordinator-registry.manifest.json``, read by
    ``type_enum.known_types(doe_clone)`` et al.) has NOT migrated in-repo —
    it still resolves the live example-doctrine-repo clone via ``resolve_doe_clone()`` and
    is genuinely skippable when that clone is unavailable; only the
    ``coordinator_registry.py`` module load below was affected by the
    b644d5a9 migration and made fail-loud.

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C3 (AC4)
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from coordinator_core.ops.docgen import type_enum as te
from coordinator_core.ops.emit.doe_drift import DoeResolveError, resolve_doe_clone

# ---------------------------------------------------------------------------
# Live-example-doctrine-repo-clone skip guard for the manifest half of AC4 conformance
# (mirrors coordinator_core/ops/emit/tests/test_doe_drift.py). The manifest
# (coordinator/schemas/coordinator-registry.manifest.json) has NOT migrated
# in-repo, unlike coordinator_registry.py below — this remains a genuine
# optional cross-repo dependency.
# ---------------------------------------------------------------------------

try:
    _DOE_CLONE = resolve_doe_clone()
    _DOE_AVAILABLE = True
except DoeResolveError:
    _DOE_CLONE = None
    _DOE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Oracle resolution for coordinator_registry.py (repo-root-relative — no
# cross-repo clone lookup). This module lives in THIS repo as of example-doctrine-repo commit
# b644d5a9; a missing oracle at the expected path is a broken checkout, not
# an unavailable optional dependency, so resolution failure fails loud.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_REGISTRY_PATH = _REPO_ROOT / "coordinator" / "bin" / "lib" / "coordinator_registry.py"

if not _REGISTRY_PATH.is_file():
    pytest.fail(
        f"oracle coordinator_registry.py not found at {_REGISTRY_PATH} "
        f"(resolved repo-root-relative from {__file__}) — this is a broken "
        "checkout, not an unavailable optional dependency; the oracle has "
        "lived in-repo since example-doctrine-repo commit b644d5a9",
        pytrace=False,
    )


def _load_cli_coordinator_registry():
    """Import bin/lib/coordinator_registry.py directly (repo-root-relative).

    Returns the live module object so tests read its KNOWN_TYPES/DOC_TYPES/
    QUEUE_TYPES directly — never a hand-transcribed copy that could silently
    drift from the oracle.
    """
    spec = importlib.util.spec_from_file_location("_oracle_coordinator_registry", _REGISTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Synthetic-manifest fixtures — no live example-doctrine-repo clone required
# ---------------------------------------------------------------------------

_SYNTHETIC_MANIFEST = {
    "schemaVersion": 1,
    "docTypes": [
        {"type": "plan", "schemaName": "plan", "isSidecar": False, "offerable": True},
        {"type": "handoff", "schemaName": "handoff", "isSidecar": False, "offerable": True},
        {"type": "review", "schemaName": "review-sidecar", "isSidecar": True, "offerable": True, "suffix": "review"},
        {"type": "memo", "schemaName": None, "isSidecar": False, "offerable": False},
    ],
    "queueTypes": ["improvement-queue", "bug-backlog", "debt-backlog"],
    "identity": {"centralReceiverIds": [], "repoAliases": []},
}


@pytest.fixture
def synthetic_clone(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(_SYNTHETIC_MANIFEST), encoding="utf-8")
    return tmp_path


class TestSyntheticManifestReconstruction:
    def test_known_types_unions_doc_types_queue_types_and_supplement(self, synthetic_clone: Path) -> None:
        result = te.known_types(synthetic_clone)
        expected = {"plan", "handoff", "review", "memo"} | {
            "improvement-queue",
            "bug-backlog",
            "debt-backlog",
        } | te.SUPPLEMENTAL_TYPES
        assert result == expected

    def test_doc_types_returns_raw_manifest_rows(self, synthetic_clone: Path) -> None:
        rows = te.doc_types(synthetic_clone)
        assert isinstance(rows, tuple)
        assert {r["type"] for r in rows} == {"plan", "handoff", "review", "memo"}

    def test_queue_types(self, synthetic_clone: Path) -> None:
        assert te.queue_types(synthetic_clone) == frozenset(
            {"improvement-queue", "bug-backlog", "debt-backlog"}
        )

    def test_sidecar_types_only_isSidecar_true(self, synthetic_clone: Path) -> None:
        assert te.sidecar_types(synthetic_clone) == frozenset({"review"})

    def test_schema_name_for_known_type(self, synthetic_clone: Path) -> None:
        assert te.schema_name_for("plan", synthetic_clone) == "plan"

    def test_schema_name_for_null_schema_type(self, synthetic_clone: Path) -> None:
        assert te.schema_name_for("memo", synthetic_clone) is None

    def test_schema_name_for_unknown_type_is_none_not_raise(self, synthetic_clone: Path) -> None:
        assert te.schema_name_for("nonexistent-type", synthetic_clone) is None

    def test_is_offerable_true_and_false(self, synthetic_clone: Path) -> None:
        assert te.is_offerable("plan", synthetic_clone) is True
        assert te.is_offerable("memo", synthetic_clone) is False

    def test_is_offerable_unknown_type_is_false(self, synthetic_clone: Path) -> None:
        assert te.is_offerable("nonexistent-type", synthetic_clone) is False

    # subagent-sidecar is now a manifest docTypes entry (schemas/coordinator-
    # registry.manifest.json), not a SUPPLEMENTAL_TYPES shim — the shim was
    # retired once the manifest carried the type. Only run-report remains a
    # local supplement (its own manifest entry landed earlier and the CLI
    # shim was left in place as harmless-idempotent; see the module comment).
    def test_supplemental_types_no_longer_carries_subagent_sidecar(self) -> None:
        assert "subagent-sidecar" not in te.SUPPLEMENTAL_TYPES
        assert te.SUPPLEMENTAL_TYPES == frozenset({"run-report"})


class TestManifestReadFailures:
    def test_missing_manifest_file_raises_manifest_read_error(self, tmp_path: Path) -> None:
        with pytest.raises(te.ManifestReadError, match="not found"):
            te.load_manifest(tmp_path)

    def test_malformed_json_raises_manifest_read_error(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(te.ManifestReadError, match="not readable/valid JSON"):
            te.load_manifest(tmp_path)

    def test_manifest_missing_doctypes_key_raises(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({"queueTypes": []}), encoding="utf-8")
        with pytest.raises(te.ManifestReadError, match="docTypes"):
            te.load_manifest(tmp_path)

    def test_manifest_missing_queuetypes_key_raises(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps({"docTypes": []}), encoding="utf-8")
        with pytest.raises(te.ManifestReadError, match="queueTypes"):
            te.load_manifest(tmp_path)

    def test_manifest_non_object_root_raises(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(te.ManifestReadError, match="JSON object"):
            te.load_manifest(tmp_path)


# ---------------------------------------------------------------------------
# AC4 — live conformance against the CLI's actual post-union _KNOWN_TYPES
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _DOE_AVAILABLE, reason="example-doctrine-repo clone not available on this machine (manifest.json has not migrated in-repo)")
class TestAC4LiveConformance:
    # 2026-07-28: the class-level `pytestmark = pytest.mark.pending_fix` demotion
    # that used to sit here is RETIRED — all 7 cases pass live against a present
    # example-doctrine-repo clone. The `skipif` above is NOT a demotion and stays: it is the
    # clone-absence guard it always was.
    #
    # The prior comment here is deleted, not reworded. It asserted "the marker was
    # never actually added here" while that very marker sat on the next line —
    # both git history and the example-doctrine-repo memo prompting its removal
    # (cross-repo/archive/2026-07-25-example-doctrine-repo-em-orient-assemble-phantom-verbs.md
    # § P2, "Both use the module-level pytestmark form") confirm it was present.
    # Leaving a false claim adjacent to the code it describes is worse than
    # leaving no comment at all.

    def test_known_types_is_set_equal_to_cli_resolved_known_types(self) -> None:
        """known_types() must be set-EQUAL, not merely a subset, to the CLI's own set.

        Reconstructs coordinator-doc-new's actual post-union ``_KNOWN_TYPES`` by
        loading its shared ``coordinator_registry`` module live from the clone and
        applying the one remaining local supplement the CLI's own module-level
        line applies (``_KNOWN_TYPES = _KNOWN_TYPES | frozenset({"run-report"})``).
        The former ``subagent-sidecar`` union was retired once the manifest grew
        its own "subagent-sidecar" docTypes entry — ``oracle.KNOWN_TYPES`` already
        carries it via the manifest import, so no local union is needed here.
        A missing type on either side fails this assertion loud (AC4).
        """
        oracle = _load_cli_coordinator_registry()
        cli_known_types = oracle.KNOWN_TYPES | frozenset({"run-report"})
        ours = te.known_types(_DOE_CLONE)
        missing_from_ours = cli_known_types - ours
        extra_in_ours = ours - cli_known_types
        assert not missing_from_ours, f"type_enum.known_types() is missing: {sorted(missing_from_ours)}"
        assert not extra_in_ours, f"type_enum.known_types() has types the CLI does not know: {sorted(extra_in_ours)}"
        assert ours == cli_known_types

    def test_doc_types_type_set_matches_oracle_doc_types(self) -> None:
        oracle = _load_cli_coordinator_registry()
        oracle_types = frozenset(d["type"] for d in oracle.DOC_TYPES)
        ours = frozenset(d["type"] for d in te.doc_types(_DOE_CLONE))
        assert ours == oracle_types

    def test_sidecar_types_matches_oracle(self) -> None:
        oracle = _load_cli_coordinator_registry()
        assert te.sidecar_types(_DOE_CLONE) == oracle.SIDECAR_TYPES

    def test_queue_types_matches_oracle(self) -> None:
        oracle = _load_cli_coordinator_registry()
        assert te.queue_types(_DOE_CLONE) == oracle.QUEUE_TYPES

    def test_schema_name_for_matches_oracle_for_every_doc_type(self) -> None:
        oracle = _load_cli_coordinator_registry()
        for entry in oracle.DOC_TYPES:
            assert te.schema_name_for(entry["type"], _DOE_CLONE) == entry.get("schemaName")

    def test_is_offerable_matches_oracle_for_every_doc_type(self) -> None:
        oracle = _load_cli_coordinator_registry()
        for entry in oracle.DOC_TYPES:
            assert te.is_offerable(entry["type"], _DOE_CLONE) == bool(entry.get("offerable", False))

    def test_known_types_defaults_to_live_resolve_doe_clone(self) -> None:
        """Calling known_types() with no argument resolves the live clone itself."""
        assert te.known_types() == te.known_types(_DOE_CLONE)
