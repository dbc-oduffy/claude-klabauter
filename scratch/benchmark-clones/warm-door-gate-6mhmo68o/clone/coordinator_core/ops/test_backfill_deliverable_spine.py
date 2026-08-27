"""Characterization + parity tests for coordinator_core.ops.backfill_deliverable_spine.

Port of: backfill-deliverable-spine.sh (DoE ca30f76c, 2026-07-17)
Expectations below are independently re-derived from the bash oracle's awk/case logic
(read directly, not by running the ported module and asserting its own output back at
itself) — this is the review-integration-doctrine "fresh re-check" posture applied to a
port: the awk `extract_fm_field` semantics, the `case` glob patterns in
`is_sidecar_plan`/`classify_artifact`, and the grouping/ambiguity/write-pass control flow
are each re-derived from the oracle's source text.

A golden dry-run fixture (docstring below) was also captured by running the ACTUAL bash
oracle (`bash coordinator/bin/backfill-deliverable-spine.sh --dry-run --root <fixture>`)
against a synthetic corpus before the oracle was retired, and is reproduced in
`test_dry_run_matches_captured_oracle_output` as a literal golden string (lesson: don't
shell out to the now-deleted/renamed oracle from a test — capture its verified output as
a fixture instead).
"""
from __future__ import annotations

import io
import os
import re
import stat
from pathlib import Path
from typing import List

import pytest

import coordinator_core.ops.backfill_deliverable_spine as mod
from coordinator_core.ops.backfill_deliverable_spine import (
    build_plan_sizing_index,
    classify_artifact,
    detect_ambiguous_groups,
    enumerate_corpus,
    extract_fm_field,
    get_workstream_key,
    group_corpus,
    is_immutable_path,
    is_sidecar_plan,
    main,
)

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# Schema-valid whole-document sizing-object YAML body (1.8.0) — for tests that
# exercise the WRITE path (`_stamp_yaml_document` schema-validates before
# landing a mutation); tests that only read `sizing_object`/`workstream` don't
# need every required field and use a minimal body instead.
_SCHEMA_VALID_SIZING_BODY = (
    "schema: sizing-object\n"
    "intent: Test intent, verbatim.\n"
    "estimate:\n"
    "  tshirt: M\n"
    "  provisional: true\n"
    "route: plan\n"
    "detents: []\n"
    "fork: null\n"
    "xl_exit: null\n"
    "status: routed\n"
    "premise:\n"
    "  provenance: read\n"
    "  evidence: test fixture, no real premise verified\n"
)


# ---------------------------------------------------------------------------
# extract_fm_field — re-derived from the oracle's awk one-liner
# ---------------------------------------------------------------------------


def test_extract_fm_field_reads_only_first_frontmatter_block(tmp_path: Path):
    p = tmp_path / "a.md"
    _write(
        p,
        "---\nworkstream: alpha\n---\n"
        "Body text with a second fence block below\n"
        "---\nworkstream: SHOULD-NOT-BE-SEEN\n---\n",
    )
    assert extract_fm_field(str(p), "workstream") == "alpha"


def test_extract_fm_field_takes_whole_remainder_of_line_not_first_token(tmp_path: Path):
    # awk `sub("^field:[[:space:]]*", "", val)` operates on the WHOLE line, not a
    # whitespace split — a multi-word value must be preserved in full.
    p = tmp_path / "a.md"
    _write(p, "---\nworkstream: some multi word value\n---\nbody\n")
    assert extract_fm_field(str(p), "workstream") == "some multi word value"


def test_extract_fm_field_strips_one_layer_of_matching_quotes(tmp_path: Path):
    p = tmp_path / "a.md"
    _write(p, '---\nworkstream: "quoted value"\n---\nbody\n')
    assert extract_fm_field(str(p), "workstream") == "quoted value"

    p2 = tmp_path / "b.md"
    _write(p2, "---\nworkstream: 'quoted value'\n---\nbody\n")
    assert extract_fm_field(str(p2), "workstream") == "quoted value"


def test_extract_fm_field_treats_yaml_null_and_tilde_as_empty(tmp_path: Path):
    p = tmp_path / "a.md"
    _write(p, "---\nworkstream: null\n---\nbody\n")
    assert extract_fm_field(str(p), "workstream") == ""

    p2 = tmp_path / "b.md"
    _write(p2, "---\nworkstream: ~\n---\nbody\n")
    assert extract_fm_field(str(p2), "workstream") == ""


def test_extract_fm_field_absent_field_returns_empty(tmp_path: Path):
    p = tmp_path / "a.md"
    _write(p, "---\nother: x\n---\nbody\n")
    assert extract_fm_field(str(p), "workstream") == ""


def test_extract_fm_field_no_frontmatter_returns_empty(tmp_path: Path):
    p = tmp_path / "a.md"
    _write(p, "just a body, no frontmatter fences\n")
    assert extract_fm_field(str(p), "workstream") == ""


def test_extract_fm_field_missing_file_returns_empty(tmp_path: Path):
    assert extract_fm_field(str(tmp_path / "does-not-exist.md"), "workstream") == ""


# ---------------------------------------------------------------------------
# is_immutable_path — re-derived from the oracle's case patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/root/archive/handoffs/foo.md", True),
        ("/root/archive/completed/foo.md", True),
        ("/root/state/handoffs/foo.md", False),
        ("/root/docs/plans/foo.md", False),
        ("/root/archive/handoffs/nested/deep/foo.md", True),
    ],
)
def test_is_immutable_path(path, expected):
    assert is_immutable_path(path) is expected


# ---------------------------------------------------------------------------
# is_sidecar_plan — re-derived from the oracle's enumerated case patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "basename,expected",
    [
        ("2026-01-01-plan.md", False),
        ("2026-01-01-plan.md.old-sidecar.md", True),  # old double-md convention
        ("2026-01-01-plan.prior-art-check.md", True),
        ("2026-01-01-plan.coverage-check.md", True),
        ("2026-01-01-plan.plan-coverage-check.md", True),
        ("2026-01-01-plan.plan-coverage-check.extra.md", True),
        ("2026-01-01-plan.code-review.md", True),
        ("2026-01-01-plan.code-review-slice-00.md", True),
        ("2026-01-01-plan.codereview-item3.md", True),
        ("2026-01-01-plan.patrik-review.md", True),
        ("2026-01-01-plan.zoli-review.md", True),
        ("2026-01-01-plan.docs-check.md", True),
        ("2026-01-01-plan.doc-link-check.md", True),
        ("2026-01-01-plan.review.md", True),
        ("2026-01-01-plan.reviewer-notes.md", False),  # not an exact ".review.md" suffix
    ],
)
def test_is_sidecar_plan(basename, expected):
    assert is_sidecar_plan(f"/root/docs/plans/{basename}") is expected


# ---------------------------------------------------------------------------
# classify_artifact / get_workstream_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/root/docs/plans/x.md", "plan"),
        ("/root/state/handoffs/x.md", "handoff"),
        ("/root/archive/handoffs/x.md", "handoff-archived"),
        ("/root/archive/completed/x.md", "completion"),
        ("/root/state/roadmap/sub/OVERVIEW.md", "roadmap"),
        ("/root/docs/ROADMAP.md", "roadmap"),
        ("/root/state/sizings/x.yaml", "sizing"),
        ("/root/archive/sizings/2026-08/x.yaml", "sizing"),
        ("/root/state/lessons/x.md", "unknown"),
    ],
)
def test_classify_artifact(path, expected):
    assert classify_artifact(path) == expected


def test_get_workstream_key_uses_chain_for_completions(tmp_path: Path):
    p = tmp_path / "c.md"
    _write(p, "---\nchain: my-chain\nworkstream: should-not-be-used\n---\nbody\n")
    assert get_workstream_key(str(p), "completion") == "my-chain"


def test_get_workstream_key_uses_workstream_for_non_completions(tmp_path: Path):
    p = tmp_path / "h.md"
    _write(p, "---\nworkstream: my-ws\nchain: should-not-be-used\n---\nbody\n")
    assert get_workstream_key(str(p), "handoff") == "my-ws"


# ---------------------------------------------------------------------------
# build_plan_sizing_index / get_workstream_key's sizing arm (AC1-AC3)
# ---------------------------------------------------------------------------


def test_build_plan_sizing_index_maps_sizing_object_to_deliverable_id(tmp_path: Path):
    _write(
        tmp_path / "docs/plans/2026-01-01-citer.md",
        "---\nslug: citer\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    index, ambiguous = build_plan_sizing_index(str(tmp_path))
    assert index == {"state/sizings/2026-01-01-s.yaml": "dlv-citer-000000"}
    assert ambiguous == {}


def test_build_plan_sizing_index_sees_already_threaded_plans(tmp_path: Path):
    """The load-bearing case: `group_corpus`'s own grouping loop would have
    short-circuited this plan into `already_threaded` (it carries
    `deliverable_id`) before ever computing a grouping key — the index must
    be built independently, by its own pass, so it still sees this edge."""
    _write(
        tmp_path / "docs/plans/2026-01-01-threaded.md",
        "---\nslug: threaded\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-already-threaded-0\n---\nBody\n",
    )
    corpus = enumerate_corpus(str(tmp_path))
    result = group_corpus(corpus)
    plan_path = str(tmp_path / "docs" / "plans" / "2026-01-01-threaded.md")
    assert result.already_threaded == [(plan_path, "dlv-already-threaded-0")]
    assert all(plan_path not in files for files in result.group_files.values())

    index, ambiguous = build_plan_sizing_index(str(tmp_path))
    assert index["state/sizings/2026-01-01-s.yaml"] == "dlv-already-threaded-0"
    assert ambiguous == {}


def test_build_plan_sizing_index_excludes_plan_with_no_deliverable_id(tmp_path: Path):
    _write(
        tmp_path / "docs/plans/2026-01-01-unthreaded.md",
        "---\nslug: unthreaded\nsizing_object: state/sizings/2026-01-01-s.yaml\n---\nBody\n",
    )
    index, ambiguous = build_plan_sizing_index(str(tmp_path))
    assert index == {}
    assert ambiguous == {}


def test_build_plan_sizing_index_excludes_sidecar_plan(tmp_path: Path):
    _write(
        tmp_path / "docs/plans/2026-01-01-x.code-review.md",
        "---\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-sidecar-000000\n---\nBody\n",
    )
    index, ambiguous = build_plan_sizing_index(str(tmp_path))
    assert index == {}
    assert ambiguous == {}


def test_build_plan_sizing_index_collision_is_dropped_and_flagged(tmp_path: Path):
    """Finding 1 (P2): two distinct plans citing the same `sizing_object:`
    with DIFFERENT `deliverable_id`s must never silently pick a winner —
    the colliding key is absent from `index` (so it falls through to
    UNKEYED via get_workstream_key's existing miss path, never a wrong
    id) and is reported in `ambiguous_sizing_refs` instead."""
    _write(
        tmp_path / "docs/plans/2026-01-01-alpha.md",
        "---\nslug: alpha\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-alpha-000000\n---\nBody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-01-02-beta.md",
        "---\nslug: beta\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-beta-000000\n---\nBody\n",
    )
    index, ambiguous = build_plan_sizing_index(str(tmp_path))
    assert "state/sizings/2026-01-01-s.yaml" not in index
    assert ambiguous == {"state/sizings/2026-01-01-s.yaml": ["alpha", "beta"]}


def test_build_plan_sizing_index_two_sizings_same_deliverable_id_not_a_collision(
    tmp_path: Path,
):
    """The other direction (2 distinct sizing_object keys resolving to the
    SAME deliverable_id, e.g. via two plan docs sharing one id) is legitimate
    and must stay working — it is why 56 moved sizings produced 57 groups in
    the live corpus. This is NOT a collision: collision means one KEY
    (sizing_object) with 2+ DIFFERENT deliverable_ids, not one deliverable_id
    reached via 2+ different keys."""
    _write(
        tmp_path / "docs/plans/2026-01-01-citer.md",
        "---\nslug: citer\nsizing_object: state/sizings/2026-01-01-a.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    _write(
        tmp_path / "docs/plans/2026-01-02-citer-again.md",
        "---\nslug: citer-again\nsizing_object: state/sizings/2026-01-02-b.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    index, ambiguous = build_plan_sizing_index(str(tmp_path))
    assert index == {
        "state/sizings/2026-01-01-a.yaml": "dlv-citer-000000",
        "state/sizings/2026-01-02-b.yaml": "dlv-citer-000000",
    }
    assert ambiguous == {}


def test_get_workstream_key_sizing_resolves_via_citing_plan_deliverable_id(tmp_path: Path):
    sizing = tmp_path / "state/sizings/2026-01-01-s.yaml"
    _write(sizing, "schema: sizing-object\n")
    index = {"state/sizings/2026-01-01-s.yaml": "dlv-citer-000000"}
    assert get_workstream_key(str(sizing), "sizing", str(tmp_path), index) == "dlv-citer-000000"


def test_get_workstream_key_sizing_no_citing_plan_is_unkeyed(tmp_path: Path):
    sizing = tmp_path / "state/sizings/2026-01-01-orphan.yaml"
    _write(sizing, "schema: sizing-object\n")
    index: dict = {}
    assert (
        get_workstream_key(str(sizing), "sizing", str(tmp_path), index)
        == mod._UNKEYED_SIZING_GROUP_KEY
    )


def test_get_workstream_key_sizing_without_index_is_unkeyed(tmp_path: Path):
    """Backward-compat: a caller (or a corpus with no plan_sizing_index built
    yet) that omits coordinator_root/plan_sizing_index must still resolve
    UNKEYED, never raise and never invent a key."""
    sizing = tmp_path / "state/sizings/2026-01-01-orphan.yaml"
    _write(sizing, "schema: sizing-object\n")
    assert get_workstream_key(str(sizing), "sizing") == mod._UNKEYED_SIZING_GROUP_KEY


def test_get_workstream_key_sizing_ignores_workstream_document_key(tmp_path: Path):
    """AC2: the `workstream:` whole-document-YAML read is GONE for sizings —
    even a sizing that carries that (never-real) key must not be keyed by
    it; only the plan-FK index resolves a key."""
    sizing = tmp_path / "state/sizings/2026-01-01-s.yaml"
    _write(sizing, "schema: sizing-object\nworkstream: should-not-be-read\n")
    assert get_workstream_key(str(sizing), "sizing") == mod._UNKEYED_SIZING_GROUP_KEY


def test_group_corpus_sizing_keyed_by_citing_plan_deliverable_id(tmp_path: Path):
    _write(
        tmp_path / "docs/plans/2026-01-01-citer.md",
        "---\nslug: citer\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    sizing = tmp_path / "state/sizings/2026-01-01-s.yaml"
    _write(sizing, "schema: sizing-object\nintent: test\n")

    corpus = enumerate_corpus(str(tmp_path))
    index, _ambiguous = build_plan_sizing_index(str(tmp_path))
    result = group_corpus(corpus, str(tmp_path), index)

    assert str(sizing) in result.group_files["dlv-citer-000000"]
    assert mod._UNKEYED_SIZING_GROUP_KEY not in result.group_files


def test_group_corpus_sizing_with_no_citing_plan_stays_unkeyed(tmp_path: Path):
    sizing = tmp_path / "state/sizings/2026-01-01-orphan.yaml"
    _write(sizing, "schema: sizing-object\nintent: test\n")

    corpus = enumerate_corpus(str(tmp_path))
    index, _ambiguous = build_plan_sizing_index(str(tmp_path))
    result = group_corpus(corpus, str(tmp_path), index)

    assert str(sizing) in result.group_files[mod._UNKEYED_SIZING_GROUP_KEY]


# ---------------------------------------------------------------------------
# Fixture builder — mirrors the fixture used to capture the golden oracle run.
# ---------------------------------------------------------------------------


def _build_corpus(root: Path) -> None:
    _write(
        root / "state/handoffs/h1.md",
        "---\nkind: handoff\nworkstream: alpha-thing\nstatus: open\n---\nBody h1\n",
    )
    _write(
        root / "state/handoffs/h2-spinoff.md",
        "---\nkind: spinoff-roadmap\nworkstream: beta-thing\nstub_id: abc123\nstatus: active\n---\nBody h2\n",
    )
    _write(
        root / "state/handoffs/h3-already.md",
        "---\nkind: handoff\nworkstream: gamma-thing\ndeliverable_id: dlv-existing-999999\nstatus: open\n---\nBody h3\n",
    )
    _write(
        root / "archive/handoffs/h4-archived.md",
        "---\nkind: handoff\nworkstream: alpha-thing\nstatus: claimed\n---\nBody h4\n",
    )
    _write(
        root / "docs/plans/2026-01-01-plan-one.md",
        "---\nslug: plan-one\nworkstream: alpha-thing\n---\nPlan one body\n",
    )
    _write(
        root / "docs/plans/2026-01-02-plan-two.md",
        "---\nslug: plan-two\nworkstream: gamma-thing\n---\nPlan two body\n",
    )
    _write(
        root / "docs/plans/2026-01-03-plan-two.code-review.md",
        "---\nworkstream: gamma-thing\n---\nsidecar - should be excluded\n",
    )
    _write(
        root / "docs/plans/2026-01-04-plan-ambig-a.md",
        "---\nslug: plan-ambig-a\nworkstream: delta-thing\n---\nAmbiguous A\n",
    )
    _write(
        root / "docs/plans/2026-01-05-plan-ambig-b.md",
        "---\nslug: plan-ambig-b\nworkstream: delta-thing\n---\nAmbiguous B\n",
    )
    _write(
        root / "docs/plans/2026-01-06-plan-noworkstream.md",
        "---\nslug: plan-noworkstream\n---\nNo workstream field\n",
    )
    _write(root / "archive/completed/c1.md", "---\nchain: alpha-thing\n---\nCompletion one\n")
    _write(
        root / "state/roadmap/rm1/OVERVIEW.md",
        "---\nworkstream: beta-thing\n---\nRoadmap overview\n",
    )
    _write(
        root / "docs/ROADMAP.md",
        "---\nworkstream: \n---\nTop-level roadmap doc, null workstream\n",
    )


def test_enumerate_corpus_excludes_sidecar_plan(tmp_path: Path):
    _build_corpus(tmp_path)
    corpus = enumerate_corpus(str(tmp_path))
    assert not any("plan-two.code-review.md" in f for f in corpus)
    assert any("plan-two.md" in f and "code-review" not in f for f in corpus)
    # 12 non-sidecar artifacts, matching the captured golden fixture's "Total artifacts
    # scanned: 12" (13 files on disk minus the 1 excluded sidecar).
    assert len(corpus) == 12


def test_group_corpus_already_threaded_excluded_from_groups(tmp_path: Path):
    _build_corpus(tmp_path)
    corpus = enumerate_corpus(str(tmp_path))
    result = group_corpus(corpus)
    assert len(result.already_threaded) == 1
    assert result.already_threaded[0][1] == "dlv-existing-999999"
    all_grouped = [f for files in result.group_files.values() for f in files]
    assert not any("h3-already.md" in f for f in all_grouped)


def test_group_corpus_ambiguity_detected_for_two_plan_slugs(tmp_path: Path):
    _build_corpus(tmp_path)
    corpus = enumerate_corpus(str(tmp_path))
    result = group_corpus(corpus)
    ambiguous = detect_ambiguous_groups(result)
    assert ambiguous == ["delta-thing"]


def test_group_corpus_unknown_group_for_missing_or_null_workstream(tmp_path: Path):
    _build_corpus(tmp_path)
    corpus = enumerate_corpus(str(tmp_path))
    result = group_corpus(corpus)
    unknown_files = result.group_files.get(mod._UNKNOWN_GROUP_KEY, [])
    basenames = {os.path.basename(f) for f in unknown_files}
    assert basenames == {"2026-01-06-plan-noworkstream.md", "ROADMAP.md"}


# ---------------------------------------------------------------------------
# main() — dry-run report parity against a golden oracle capture
# ---------------------------------------------------------------------------


def test_dry_run_matches_captured_oracle_output(tmp_path: Path):
    """Golden fixture: captured by running the retired bash oracle
    (`bash coordinator/bin/backfill-deliverable-spine.sh --dry-run --root <this
    same fixture>`) before it was ported. Compared here field-by-field rather than
    as one giant string diff because the oracle's `find -print0` corpus-enumeration
    order is filesystem/inode-dependent (verified non-deterministic across runs on
    the SAME oracle) — this port deliberately sorts for determinism (see module
    docstring "Departure from the oracle"), so only the SET of grouped artifacts and
    the summary/ambiguity verdicts are asserted, not exact line order.
    """
    _build_corpus(tmp_path)
    out = io.StringIO()
    err = io.StringIO()
    rc = main(["--dry-run", "--root", str(tmp_path)], out=out, err=err)
    report = out.getvalue()

    assert rc == 2  # ambiguous group present -> exit 2 even in dry-run

    assert "Total artifacts scanned:      12" in report
    assert "Already-threaded (skip):      1" in report
    assert "Known workstream groups:      4" in report
    assert "Unknown (no workstream):      2" in report
    assert "Ambiguous groups:             1" in report
    assert "Mutable to stamp (named):     7" in report
    assert "Mutable (unknown, skip):      2" in report
    assert "Immutable (derive-at-emit):   2" in report

    assert "[dlv-existing-999999]  state/handoffs/h3-already.md" in report

    assert "GROUP [OK]: alpha-thing" in report
    assert "[handoff] state/handoffs/h1.md  (mutable — will stamp deliverable_id)" in report
    assert (
        "[handoff-archived] archive/handoffs/h4-archived.md  (immutable — derive-at-emit, no write)"
        in report
    )
    assert "[plan] docs/plans/2026-01-01-plan-one.md  (mutable — will stamp deliverable_id)" in report
    assert (
        "[completion] archive/completed/c1.md  (immutable — derive-at-emit, no write)" in report
    )

    assert "GROUP [OK]: beta-thing" in report
    assert "Proposed id: dlv-abc123  [from stub_id]" in report
    assert "[roadmap] state/roadmap/rm1/OVERVIEW.md  (mutable — will stamp deliverable_id)" in report

    assert "GROUP [AMBIGUOUS]: delta-thing" in report
    assert "AMBIGUITY: 2+ distinct plan slugs in this workstream group." in report

    assert "GROUP [OK]: gamma-thing" in report
    assert "[plan] docs/plans/2026-01-02-plan-two.md  (mutable — will stamp deliverable_id)" in report

    assert "UNKNOWN GROUP (no workstream / chain field)" in report
    assert (
        "[plan] docs/plans/2026-01-06-plan-noworkstream.md  (mutable — no workstream, skip)"
        in report
    )
    assert "[roadmap] docs/ROADMAP.md  (mutable — no workstream, skip)" in report

    assert "STATUS: REVIEW REQUIRED (1 ambiguous group(s))" in report
    assert "  Fix ambiguities before running --write." in report


# ---------------------------------------------------------------------------
# main() — write mode
# ---------------------------------------------------------------------------


def _build_clean_corpus(root: Path) -> None:
    """Same fixture, minus the already-threaded and ambiguous/no-workstream artifacts
    (so --write can run clean, per the oracle's own AMBIGUOUS-blocks-write contract)."""
    _write(
        root / "state/handoffs/h1.md",
        "---\nkind: handoff\nworkstream: alpha-thing\nstatus: open\n---\nBody h1\n",
    )
    _write(
        root / "state/handoffs/h2-spinoff.md",
        "---\nkind: spinoff-roadmap\nworkstream: beta-thing\nstub_id: abc123\nstatus: active\n---\nBody h2\n",
    )
    _write(
        root / "archive/handoffs/h4-archived.md",
        "---\nkind: handoff\nworkstream: alpha-thing\nstatus: claimed\n---\nBody h4\n",
    )
    _write(
        root / "docs/plans/2026-01-01-plan-one.md",
        "---\nslug: plan-one\nworkstream: alpha-thing\n---\nPlan one body\n",
    )
    _write(
        root / "docs/plans/2026-01-02-plan-two.md",
        "---\nslug: plan-two\nworkstream: gamma-thing\n---\nPlan two body\n",
    )
    _write(root / "archive/completed/c1.md", "---\nchain: alpha-thing\n---\nCompletion one\n")
    _write(
        root / "state/roadmap/rm1/OVERVIEW.md",
        "---\nworkstream: beta-thing\n---\nRoadmap overview\n",
    )


def test_write_blocked_when_ambiguous(tmp_path: Path):
    _build_corpus(tmp_path)
    out = io.StringIO()
    err = io.StringIO()
    rc = main(["--write", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 2
    assert "Cannot --write while ambiguous groups exist." in err.getvalue()
    # No mutable file in the ambiguous fixture should have been stamped.
    for p in (tmp_path / "docs/plans/2026-01-04-plan-ambig-a.md").parent.glob("*.md"):
        assert "deliverable_id:" not in p.read_text(encoding="utf-8")


def test_write_stamps_sizing_with_citing_plans_deliverable_id_unchanged(tmp_path: Path):
    """AC6, end to end: the write pass must stamp the CITING PLAN's own
    deliverable_id onto the sizing verbatim — not a brand-new id minted from
    that id treated as a workstream slug (the bug `_find_group_id`'s
    generic mint-or-carry search would produce if it were reached here).

    A sizing writes through `_stamp_yaml_document` -> `locked_write.locked_rmw`,
    which needs a real git repo to resolve a lock directory — unlike the
    fence-anchored `_stamp_file` path the other write-mode tests below
    exercise, so this fixture needs its own `git init`.
    """
    import subprocess

    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write(
        tmp_path / "docs/plans/2026-01-01-citer.md",
        "---\nslug: citer\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    sizing = tmp_path / "state/sizings/2026-01-01-s.yaml"
    _write(sizing, _SCHEMA_VALID_SIZING_BODY)

    out = io.StringIO()
    err = io.StringIO()
    rc = main(["--write", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 0, f"stderr: {err.getvalue()}"

    stamped = sizing.read_text(encoding="utf-8")
    assert "deliverable_id: dlv-citer-000000" in stamped
    assert "[stamp]" in out.getvalue()
    assert "state/sizings/2026-01-01-s.yaml  -> dlv-citer-000000" in out.getvalue()


def test_write_stamps_mutable_artifacts_and_skips_immutable(tmp_path: Path):
    _build_clean_corpus(tmp_path)
    out = io.StringIO()
    err = io.StringIO()
    rc = main(["--write", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 0

    h1 = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")
    assert "deliverable_id: dlv-alpha-thing-" in h1

    plan_one = (tmp_path / "docs/plans/2026-01-01-plan-one.md").read_text(encoding="utf-8")
    assert "deliverable_id: dlv-alpha-thing-" in plan_one
    # Same group -> same minted id shared across all mutable members.
    h1_id = [l for l in h1.splitlines() if l.startswith("deliverable_id:")][0]
    plan_one_id = [l for l in plan_one.splitlines() if l.startswith("deliverable_id:")][0]
    assert h1_id == plan_one_id

    # spinoff-roadmap stub_id path: minted id is exactly "dlv-<stub_id>", not a random hex.
    h2 = (tmp_path / "state/handoffs/h2-spinoff.md").read_text(encoding="utf-8")
    assert "deliverable_id: dlv-abc123\n" in h2
    overview = (tmp_path / "state/roadmap/rm1/OVERVIEW.md").read_text(encoding="utf-8")
    assert "deliverable_id: dlv-abc123\n" in overview

    # Immutable artifacts are reported but never written.
    h4 = (tmp_path / "archive/handoffs/h4-archived.md").read_text(encoding="utf-8")
    assert "deliverable_id:" not in h4
    c1 = (tmp_path / "archive/completed/c1.md").read_text(encoding="utf-8")
    assert "deliverable_id:" not in c1

    assert "Stamped:            5" in out.getvalue()
    assert "Skipped (immutable): 2" in out.getvalue()


def test_write_injects_field_immediately_after_opening_fence(tmp_path: Path):
    _build_clean_corpus(tmp_path)
    main(["--write", "--root", str(tmp_path)])
    lines = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    assert lines[1].startswith("deliverable_id: ")


def test_write_is_idempotent_second_run_skips_already_stamped(tmp_path: Path):
    _build_clean_corpus(tmp_path)
    main(["--write", "--root", str(tmp_path)])
    before = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")

    out2 = io.StringIO()
    rc2 = main(["--write", "--root", str(tmp_path)], out=out2, err=io.StringIO())
    after = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")

    assert rc2 == 0
    assert before == after
    # Second run's corpus already excludes stamped artifacts from grouping entirely
    # (already-threaded exclusion at scan time) — nothing left to stamp.
    assert "Total artifacts scanned:      7" in out2.getvalue()
    assert "Already-threaded (skip):      5" in out2.getvalue()


def test_write_preserves_executable_permission_bit(tmp_path: Path):
    """Porter addendum §5 — atomic rewrite must not strip an executable bit.
    The corpus is markdown-only in practice, but the stamping primitive is
    exercised directly here to prove the permission-preservation code path."""
    # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is always
    # 0o666 there regardless of the os.chmod() argument. Assert the real
    # invariant under test — mode preserved across the atomic rewrite — by
    # comparing against whatever mode chmod actually produced on this
    # platform, rather than asserting the literal POSIX octal.
    p = tmp_path / "exec-like.md"
    _write(p, "---\nworkstream: x\n---\nbody\n")
    os.chmod(p, 0o755)
    mode_before = stat.S_IMODE(os.stat(p).st_mode)
    mod._stamp_file(str(p), "dlv-test-000000")
    mode_after = stat.S_IMODE(os.stat(p).st_mode)
    assert mode_after == mode_before


# ---------------------------------------------------------------------------
# main() — argument parsing / fatal-error paths
# ---------------------------------------------------------------------------


def test_main_unknown_argument_exits_1(tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--bogus"], out=out, err=err)
    assert rc == 1
    assert "Unknown argument" in err.getvalue()


def test_main_missing_root_exits_1(tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--dry-run", "--root", str(tmp_path / "does-not-exist")], out=out, err=err)
    assert rc == 1
    assert "COORDINATOR_ROOT not found" in err.getvalue()


def test_main_help_exits_0_and_prints_usage(tmp_path: Path):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--help"], out=out, err=err)
    assert rc == 0
    assert "backfill-deliverable-spine" in out.getvalue()
    assert "Exit codes:" in out.getvalue()


def test_main_default_root_from_trampoline_arg(tmp_path: Path):
    """default_coordinator_root is honored when --root is not passed."""
    _build_clean_corpus(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--dry-run"], default_coordinator_root=str(tmp_path), out=out, err=err)
    assert rc == 0
    assert str(tmp_path) in out.getvalue()


def test_main_only_kind_unknown_returns_1_and_writes_nothing(tmp_path: Path):
    _build_clean_corpus(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--write", "--only-kind", "bogus", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 1
    h1 = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")
    assert "deliverable_id:" not in h1


def test_main_only_kind_scope_label_differs_from_default_mode_line(tmp_path: Path):
    """Narrowly named per the code-review P3 finding: this ONLY checks the
    Mode: line's scoping label — the report-body/write-pass parity claim
    lives in `test_main_only_kind_absent_is_byte_identical_to_default`
    below, which actually diffs full report bodies and --write bytes."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _build_clean_corpus(root_a)
    _build_clean_corpus(root_b)
    out_a, err_a = io.StringIO(), io.StringIO()
    out_b, err_b = io.StringIO(), io.StringIO()
    rc_a = main(["--dry-run", "--root", str(root_a)], out=out_a, err=err_a)
    rc_b = main(["--dry-run", "--only-kind", "plan", "--root", str(root_b)], out=out_b, err=err_b)
    # sanity: scoped run's report differs (Mode: line), unscoped stays default
    assert rc_a == 0
    assert "Mode: dry-run\n" in out_a.getvalue()
    assert "Mode: dry-run (scope: plan)\n" in out_b.getvalue()
    assert rc_b == 0


_DLV_ID_RE = re.compile(r"dlv-\S+")


def _normalize_minted_ids(text: str) -> str:
    """Replace every `dlv-...` token with a positional placeholder, keyed by
    order of first appearance, so two independent `--write` runs (each
    minting its own random hex suffix via `mint_deliverable_id.mint()`) can
    be diffed for identical STRUCTURE — same files grouped, same files
    stamped vs skipped, same relative id-sharing — without requiring the
    literal random hex to match, which it structurally never will."""
    seen: dict = {}

    def _repl(m: "re.Match") -> str:
        tok = m.group(0)
        if tok not in seen:
            seen[tok] = f"<ID{len(seen)}>"
        return seen[tok]

    return _DLV_ID_RE.sub(_repl, text)


def test_main_only_kind_absent_is_byte_identical_to_default(tmp_path: Path):
    """Review: coordinator:code-reviewer — the original version of this test
    only ran `--dry-run` and only compared the `Mode:` line, so the actual
    "unscoped run is unaffected by the --only-kind machinery" claim its name
    promised was established by code inspection, not by this test. This
    version diffs the FULL report body (Mode: line aside) between two
    identically-built, independently-run unscoped invocations, for both
    `--dry-run` and `--write`, and for `--write` additionally diffs the
    resulting on-disk bytes of every touched file (minted-id tokens
    normalized to positional placeholders, since `mint()` draws fresh random
    hex per invocation by design — see `mint_deliverable_id.mint`)."""

    def _strip_mode_line(report: str) -> str:
        # `Root:` and `Generated:` are per-invocation (tmp_path, wall clock) —
        # not part of the parity claim under test; strip both alongside
        # `Mode:` so the diff is scoped to actual write-decision content.
        return "\n".join(
            line
            for line in report.splitlines()
            if not line.startswith("Mode:")
            and not line.startswith("Root:")
            and not line.startswith("Generated:")
        )

    # --- dry-run leg ---
    root_a = tmp_path / "dry-a"
    root_b = tmp_path / "dry-b"
    _build_clean_corpus(root_a)
    _build_clean_corpus(root_b)
    out_a, err_a = io.StringIO(), io.StringIO()
    out_b, err_b = io.StringIO(), io.StringIO()
    rc_a = main(["--dry-run", "--root", str(root_a)], out=out_a, err=err_a)
    rc_b = main(["--dry-run", "--root", str(root_b)], out=out_b, err=err_b)
    assert rc_a == 0
    assert rc_b == 0
    assert _strip_mode_line(out_a.getvalue()) == _strip_mode_line(out_b.getvalue())
    assert "Mode: dry-run\n" in out_a.getvalue()
    assert "Mode: dry-run\n" in out_b.getvalue()

    # --- write leg: identical write decisions AND identical on-disk bytes ---
    root_c = tmp_path / "write-c"
    root_d = tmp_path / "write-d"
    _build_clean_corpus(root_c)
    _build_clean_corpus(root_d)
    out_c, err_c = io.StringIO(), io.StringIO()
    out_d, err_d = io.StringIO(), io.StringIO()
    rc_c = main(["--write", "--root", str(root_c)], out=out_c, err=err_c)
    rc_d = main(["--write", "--root", str(root_d)], out=out_d, err=err_d)
    assert rc_c == 0, f"stderr: {err_c.getvalue()}"
    assert rc_d == 0, f"stderr: {err_d.getvalue()}"

    report_c = _normalize_minted_ids(_strip_mode_line(out_c.getvalue()))
    report_d = _normalize_minted_ids(_strip_mode_line(out_d.getvalue()))
    assert report_c == report_d

    touched_rel_paths = [
        "state/handoffs/h1.md",
        "state/handoffs/h2-spinoff.md",
        "archive/handoffs/h4-archived.md",
        "docs/plans/2026-01-01-plan-one.md",
        "docs/plans/2026-01-02-plan-two.md",
        "archive/completed/c1.md",
        "state/roadmap/rm1/OVERVIEW.md",
    ]
    for rel in touched_rel_paths:
        bytes_c = (root_c / rel).read_text(encoding="utf-8")
        bytes_d = (root_d / rel).read_text(encoding="utf-8")
        assert _normalize_minted_ids(bytes_c) == _normalize_minted_ids(bytes_d), rel


def test_main_only_kind_sizing_stamps_sizing_leaves_handoff_unchanged(tmp_path: Path):
    import subprocess

    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write(
        tmp_path / "docs/plans/2026-01-01-citer.md",
        "---\nslug: citer\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    sizing = tmp_path / "state/sizings/2026-01-01-s.yaml"
    _write(sizing, _SCHEMA_VALID_SIZING_BODY)

    handoff = tmp_path / "state/handoffs/h1.md"
    _write(
        handoff,
        "---\nkind: handoff\nworkstream: alpha-thing\nstatus: open\n---\nBody h1\n",
    )
    handoff_before = handoff.read_text(encoding="utf-8")

    out, err = io.StringIO(), io.StringIO()
    rc = main(["--write", "--only-kind", "sizing", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 0, f"stderr: {err.getvalue()}"

    stamped = sizing.read_text(encoding="utf-8")
    assert "deliverable_id: dlv-citer-000000" in stamped
    handoff_after = handoff.read_text(encoding="utf-8")
    assert handoff_after == handoff_before


def test_main_only_kind_group_with_only_out_of_scope_files_skipped_no_mint(tmp_path: Path):
    _write(
        tmp_path / "state/handoffs/h1.md",
        "---\nkind: handoff\nworkstream: alpha-thing\nstatus: open\n---\nBody h1\n",
    )
    _write(
        tmp_path / "docs/plans/2026-01-01-plan-one.md",
        "---\nslug: plan-one\nworkstream: alpha-thing\n---\nPlan one body\n",
    )
    before = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")
    before_plan = (tmp_path / "docs/plans/2026-01-01-plan-one.md").read_text(encoding="utf-8")

    out, err = io.StringIO(), io.StringIO()
    rc = main(["--write", "--only-kind", "roadmap", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 0

    assert "[mint] alpha-thing" not in err.getvalue()
    after = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")
    after_plan = (tmp_path / "docs/plans/2026-01-01-plan-one.md").read_text(encoding="utf-8")
    assert after == before
    assert after_plan == before_plan


def test_main_only_kind_repeated_scopes_to_both(tmp_path: Path):
    _build_clean_corpus(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = main(
        ["--write", "--only-kind", "sizing", "--only-kind", "handoff", "--root", str(tmp_path)],
        out=out,
        err=err,
    )
    assert rc == 0
    h1 = (tmp_path / "state/handoffs/h1.md").read_text(encoding="utf-8")
    assert "deliverable_id: dlv-alpha-thing-" in h1
    plan_one = (tmp_path / "docs/plans/2026-01-01-plan-one.md").read_text(encoding="utf-8")
    assert "deliverable_id:" not in plan_one


def test_main_no_root_and_no_default_fails_loud(tmp_path: Path):
    """Neither --root nor default_coordinator_root: must fail loud (exit 1),
    never silently fall back to this module's own coordinator_core/ops/
    directory (a nonsense corpus root with no state/handoffs, docs/plans,
    etc.). Regression guard for the __file__-fallback bug fixed alongside
    the coordinator/bin/backfill-deliverable-spine.py trampoline's default-root
    resolution (see that trampoline's _resolve_default_coordinator_root()).
    """
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--dry-run"], default_coordinator_root=None, out=out, err=err)
    assert rc == 1
    assert "no corpus root available" in err.getvalue()


# ---------------------------------------------------------------------------
# main() — write-failure reporting (a refused write must not be reported as
# a stamp; regression guard for the "Stamped: 56" over-report bug)
# ---------------------------------------------------------------------------


def test_main_write_reports_schema_invalid_sizing_as_write_failed_not_stamped(tmp_path: Path, capsys):
    """A sizing whose post-mutation content fails schema validation must be
    reported as a refused write, not a stamp: the file is unchanged on disk,
    the report carries `[skip-write-failed]` and NOT `[stamp]` for it,
    `Stamped:` counts only records that really landed, `Write failures: 1`
    appears, and `main()` returns 4.
    """
    import subprocess

    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write(
        tmp_path / "docs/plans/2026-01-01-citer.md",
        "---\nslug: citer\nsizing_object: state/sizings/2026-01-01-s.yaml\n"
        "deliverable_id: dlv-citer-000000\n---\nBody\n",
    )
    sizing = tmp_path / "state/sizings/2026-01-01-s.yaml"
    # Schema-invalid: missing the required fields _SCHEMA_VALID_SIZING_BODY
    # carries (e.g. `estimate`/`route`/`detents`/etc.) — post-mutation
    # validate_frontmatter must reject this, raising MutateAbort inside
    # `_stamp_yaml_document`.
    _write(sizing, "schema: sizing-object\nintent: bare, invalid record\n")
    sizing_before = sizing.read_text(encoding="utf-8")

    out, err = io.StringIO(), io.StringIO()
    rc = main(["--write", "--root", str(tmp_path)], out=out, err=err)

    assert rc == 4
    sizing_after = sizing.read_text(encoding="utf-8")
    assert sizing_after == sizing_before, "refused write must not mutate the file on disk"
    report = out.getvalue()
    assert "[skip-write-failed]" in report
    assert "state/sizings/2026-01-01-s.yaml" in report
    assert "[stamp]" not in report
    assert "Stamped:            0" in report
    assert "Write failures:     1" in report
    # `_stamp_yaml_document`'s skip message is printed to the real
    # `sys.stderr` (module-level `print(..., file=sys.stderr)`), not the
    # `err` StringIO passed to `main()` — asserted via `capsys` instead of
    # `err.getvalue()`.
    captured = capsys.readouterr()
    assert "skip: _stamp_yaml_document:" in captured.err


def test_main_write_clean_run_still_returns_0_and_matches_prior_report_shape(tmp_path: Path):
    """A clean --write run (no write failures) still returns 0 and its
    WRITE COMPLETE block is byte-identical to the pre-fix shape — no
    `Write failures:` line appears when the count is zero.
    """
    _build_clean_corpus(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--write", "--root", str(tmp_path)], out=out, err=err)
    assert rc == 0, f"stderr: {err.getvalue()}"
    report = out.getvalue()
    assert "WRITE COMPLETE:" in report
    assert "Write failures:" not in report
    assert "ops" not in out.getvalue()
