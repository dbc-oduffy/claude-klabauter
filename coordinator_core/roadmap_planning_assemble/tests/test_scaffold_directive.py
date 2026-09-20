"""
coordinator_core.roadmap_planning_assemble.tests.test_scaffold_directive --
unit tests for the shared `coordinator-doc-new` directive constructor (C2).

Purpose: proves `scaffold_directive.py`'s per-type flag computation in
isolation from any host, per AC1/AC3/AC4 -- per-type required-flag
computation, omit-when-None for an optional flag, `MutexFlagPair` pair-
gating (exactly one of two flags, never zero or two), the `already_satisfied`
existence predicate over the resolved `--out`, and `--out` containment
rejection for both an escaping relative path and an absolute foreign path.
This is the constructor's own test surface -- no host module is imported or
exercised here; C3-C6's parity pins are what prove each host's call site,
not this module.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C2 (§ Test surface, § Acceptance criteria AC1/AC3/AC4).

Negative spec: does NOT test doctype_hosts.py's exclusion-row reasons or the
`EXCLUSION_REASONS` closed set -- that table carries no concept the
constructor reads or produces, and its coverage is C8's falsifier-gated pin,
not this chunk's (C2's `writes` is this one file). Does NOT import or call
any host's `brief()` -- that is C3-C6's parity-pin surface. Does NOT invoke
`coordinator-doc-new` or any other CLI -- the constructor is a compute-half
only and this suite exercises it exactly that way.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap_planning_assemble.scaffold_directive import (
    Flag,
    MutexFlagPair,
    ScaffoldDirectiveError,
    build_args,
    build_scaffold_directive,
)


# --- build_args: per-type required-flag computation -----------------------


def test_required_flag_is_emitted_when_resolved():
    args = build_args(
        "roadmap-baton",
        {"predecessor_id": "roadmap-1"},
        [Flag(name="--predecessor-id", key="predecessor_id", required=True)],
    )
    assert args == ["--type=roadmap-baton", "--predecessor-id=roadmap-1"]


def test_required_flag_missing_raises():
    with pytest.raises(ScaffoldDirectiveError, match="predecessor_id"):
        build_args(
            "roadmap-baton",
            {},
            [Flag(name="--predecessor-id", key="predecessor_id", required=True)],
        )


def test_list_valued_field_repeats_flag_once_per_item():
    args = build_args(
        "plan",
        {"blocks": ["b1", "b2", "b3"]},
        [Flag(name="--blocks", key="blocks", required=True)],
    )
    assert args == [
        "--type=plan",
        "--blocks=b1",
        "--blocks=b2",
        "--blocks=b3",
    ]


# --- build_args: omit-when-None for an optional flag -----------------------


def test_optional_flag_omitted_when_none():
    args = build_args(
        "sizing-object",
        {"sizing_object": None},
        [Flag(name="--sizing-object", key="sizing_object", required=False)],
    )
    assert args == ["--type=sizing-object"]


def test_optional_flag_omitted_when_key_absent():
    args = build_args(
        "sizing-object",
        {},
        [Flag(name="--sizing-object", key="sizing_object", required=False)],
    )
    assert args == ["--type=sizing-object"]


def test_optional_flag_included_when_resolved():
    args = build_args(
        "sizing-object",
        {"sizing_object": "yes"},
        [Flag(name="--sizing-object", key="sizing_object", required=False)],
    )
    assert args == ["--type=sizing-object", "--sizing-object=yes"]


# --- build_args: MutexFlagPair pair-gating ---------------------------------


def test_mutex_pair_exactly_one_resolved_a():
    args = build_args(
        "handoff",
        {"predecessor": "p1"},
        [
            MutexFlagPair(
                a=Flag(name="--predecessor", key="predecessor"),
                b=Flag(name="--predecessor-id", key="predecessor_id"),
            )
        ],
    )
    assert args == ["--type=handoff", "--predecessor=p1"]


def test_mutex_pair_exactly_one_resolved_b():
    args = build_args(
        "handoff",
        {"predecessor_id": "p1-id"},
        [
            MutexFlagPair(
                a=Flag(name="--predecessor", key="predecessor"),
                b=Flag(name="--predecessor-id", key="predecessor_id"),
            )
        ],
    )
    assert args == ["--type=handoff", "--predecessor-id=p1-id"]


def test_mutex_pair_neither_resolved_raises():
    with pytest.raises(ScaffoldDirectiveError, match="--predecessor"):
        build_args(
            "handoff",
            {},
            [
                MutexFlagPair(
                    a=Flag(name="--predecessor", key="predecessor"),
                    b=Flag(name="--predecessor-id", key="predecessor_id"),
                )
            ],
        )


def test_mutex_pair_both_resolved_raises():
    with pytest.raises(ScaffoldDirectiveError, match="--predecessor"):
        build_args(
            "handoff",
            {"predecessor": "p1", "predecessor_id": "p1-id"},
            [
                MutexFlagPair(
                    a=Flag(name="--predecessor", key="predecessor"),
                    b=Flag(name="--predecessor-id", key="predecessor_id"),
                )
            ],
        )


def test_mutex_pair_not_required_allows_neither():
    args = build_args(
        "handoff",
        {},
        [
            MutexFlagPair(
                a=Flag(name="--predecessor", key="predecessor"),
                b=Flag(name="--predecessor-id", key="predecessor_id"),
                required=False,
            )
        ],
    )
    assert args == ["--type=handoff"]


# --- build_scaffold_directive: already_satisfied predicate -----------------


def test_already_satisfied_false_when_out_missing(tmp_path: Path):
    directive = build_scaffold_directive(
        id_="d1",
        doc_type="roadmap-seed",
        resolved={"out": "docs/plans/does-not-exist.md"},
        flag_spec=[],
        root=tmp_path,
    )
    assert directive["already_satisfied"] is False
    assert "already_satisfied_reason" not in directive


def test_already_satisfied_true_when_out_exists(tmp_path: Path):
    existing = tmp_path / "docs" / "plans" / "already-here.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("authored content", encoding="utf-8")

    directive = build_scaffold_directive(
        id_="d1",
        doc_type="roadmap-seed",
        resolved={"out": "docs/plans/already-here.md"},
        flag_spec=[],
        root=tmp_path,
    )
    assert directive["already_satisfied"] is True
    assert "already exists on disk" in directive["already_satisfied_reason"]


def test_directive_shape_has_required_keys(tmp_path: Path):
    directive = build_scaffold_directive(
        id_="d1",
        doc_type="roadmap-seed",
        resolved={"out": "docs/plans/new-doc.md"},
        flag_spec=[],
        root=tmp_path,
        depends_on=["upstream-id"],
    )
    assert directive["id"] == "d1"
    assert directive["cli"] == "coordinator-doc-new"
    assert directive["depends_on"] == ["upstream-id"]
    assert "--type=roadmap-seed" in directive["args"]
    assert any(a.startswith("--out=") for a in directive["args"])


def test_missing_out_key_raises():
    with pytest.raises(ScaffoldDirectiveError, match="out"):
        build_scaffold_directive(
            id_="d1",
            doc_type="roadmap-seed",
            resolved={},
            flag_spec=[],
            root=Path("/tmp"),
        )


# --- build_scaffold_directive: --out containment rejection -----------------


def test_out_containment_rejects_escaping_relative_path(tmp_path: Path):
    with pytest.raises(ScaffoldDirectiveError, match="outside repo root"):
        build_scaffold_directive(
            id_="d1",
            doc_type="roadmap-seed",
            resolved={"out": "../../etc/passwd"},
            flag_spec=[],
            root=tmp_path,
        )


def test_out_containment_rejects_absolute_foreign_path(tmp_path: Path):
    foreign_root = tmp_path.parent / "not-this-repo"
    with pytest.raises(ScaffoldDirectiveError, match="outside repo root"):
        build_scaffold_directive(
            id_="d1",
            doc_type="roadmap-seed",
            resolved={"out": str(foreign_root / "doc.md")},
            flag_spec=[],
            root=tmp_path,
        )


def test_out_containment_accepts_path_inside_root(tmp_path: Path):
    directive = build_scaffold_directive(
        id_="d1",
        doc_type="roadmap-seed",
        resolved={"out": str(tmp_path / "docs" / "plans" / "new-doc.md")},
        flag_spec=[],
        root=tmp_path,
    )
    assert any(a == "--out=docs/plans/new-doc.md" for a in directive["args"])
