"""Pair primitives and result dataclasses of origin_stub_staleness."""

import dataclasses

import pytest

from pathlib import Path

from coordinator_core.ops.origin_stub_staleness import (
    OriginStubSurvey,
    StaleOriginStub,
    is_baton_kind,
    read_closes_stubs,
    read_pair,
)
from coordinator_core.session import record_homes


def _handoff(name):
    return Path(record_homes.record_path("", "handoffs", name)).as_posix()


def test_read_pair_well_formed_strips():
    assert read_pair({"roadmap_id": " r1 ", "stub_id": "s1 "}) == ("r1", "s1")


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"roadmap_id": "r1"},
        {"stub_id": "s1"},
        {"roadmap_id": "  ", "stub_id": "s1"},
        {"roadmap_id": "r1", "stub_id": ""},
        {"roadmap_id": 1, "stub_id": "s1"},
        {"roadmap_id": "r1", "stub_id": None},
    ],
)
def test_read_pair_partial_or_non_string_is_none(meta):
    assert read_pair(meta) is None


def test_read_closes_stubs_keeps_only_well_formed():
    meta = {
        "closes_stubs": [
            {"roadmap_id": "r1", "stub_id": "s1"},
            {"roadmap_id": "r2"},
            "not-a-dict",
            {"roadmap_id": 3, "stub_id": "s3"},
            {"roadmap_id": "r4", "stub_id": "s4"},
        ]
    }
    assert read_closes_stubs(meta) == [("r1", "s1"), ("r4", "s4")]


@pytest.mark.parametrize("raw", [None, "x", {"a": 1}, 5])
def test_read_closes_stubs_non_list_is_empty(raw):
    assert read_closes_stubs({"closes_stubs": raw}) == []
    assert read_closes_stubs({}) == []


def test_is_baton_kind():
    assert is_baton_kind("spinoff")
    assert is_baton_kind("spinoff-roadmap")
    assert not is_baton_kind("handoff")
    assert not is_baton_kind(None)


def test_result_dataclasses_are_frozen():
    stub = StaleOriginStub(
        path=_handoff("a.md"),
        pair=("r", "s"),
        deployment_state="ready_to_fire",
        evidence_path="docs/plans/p.md",
        evidence_kind="plan",
    )
    result = OriginStubSurvey(stale=(stub,), live_with_pair=1, unreadable=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        stub.path = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.live_with_pair = 2


def _w(root, rel, text, binary=False):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")


def _stub(root, name, rid, sid, state="ready_to_fire"):
    _w(
        root,
        _handoff(f"{name}.md"),
        f"---\nkind: spinoff\nroadmap_id: {rid}\nstub_id: {sid}\n"
        f"deployment_state: {state}\n---\nbody\n",
    )


def test_survey_reports_stale_stubs_and_skips_unshipped(tmp_path):
    from coordinator_core.ops.origin_stub_staleness import survey

    _stub(tmp_path, "s1", "r", "s")
    _stub(tmp_path, "s2", "r", "t", "awaiting_gate")
    _stub(tmp_path, "s3", "r", "u")
    _stub(tmp_path, "s4", "r", "v")
    _stub(tmp_path, "s5", "r", "w")
    _stub(tmp_path, "s6", "r", "x")
    _w(tmp_path, "docs/plans/p.md", "---\nstatus: implemented\nroadmap_id: r\nstub_id: s\n---\n")
    _w(
        tmp_path,
        "archive/handoffs/2026-01/h.md",
        "---\nkind: handoff\ndeployment_state: shipped\ncloses_stubs:\n"
        "  - roadmap_id: r\n    stub_id: t\n---\n",
    )
    _w(tmp_path, "docs/plans/ab.md", "---\nstatus: abandoned\nroadmap_id: r\nstub_id: v\n---\n")
    _w(tmp_path, "archive/specs/2026/sp.md", "---\nstatus: superseded\nroadmap_id: r\nstub_id: w\n---\n")
    _w(tmp_path, "archive/handoffs/ab.md", "---\ndeployment_state: abandoned\nroadmap_id: r\nstub_id: x\n---\n")

    res = survey(tmp_path)
    got = {s.path: s for s in res.stale}
    s1, s2 = _handoff("s1.md"), _handoff("s2.md")
    assert set(got) == {s1, s2}
    assert got[s1].evidence_path == "docs/plans/p.md"
    assert got[s1].evidence_kind == "plan"
    assert got[s2].evidence_path == "archive/handoffs/2026-01/h.md"
    assert got[s2].evidence_kind == "handoff"
    assert res.live_with_pair == 6
    assert res.unreadable == ()


def test_survey_archive_spec_evidence_and_sorted_first(tmp_path):
    from coordinator_core.ops.origin_stub_staleness import survey

    _stub(tmp_path, "s1", "r", "s")
    _w(tmp_path, "archive/specs/z.md", "---\nstatus: shipped\nroadmap_id: r\nstub_id: s\n---\n")
    _w(tmp_path, "docs/plans/a.md", "---\nstatus: shipped\nroadmap_id: r\nstub_id: s\n---\n")
    res = survey(tmp_path)
    assert res.stale[0].evidence_path == "archive/specs/z.md" or res.stale[0].evidence_path == "docs/plans/a.md"
    assert res.stale[0].evidence_path == min("archive/specs/z.md", "docs/plans/a.md")


def test_survey_unreadable_file_is_listed_and_survey_continues(tmp_path):
    from coordinator_core.ops.origin_stub_staleness import survey

    _stub(tmp_path, "s1", "r", "s")
    _w(tmp_path, "docs/plans/p.md", "---\nstatus: shipped\nroadmap_id: r\nstub_id: s\n---\n")
    _w(tmp_path, "docs/plans/bad.md", b"---\nstub_id: \xff\xfe\n---\n", binary=True)
    res = survey(tmp_path)
    assert res.unreadable == ("docs/plans/bad.md",)
    assert [s.path for s in res.stale] == [_handoff("s1.md")]


def test_survey_empty_repo(tmp_path):
    from coordinator_core.ops.origin_stub_staleness import survey

    res = survey(tmp_path)
    assert (res.stale, res.live_with_pair, res.unreadable) == ((), 0, ())
