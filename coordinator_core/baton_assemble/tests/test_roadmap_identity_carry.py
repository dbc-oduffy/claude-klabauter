
from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _init_repo, _write_artifact

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


def _roadmap_baton_predecessor(
    tmp_path, *, roadmap_id="rm-c10", stub_id="stub-c10", blocks=None
):
    return _write_artifact(
        tmp_path / "state" / "handoffs" / "2026-08-18-roadmap-baton-predecessor.md",
        [
            "deliverable_id: DEL-C10-IDENTITY",
            "handoff_id: hnd-c10-identity-1a2b90",
            "kind: roadmap-baton",
            f"roadmap_id: {roadmap_id}",
            f"stub_id: {stub_id}",
            'predecessor: "none"',
        ]
        + ([f"blocks: [{', '.join(blocks)}]"] if blocks else []),
    )


class TestRoadmapBatonSuccessorMintsAsRoadmapBaton:

    def test_ordinary_predecessor_mints_as_handoff_unchanged(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-ordinary-predecessor.md",
            ["deliverable_id: DEL-ORDINARY", "handoff_id: hnd-ordinary-1a2b91"],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=handoff" in d1["args"]
        assert not any(a.startswith("--roadmap-id=") for a in d1["args"])
        assert not any(a.startswith("--stub-id=") for a in d1["args"])

    def test_roadmap_baton_predecessor_mints_successor_as_roadmap_baton(self, tmp_path):
        artifact = _roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=roadmap-baton" in d1["args"], (
            "C10 Part 2: a roadmap-baton predecessor's successor must mint "
            f"as a roadmap-baton -- got args {d1['args']!r}"
        )
        assert "--type=handoff" not in d1["args"]

    def test_spinoff_kind_is_never_flipped(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-spinoff-origin.md",
            ["deliverable_id: DEL-SPINOFF-ORIGIN", "handoff_id: hnd-spinoff-origin-1a2b92"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=spinoff" in d1["args"]


class TestRoadmapIdentityFieldsCarried:

    def test_roadmap_id_and_stub_id_are_forwarded(self, tmp_path):
        artifact = _roadmap_baton_predecessor(
            tmp_path, roadmap_id="rm-identity-test", stub_id="stub-identity-test"
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--roadmap-id=rm-identity-test" in d1["args"]
        assert "--stub-id=stub-identity-test" in d1["args"]

    def test_missing_roadmap_id_or_stub_id_on_predecessor_omits_the_flag(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-partial-identity.md",
            [
                "deliverable_id: DEL-PARTIAL-IDENTITY",
                "handoff_id: hnd-partial-identity-1a2b93",
                "kind: roadmap-baton",
                "roadmap_id: rm-partial-only",
                'predecessor: "none"',
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--roadmap-id=rm-partial-only" in d1["args"]
        assert not any(a.startswith("--stub-id=") for a in d1["args"])


class TestBlocksCarriedWithTheStubId:

    def test_blocks_are_forwarded_one_flag_per_entry(self, tmp_path):
        artifact = _roadmap_baton_predecessor(
            tmp_path, blocks=["the-meter-02", "archival-sweeps-03"]
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--blocks=the-meter-02" in d1["args"]
        assert "--blocks=archival-sweeps-03" in d1["args"]

    def test_blocks_travel_with_the_stub_id_never_without_it(self, tmp_path):
        artifact = _roadmap_baton_predecessor(tmp_path, blocks=["dep-01"])
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert any(a.startswith("--stub-id=") for a in d1["args"])
        assert any(a.startswith("--blocks=") for a in d1["args"])

    def test_predecessor_with_no_blocks_omits_the_flag(self, tmp_path):
        artifact = _roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert not any(a.startswith("--blocks=") for a in d1["args"])

    def test_ordinary_handoff_predecessor_carries_no_blocks(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-18-ordinary-with-blocks.md",
            [
                "deliverable_id: DEL-ORDINARY-BLOCKS",
                "handoff_id: hnd-ordinary-blocks-1a2b94",
                "blocks: [dep-01]",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        assert "--type=handoff" in d1["args"]
        assert not any(a.startswith("--blocks=") for a in d1["args"])


class TestSingleLiveStubIdAfterSuccession:

    def test_d6_supersede_and_roadmap_baton_mint_are_the_same_decision(self, tmp_path):
        artifact = _roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object

        d1 = next(d for d in decision["directives"] if d["cli"] == "coordinator-doc-new")
        d6 = next(
            (d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"),
            None,
        )

        assert "--type=roadmap-baton" in d1["args"], (
            "the successor must mint as a roadmap-baton, carrying a fresh "
            "live stub_id"
        )
        assert d6 is not None, (
            "d6 must arm in the SAME decision object -- an unarchived "
            "predecessor plus a freshly-minted roadmap-baton successor "
            "would leave two live records claiming the same stub_id"
        )
        assert d6["args"][0] == str(artifact)
