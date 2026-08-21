"""Tests for ``coordinator_core.roadmap.cluster_scout``.

Purpose: pins the vendored ``cluster-scout-brief-fragment.json`` byte-identical
against its DoE-claude source (AC17's fragment-verbatim pin, mirroring
AC15/AC20), and pins the dispatch-planning behavior the plan C9 body and
AC17 describe: ``depth_disposition`` and ``excluded_clusters`` are consumed
verbatim and never computed, a ``deep-research`` cluster yields a directive
and no dispatch, an excluded cluster is never substituted with a web scout,
a measurement-derived cluster is skipped rather than overwritten, the brief
composition is exactly preamble + blank line + scope text, and the
``max_concurrent`` cap is enforced.

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md § C9
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.roadmap.cluster_scout import (
    Cluster,
    ClusterDispatchCapExceededError,
    ClusterDispatchInputError,
    ClusterScoutFragmentError,
    compose_brief,
    load_fragment,
    plan_cluster_dispatch,
)

CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
DOE_ROOT = CLAUDE_KLABAUTER_ROOT.parent / "DoE-claude"
VENDORED_FRAGMENT = (
    CLAUDE_KLABAUTER_ROOT / "coordinator_core" / "roadmap" / "fragments" / "cluster-scout-brief-fragment.json"
)
DOE_SOURCE_FRAGMENT = DOE_ROOT / "coordinator" / "contract" / "cluster-scout-brief-fragment.json"


def _fragment() -> dict:
    return load_fragment()


# --- byte-identity pin -------------------------------------------------


def test_vendored_fragment_exists_and_is_json() -> None:
    assert VENDORED_FRAGMENT.is_file()
    json.loads(VENDORED_FRAGMENT.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    not DOE_SOURCE_FRAGMENT.is_file(),
    reason="DoE-claude sibling clone not present at this box's expected path",
)
def test_vendored_fragment_is_byte_identical_to_doe_source() -> None:
    """AC17's fragment-verbatim pin, mirroring AC15/AC20: an unpinned second
    copy of a DoE-authored rule is the same defect class as the schema drift
    this plan avoids elsewhere."""
    vendored = VENDORED_FRAGMENT.read_bytes()
    source = DOE_SOURCE_FRAGMENT.read_bytes()
    assert vendored == source, (
        "coordinator_core/roadmap/fragments/cluster-scout-brief-fragment.json "
        "has drifted from DoE-claude's "
        "coordinator/contract/cluster-scout-brief-fragment.json -- re-vendor it "
        "verbatim, never hand-edit the local copy."
    )


def test_fragment_schema_key_names_its_contract() -> None:
    assert _fragment()["schema"] == "cluster-scout-brief-fragment"


def test_load_fragment_raises_loud_on_missing_file() -> None:
    missing = VENDORED_FRAGMENT.parent / "does-not-exist.json"
    with pytest.raises(ClusterScoutFragmentError):
        load_fragment(fragment_path=missing)


# --- composition ---------------------------------------------------------


def test_composition_is_preamble_blank_line_scope_only() -> None:
    brief = compose_brief("PREAMBLE", "SCOPE TEXT")
    assert brief == "PREAMBLE\n\nSCOPE TEXT"


def test_preamble_matches_the_fragment_verbatim() -> None:
    fragment = _fragment()
    clusters = [Cluster(id="c1", scope_text="scope one")]
    decisions = plan_cluster_dispatch(
        clusters,
        depth_disposition="solo-scout",
        excluded_clusters=[],
        fragment=fragment,
    )
    assert decisions[0].brief == compose_brief(
        fragment["brief"]["preamble_text"], "scope one"
    )
    assert decisions[0].brief.startswith(fragment["brief"]["preamble_text"])


# --- depth_disposition: solo-scout / deep-research / mixed ---------------


def test_solo_scout_dispatches_with_composed_brief() -> None:
    clusters = [Cluster(id="c1", scope_text="scope one")]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="solo-scout", excluded_clusters=[]
    )
    assert decisions == [
        decisions[0]
    ]  # sanity: single decision
    assert decisions[0].action == "dispatch"
    assert decisions[0].brief is not None


def test_deep_research_yields_directive_and_no_dispatch() -> None:
    """AC17: a deep-research cluster yields a directive and no dispatch --
    /research is PM-gated and never self-escalated."""
    clusters = [Cluster(id="c1", scope_text="scope one")]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="deep-research", excluded_clusters=[]
    )
    assert decisions[0].action == "deep-research-directive"
    assert decisions[0].brief is None


def test_mixed_disposition_is_per_cluster_mapping() -> None:
    clusters = [
        Cluster(id="c1", scope_text="scope one"),
        Cluster(id="c2", scope_text="scope two"),
    ]
    decisions = plan_cluster_dispatch(
        clusters,
        depth_disposition={"c1": "solo-scout", "c2": "deep-research"},
        excluded_clusters=[],
    )
    by_id = {d.cluster_id: d for d in decisions}
    assert by_id["c1"].action == "dispatch"
    assert by_id["c2"].action == "deep-research-directive"


def test_mixed_mapping_missing_a_cluster_raises_loud() -> None:
    clusters = [
        Cluster(id="c1", scope_text="scope one"),
        Cluster(id="c2", scope_text="scope two"),
    ]
    with pytest.raises(ClusterDispatchInputError):
        plan_cluster_dispatch(
            clusters,
            depth_disposition={"c1": "solo-scout"},
            excluded_clusters=[],
        )


def test_literal_mixed_value_is_rejected_not_guessed() -> None:
    """The fragment's "mixed" value describes the disposition OBJECT shape
    (a per-cluster mapping), never a literal per-cluster value -- passing it
    as a uniform value is a caller defect, never silently treated as
    solo-scout."""
    clusters = [Cluster(id="c1", scope_text="scope one")]
    with pytest.raises(ClusterDispatchInputError):
        plan_cluster_dispatch(
            clusters, depth_disposition="mixed", excluded_clusters=[]
        )


def test_unrecognised_disposition_value_raises_loud() -> None:
    clusters = [Cluster(id="c1", scope_text="scope one")]
    with pytest.raises(ClusterDispatchInputError):
        plan_cluster_dispatch(
            clusters, depth_disposition="quick-look", excluded_clusters=[]
        )


# --- excluded_clusters: never substituted ---------------------------------


def test_excluded_cluster_is_skipped_never_substituted() -> None:
    clusters = [Cluster(id="c1", scope_text="scope one")]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="solo-scout", excluded_clusters=["c1"]
    )
    assert decisions[0].action == "skip-excluded"
    assert decisions[0].brief is None


def test_exclusion_is_consumed_verbatim_never_recomputed() -> None:
    """excluded_clusters is an INPUT; a cluster absent from it dispatches
    even if it looks UE-flavored -- this module runs no probe of its own."""
    clusters = [Cluster(id="ue-widget-internal-api", scope_text="scope")]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="solo-scout", excluded_clusters=[]
    )
    assert decisions[0].action == "dispatch"


# --- measurement-derived: skipped, never overwritten ----------------------


def test_measurement_derived_cluster_is_skipped() -> None:
    clusters = [
        Cluster(id="c1", scope_text="scope one", measurement_derived=True)
    ]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="solo-scout", excluded_clusters=[]
    )
    assert decisions[0].action == "skip-measurement"
    assert decisions[0].brief is None


def test_measurement_derived_takes_precedence_over_excluded_check_order() -> None:
    """Order of precedence: exclusion first, then measurement -- an excluded
    cluster is reported as excluded even if it is also measurement-derived,
    since never_substitute is the load-bearing rule (fragment
    excluded_clusters.never_substitute)."""
    clusters = [
        Cluster(id="c1", scope_text="scope one", measurement_derived=True)
    ]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="solo-scout", excluded_clusters=["c1"]
    )
    assert decisions[0].action == "skip-excluded"


# --- max_concurrent cap ----------------------------------------------------


def test_dispatch_cap_is_pinned_at_eight() -> None:
    assert _fragment()["dispatch"]["max_concurrent"] == 8


def test_dispatch_over_cap_raises_loud() -> None:
    clusters = [Cluster(id=f"c{i}", scope_text=f"scope {i}") for i in range(9)]
    with pytest.raises(ClusterDispatchCapExceededError):
        plan_cluster_dispatch(
            clusters, depth_disposition="solo-scout", excluded_clusters=[]
        )


def test_dispatch_at_cap_succeeds() -> None:
    clusters = [Cluster(id=f"c{i}", scope_text=f"scope {i}") for i in range(8)]
    decisions = plan_cluster_dispatch(
        clusters, depth_disposition="solo-scout", excluded_clusters=[]
    )
    assert len(decisions) == 8
    assert all(d.action == "dispatch" for d in decisions)


def test_min_bytes_grounding_floor_is_pinned() -> None:
    assert _fragment()["output"]["min_bytes"] == 2048


# --- never rules survive ---------------------------------------------------


def test_every_never_rule_survives_in_the_vendored_copy() -> None:
    expected = (
        "no-research-pipeline",
        "no-substitute-scout",
        "no-scout-over-measurement",
        "no-brief-authoring",
    )
    assert tuple(rule["id"] for rule in _fragment()["never"]) == expected


def test_excluded_clusters_are_an_input_not_a_probe() -> None:
    excluded = _fragment()["excluded_clusters"]
    assert excluded["never_substitute"] is True
    assert "INPUT to this op" in excluded["source"]


def test_pm_gated_research_never_self_escalates() -> None:
    scout = _fragment()
    assert scout["depth_disposition"]["deep-research"].startswith(
        "emit a directive"
    )
    assert "never computed by it" in scout["depth_disposition"]["source"]
