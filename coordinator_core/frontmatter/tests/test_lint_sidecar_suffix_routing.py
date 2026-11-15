"""A sidecar suffix (annotations, premise-check) is never its parent artifact type."""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter import schema_validate as sv


@pytest.mark.parametrize(
    "path",
    [
        "docs/decisions/2026-07-14-fleet-claim-identity-space-contract.annotations.md",
        "docs/plans/2026-11-14-foo.annotations.md",
        "docs/plans/2026-11-14-foo.premise-check.md",
        "docs/plans/2026-10/2026-10-25-x-77db60.first-red-ledger.md",
        "docs/plans/2026-10/2026-10-29-group-em-references-split.cut-ledger.md",
        "docs/plans/2026-11/2026-11-04-hnd-773493.c6-matrix.md",
        "docs/plans/2026-11/2026-11-15-foo-33835f.evidence.yaml",
        "docs/plans/REDIRECTS.md",
        "state/handoffs/REDIRECTS.md",
    ],
)
def test_sidecar_suffix_is_exempt(path):
    assert sv._lint_is_sidecar_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "docs/decisions/2026-07-14-fleet-claim-identity-space-contract.md",
        "docs/plans/2026-11-14-annotations-cleanup.md",
        "docs/plans/2026-11-14-premise-check-rework.md",
        "docs/plans/2026-11-14-ledger-cut-matrix-evidence.md",
    ],
)
def test_parent_artifact_is_not_exempt(path):
    assert sv._lint_is_sidecar_file(path) is False


def _schemas():
    from pathlib import Path

    return sv.load_schemas(Path(sv.__file__).parent / "schemas")


_VERDICT = "docs/research/spikes/2026-11-01-x/verdict.md"


def test_declared_type_naming_a_schema_beats_the_path():
    resolved = sv.match_schema(_VERDICT, {"type": "plan"}, _schemas())
    assert resolved is not None and resolved["schemaName"] == "plan"


def test_path_routes_when_nothing_is_declared():
    resolved = sv.match_schema(_VERDICT, {"title": "t"}, _schemas())
    assert resolved is not None and resolved["schemaName"] == "spike-result"


def test_unregistered_type_is_content_and_the_path_still_routes():
    resolved = sv.match_schema(_VERDICT, {"type": "inconclusive"}, _schemas())
    assert resolved is not None and resolved["schemaName"] == "spike-result"


def test_declared_kind_still_outranks_declared_type():
    resolved = sv.match_schema(_VERDICT, {"kind": "session-handoff", "type": "decision"}, _schemas())
    assert resolved is not None and resolved["schemaName"] == "handoff"
