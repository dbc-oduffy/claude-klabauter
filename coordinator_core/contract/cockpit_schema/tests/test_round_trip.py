from __future__ import annotations

from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope
from coordinator_core.contract.cockpit_schema.common import IsoDateTime, MachineSlug, OwnerSlug
from coordinator_core.contract.cockpit_schema.entities.summaries import HandoffStatus
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_safe_parse_ok,
)


def test_isodatetime_bare_z_suffix():
    assert zod_safe_parse_ok(IsoDateTime, "2026-06-22T03:06:15Z")


def test_isodatetime_numeric_offset():
    assert zod_safe_parse_ok(IsoDateTime, "2026-06-22T03:06:15+00:00")


def test_isodatetime_no_offset_rejected():
    assert not zod_safe_parse_ok(IsoDateTime, "2026-06-22T03:06:15")


def test_machine_slug_empty_string_rejected():
    assert not zod_safe_parse_ok(MachineSlug, "")


def test_owner_slug_empty_string_rejected():
    assert not zod_safe_parse_ok(OwnerSlug, "")


def test_handoff_status_superseded_rejected():
    assert not zod_safe_parse_ok(HandoffStatus, "superseded")


def test_handoff_status_open_accepted():
    assert zod_safe_parse_ok(HandoffStatus, "open")


def test_handoff_status_claimed_accepted():
    assert zod_safe_parse_ok(HandoffStatus, "claimed")


_BASE_PROV = {
    "repo": "test-repo",
    "path": "state/handoffs/test.md",
    "observed_at": "2026-06-22T03:06:15Z",
    "derivation": "parsed",
    "entity_anchor": None,
}
_REAL_REF = {"branch": "work/machine-a/2026-07-03", "sha": "abc1234f"}


def test_provenance_a_github_graphql_real_ref_passes():
    v = {**_BASE_PROV, "source_kind": "github_graphql", "ref": _REAL_REF}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_b_local_fs_ref_null_passes():
    v = {**_BASE_PROV, "source_kind": "local_fs", "ref": None}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_c_github_graphql_ref_null_rejected():
    v = {**_BASE_PROV, "source_kind": "github_graphql", "ref": None}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_d_local_fs_real_ref_rejected():
    v = {**_BASE_PROV, "source_kind": "local_fs", "ref": _REAL_REF}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_e_ref_key_omitted_entirely_rejected():
    v = {**_BASE_PROV, "source_kind": "github_graphql"}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


_ANCHOR_BASE = {
    "source_kind": "local_fs",
    "ref": None,
    "path": "state/handoffs/test.md",
    "observed_at": "2026-06-22T03:06:15Z",
    "derivation": "parsed",
}


def test_provenance_anchorless_1_repo_empty_anchor_null_rejected():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": None}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_2_repo_empty_anchor_empty_value_rejected():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": {"kind": "x", "value": ""}}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_3_repo_empty_anchor_empty_kind_rejected():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": {"kind": "", "value": "x"}}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_4_repo_empty_entity_anchored_passes():
    v = {**_ANCHOR_BASE, "repo": "", "entity_anchor": {"kind": "x", "value": "y"}}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_5_repo_anchored_anchor_null_passes():
    v = {**_ANCHOR_BASE, "repo": "owner/repo", "entity_anchor": None}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_6_composite_anchoring_passes():
    v = {**_ANCHOR_BASE, "repo": "owner/repo", "entity_anchor": {"kind": "x", "value": "y"}}
    assert zod_safe_parse_ok(ProvenanceEnvelope, v)


def test_provenance_anchorless_7_malformed_composite_rejected():
    v = {**_ANCHOR_BASE, "repo": "owner/repo", "entity_anchor": {"kind": "", "value": ""}}
    assert not zod_safe_parse_ok(ProvenanceEnvelope, v)
