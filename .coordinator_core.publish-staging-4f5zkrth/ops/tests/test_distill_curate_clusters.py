"""
coordinator_core.ops.tests.test_distill_curate_clusters

Tests for the distill.curate_clusters COMPUTE_ONLY op (deterministic
keep/normalize/merge/drop gate over a {system_tag: count} map).

Fixtures are drawn from the live-run evidence cited in the dispatch brief:
  - three naming conventions (kebab-slug, dotted namespace, Title-Case prose)
  - the performance-* (9 tags) and git-* (8 tags) fragmentation families
  - the N/A placeholder
  - bare-abstraction catch-alls (meta, coordination, governance, ...)
  - a synonym-denylist-trap case: "stewardship", a bare catch-all NOT present
    in any hardcoded word list, which must still be caught structurally.

Spec backlink: coordinator_core/ops/distill_curate_clusters.py
"""

from __future__ import annotations

import asyncio

import coordinator_core.ops  # noqa: F401 -- import for side effect: registers every op, incl. distill.curate_clusters (see _EAGER_OP_MODULES)
from coordinator_core import ipc
from coordinator_core.ops.distill_curate_clusters import (
    _BARE_ABSTRACTION_VOCAB,
    _handler,
    curate_clusters,
)


def test_kebab_slug_already_canonical_keeps() -> None:
    result = curate_clusters({"git-safety": 5})
    v = result["verdicts"][0]
    assert v["verdict"] == "keep"
    assert v["canonical_slug"] == "git-safety"
    assert result["degraded"] is False


def test_dotted_namespace_normalizes() -> None:
    result = curate_clusters({"claude-klabauter.ops.fleet-archival": 4})
    v = result["verdicts"][0]
    assert v["verdict"] == "normalize"
    assert v["canonical_slug"] == "claude-klabauter-ops-fleet-archival"


def test_title_case_prose_normalizes() -> None:
    result = curate_clusters({"Hook contract model": 3})
    v = result["verdicts"][0]
    assert v["verdict"] == "normalize"
    assert v["canonical_slug"] == "hook-contract-model"


def test_performance_fragmentation_family_merges_into_largest() -> None:
    tags = {
        "performance-characteristics": 3,
        "performance-optimization": 6,
        "performance-analysis": 2,
        "performance-budgets": 1,
        "performance-regression": 1,
        "performance-latency": 1,
        "performance-profiling": 1,
        "performance-diagnostic": 1,
    }
    result = curate_clusters(tags)
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["performance-optimization"]["verdict"] == "keep"
    for tag in tags:
        if tag == "performance-optimization":
            continue
        assert by_tag[tag]["verdict"] == "merge"
        assert by_tag[tag]["merge_target"] == "performance-optimization"


def test_git_fragmentation_family_merges() -> None:
    tags = {
        "git-safety": 4,
        "git-guards": 3,
        "git-mechanics": 2,
        "git-safety-audit": 1,
        "git-commit-hazards": 1,
        "git-index-guard": 1,
        "git-index-management": 1,
        "git-optimization": 1,
    }
    result = curate_clusters(tags)
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["git-safety"]["verdict"] == "keep"
    assert by_tag["git-mechanics"]["verdict"] == "merge"
    assert by_tag["git-mechanics"]["merge_target"] == "git-safety"


def test_na_placeholder_drops() -> None:
    result = curate_clusters({"N/A": 2})
    v = result["verdicts"][0]
    assert v["verdict"] == "drop"
    assert v["canonical_slug"] is None
    assert "placeholder" in v["reason"]


def test_empty_and_tbd_placeholders_drop() -> None:
    result = curate_clusters({"": 1, "tbd": 1, "unknown": 1})
    for v in result["verdicts"]:
        assert v["verdict"] == "drop"


def test_bare_abstraction_catchalls_drop_structurally() -> None:
    tags = {"meta": 5, "coordination": 3, "governance": 2, "ordering": 1}
    result = curate_clusters(tags)
    for v in result["verdicts"]:
        assert v["verdict"] == "drop", v
        assert "no compound domain-qualifier sibling" in v["reason"]


def test_synonym_denylist_trap_is_closed() -> None:
    """A catch-all NOT present in any hardcoded word list must still be
    caught by the structural rule (bare token, no compound sibling)."""
    assert "stewardship" not in _BARE_ABSTRACTION_VOCAB
    result = curate_clusters({"stewardship": 40})
    v = result["verdicts"][0]
    assert v["verdict"] == "drop"
    assert "no compound domain-qualifier sibling" in v["reason"]
    # high count does not rescue a structurally bare catch-all — PM ruling:
    # "either shit deserves a home or it doesn't", not "unless it's popular".


def test_bare_token_with_compound_sibling_merges_not_drops() -> None:
    """A bare tag sharing a leading token with a COMPOUND sibling has a real
    structural home — discovered by shared prefix, not by name lookup."""
    tags = {"coordination": 3, "coordination-ledger": 5}
    result = curate_clusters(tags)
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["coordination-ledger"]["verdict"] == "keep"
    assert by_tag["coordination"]["verdict"] == "merge"
    assert by_tag["coordination"]["merge_target"] == "coordination-ledger"


def test_singleton_no_merge_target_drops() -> None:
    result = curate_clusters({"distillation-log-schema": 1}, keep_threshold=2)
    v = result["verdicts"][0]
    assert v["verdict"] == "drop"
    assert "no merge target" in v["reason"]


def test_singleton_above_threshold_keeps() -> None:
    result = curate_clusters({"distillation-log-schema": 2}, keep_threshold=2)
    v = result["verdicts"][0]
    assert v["verdict"] == "keep"


def test_shape_drift_variants_of_same_slug_fold_together() -> None:
    """Two raw tags that normalize to the IDENTICAL slug (pure shape drift,
    no distinct sibling) both resolve "normalize"/"keep" against that one
    slug; a raw tag whose normalized slug DIFFERS (a genuine sibling in the
    same leading-token family, e.g. "...-scope" vs "...-trail") is a MERGE,
    not a normalize — shape-drift and family-merge are orthogonal axes."""
    tags = {"review-trail": 2, "Review-Trail": 1, "review_trail_safety": 1}
    result = curate_clusters(tags, keep_threshold=2)
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["review-trail"]["verdict"] == "keep"
    assert by_tag["Review-Trail"]["verdict"] == "normalize"
    assert by_tag["Review-Trail"]["canonical_slug"] == "review-trail"
    assert by_tag["review_trail_safety"]["verdict"] == "merge"
    assert by_tag["review_trail_safety"]["merge_target"] == "review-trail"


def test_determinism_same_input_twice_identical_output() -> None:
    tags = {
        "git-safety": 4,
        "git-guards": 3,
        "performance-optimization": 6,
        "performance-analysis": 2,
        "N/A": 2,
        "meta": 1,
        "claude-klabauter.ops.fleet-archival": 4,
        "Hook contract model": 3,
        "distillation-log-schema": 1,
    }
    r1 = curate_clusters(tags)
    r2 = curate_clusters(tags)
    assert r1 == r2


def test_empty_input_is_loudly_degraded_not_a_clean_zero() -> None:
    """The failure mode this op exists to prevent: an unparseable/empty
    input must never look like "0 problems found"."""
    result = curate_clusters({})
    assert result["degraded"] is True
    assert result["verdicts"] == []
    assert result["counts"]["total"] == 0


def test_non_dict_input_is_degraded() -> None:
    result = curate_clusters(None)  # type: ignore[arg-type]
    assert result["degraded"] is True
    result2 = curate_clusters("not-a-dict")  # type: ignore[arg-type]
    assert result2["degraded"] is True


def test_removing_bare_abstraction_vocab_changes_no_verdict() -> None:
    """Asserts the closed word list is SECONDARY/annotation-only, per the
    module docstring's discriminator section — it must never be the gating
    mechanism. Simulated by comparing against a tag not in the list
    (test_synonym_denylist_trap_is_closed) producing an identical verdict
    shape (same verdict, same reason-substring) to one that IS in the list."""
    in_list = curate_clusters({"meta": 5})["verdicts"][0]
    not_in_list = curate_clusters({"stewardship": 5})["verdicts"][0]
    assert in_list["verdict"] == not_in_list["verdict"] == "drop"
    core_reason = "bare token, no compound domain-qualifier sibling in the corpus (no merge target)"
    assert core_reason in in_list["reason"]
    assert core_reason in not_in_list["reason"]


def test_misc_and_other_are_structurally_dropped_not_denylisted() -> None:
    """Review: code-reviewer Finding (2026-08-06) — "misc"/"other" used to be
    listed in `_PLACEHOLDER_VALUES`, which pre-empted the structural test and
    made this the module's ONE actual denylist-as-primary-mechanism spot,
    contradicting its own Negative-spec. This pins that they now reach and
    are caught by the structural bare-token test (verdict "drop",
    drop_cause "bare-no-sibling", NOT "placeholder"), with the closed vocab
    list only annotating the reason string — same shape as any other bare
    abstraction noun (`test_removing_bare_abstraction_vocab_changes_no_verdict`
    only exercised "meta"/"stewardship" and missed this)."""
    result = curate_clusters({"misc": 5, "other": 3})
    verdicts = {v["tag"]: v for v in result["verdicts"]}
    for tag in ("misc", "other"):
        assert verdicts[tag]["verdict"] == "drop"
        assert verdicts[tag]["drop_cause"] == "bare-no-sibling"
        assert "bare token, no compound domain-qualifier sibling" in verdicts[tag]["reason"]
        assert "also matches the closed bare-abstraction word list" in verdicts[tag]["reason"]


def test_downstream_consumer_contract_drop_and_merge_fields() -> None:
    """Enforces the module docstring's Downstream-consumer contract, ratified
    2026-08-06 with doe-claude-em: their /distill C3 leg logs each dropped
    nugget as EPHEMERAL-with-reason, and their clustering resolves a merged tag
    to its destination — so drop-set enumerability, drop `reason` presence, and
    `merge_target` presence are a stability commitment to a named sibling
    consumer, not incidental output. Exercises all three drop paths at once."""
    tag_counts = {
        "N/A": 1,  # placeholder path
        "stewardship": 4,  # bare token, no compound sibling
        "audit-trail": 1,  # family total 1 < keep_threshold 2
        "git-safety": 5,  # keep (canonical family member)
        "git-mechanics": 2,  # merge into git-safety
    }
    # Threshold pinned explicitly: this test asserts the FIELD contract, and
    # must not become a hostage to how an omitted threshold is derived.
    result = curate_clusters(tag_counts, keep_threshold=2)
    assert result["degraded"] is False

    # One verdict per raw input tag — the invariant that makes the drop set a
    # filter over `verdicts` rather than a separately-maintained array.
    assert len(result["verdicts"]) == len(tag_counts)
    assert {v["tag"] for v in result["verdicts"]} == set(tag_counts)

    dropped = [v for v in result["verdicts"] if v["verdict"] == "drop"]
    assert {v["tag"] for v in dropped} == {"N/A", "stewardship", "audit-trail"}
    for v in dropped:
        assert isinstance(v["reason"], str) and v["reason"].strip()

    merged = [v for v in result["verdicts"] if v["verdict"] == "merge"]
    assert merged, "fixture must exercise the merge path"
    for v in merged:
        assert v["merge_target"], "a merge verdict must name its destination"
        # canonical_slug is the tag's OWN slug, distinct from the destination.
        assert v["canonical_slug"] != v["merge_target"]
    assert merged[0]["merge_target"] == "git-safety"


def test_keep_verdict_weights_nugget_volume_via_keep_threshold() -> None:
    """The keep gate DOES weight per-tag nugget volume — summed across the
    family, compared against `keep_threshold`. Consumer-visible consequence
    (answered to doe-claude-em 2026-08-06): a caller wanting singleton-floor
    semantics ("a 1-2 nugget cluster does not earn its own file") must pass
    keep_threshold=3; the default of 2 drops only 1-nugget families."""
    two_nuggets = {"audit-trail": 2}
    assert curate_clusters(two_nuggets)["verdicts"][0]["verdict"] == "keep"
    assert curate_clusters(two_nuggets, keep_threshold=3)["verdicts"][0]["verdict"] == "drop"

    # Volume is summed across the family, not judged per-tag: two count-1
    # siblings clear a threshold neither would clear alone.
    family = curate_clusters({"audit-trail": 1, "audit-scope": 1})
    assert {v["verdict"] for v in family["verdicts"]} == {"keep", "merge"}


def test_handler_wire_contract() -> None:
    result = _handler(
            {"tag_counts": {"git-safety": 5, "N/A": 1}},
            repo_root=None,
        )
    
    assert result["degraded"] is False
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["git-safety"]["verdict"] == "keep"
    assert by_tag["N/A"]["verdict"] == "drop"


def test_handler_missing_tag_counts_is_degraded() -> None:
    result = _handler({}, repo_root=None)
    assert result["degraded"] is True


def test_op_reachable_via_registry_and_jsonrpc_dispatch() -> None:
    """End-to-end seam check: registry -> ipc.dispatch_message -> structured JSON
    verdict, not just a direct call into _handler. This is the seam a Phase-0
    caller (a fresh process invoking `python -m coordinator_core.invoke
    distill.curate_clusters '<params>'`) actually goes through — a handler-level
    test alone would miss a registration or JSON-RPC-envelope regression."""
    assert "distill.curate_clusters" in ipc._REGISTRY
    reply = asyncio.run(
        ipc.dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "distill.curate_clusters",
                "params": {"tag_counts": {"git-safety": 5, "N/A": 1, "meta": 2}},
            }
        )
    )
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 1
    assert "error" not in reply
    result = reply["result"]
    assert result["degraded"] is False
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["git-safety"]["verdict"] == "keep"
    assert by_tag["N/A"]["verdict"] == "drop"
    assert by_tag["meta"]["verdict"] == "drop"
    assert result["counts"] == {
        "total": 3,
        "keep": 1,
        "normalize": 0,
        "merge": 0,
        "drop": 2,
        "drop_by_cause": {"placeholder": 1, "bare-no-sibling": 1, "below-threshold": 0},
    }


def test_handler_invalid_keep_threshold_falls_back_to_auto() -> None:
    """An invalid explicit value (< 1) must reach the auto-derivation path,
    not silently become 2 — a single-tag count-1 corpus auto-derives to 1
    (100% drop share at threshold 2 exceeds the 25% cap), so it KEEPS."""
    result = _handler(
            {"tag_counts": {"distillation-log-schema": 1}, "keep_threshold": -3},
            repo_root=None,
        )
    
    v = result["verdicts"][0]
    assert v["verdict"] == "keep"
    assert result["threshold_applied"] == 1
    assert result["threshold_auto"] is True


def test_explicit_keep_threshold_2_on_cold_start_corpus_still_drops() -> None:
    """Regression guard for the sibling repo's 1-vs-2 measurement: an
    EXPLICIT keep_threshold must be used verbatim, never auto-adjusted, even
    on a corpus shaped exactly like the cold-start case that would otherwise
    trigger the auto fallback."""
    tags = {f"tag{i}-subject": 1 for i in range(20)}
    result = curate_clusters(tags, keep_threshold=2)
    assert result["threshold_applied"] == 2
    assert result["threshold_auto"] is False
    assert all(v["verdict"] == "drop" for v in result["verdicts"])
    assert all(v["drop_cause"] == "below-threshold" for v in result["verdicts"])


def test_cold_start_corpus_no_threshold_supplied_auto_derives_to_1() -> None:
    tags = {f"tag{i}-subject": 1 for i in range(20)}
    result = curate_clusters(tags)
    assert result["threshold_applied"] == 1
    assert result["threshold_auto"] is True
    assert all(v["verdict"] == "keep" for v in result["verdicts"])


def test_mature_corpus_no_threshold_supplied_stays_at_2() -> None:
    """A mature-shaped corpus (families with real volume, low drop share at
    threshold 2) must NOT flip to 1 just because keep_threshold was
    omitted."""
    tags = {}
    for i in range(20):
        tags[f"tag{i}-primary"] = 5
        tags[f"tag{i}-secondary"] = 5
    result = curate_clusters(tags)
    assert result["threshold_applied"] == 2
    assert result["threshold_auto"] is True
    assert result["nugget_drop_share"] == 0.0


def test_drop_cause_correctness_across_all_three() -> None:
    tags = {
        "N/A": 2,
        "meta": 3,
        "distillation-log-schema": 1,
    }
    result = curate_clusters(tags, keep_threshold=2)
    by_tag = {v["tag"]: v for v in result["verdicts"]}
    assert by_tag["N/A"]["drop_cause"] == "placeholder"
    assert by_tag["meta"]["drop_cause"] == "bare-no-sibling"
    assert by_tag["distillation-log-schema"]["drop_cause"] == "below-threshold"
    assert result["counts"]["drop_by_cause"] == {
        "placeholder": 1,
        "bare-no-sibling": 1,
        "below-threshold": 1,
    }


def test_drop_cause_none_for_non_drop_verdicts() -> None:
    result = curate_clusters({"git-safety": 5}, keep_threshold=2)
    v = result["verdicts"][0]
    assert v["verdict"] == "keep"
    assert v["drop_cause"] is None


def test_nugget_drop_share_arithmetic() -> None:
    tags = {"git-safety": 3, "distillation-log-schema": 1}
    result = curate_clusters(tags, keep_threshold=2)
    # git-safety(3) keeps, distillation-log-schema(1) drops -> 1/4 = 0.25
    assert result["nugget_drop_share"] == 0.25


def test_auto_derivation_boundary_at_exactly_25_percent_stays_at_2() -> None:
    """Regression guard for the strict `>` in `_resolve_keep_threshold`: a
    corpus whose threshold-2 probe drops EXACTLY 25% of nuggets must stay at
    threshold 2 (only a share that EXCEEDS the cap falls back to 1). Drives
    the AUTO probe itself (no explicit threshold passed) — the arithmetic
    test above (`test_nugget_drop_share_arithmetic`) pins an explicit
    threshold and never exercises the auto path. Would fail if `>` were
    swapped to `>=`."""
    tags = {"git-safety": 3, "distillation-log-schema": 1}
    # git-safety(3) keeps at threshold 2, distillation-log-schema(1) drops
    # -> drop_share = 1/4 = 0.25 exactly, at the cap, not exceeding it.
    result = curate_clusters(tags)
    assert result["threshold_applied"] == 2
    assert result["threshold_auto"] is True
    assert result["nugget_drop_share"] == 0.25


def test_auto_derivation_zero_sum_tag_counts_falls_back_to_cold_start() -> None:
    """A non-empty `tag_counts` whose values sum to zero (e.g. a tag recorded
    with a zero nugget count) must not raise ZeroDivisionError — the
    `total_nuggets == 0` guard in `_resolve_keep_threshold` routes it to the
    cold-start fallback threshold, same as a genuinely empty corpus would if
    it weren't caught earlier by the `degraded` check."""
    result = curate_clusters({"tag-a": 0})
    assert result["degraded"] is False
    assert result["threshold_applied"] == 1
    assert result["threshold_auto"] is True


def test_determinism_of_auto_threshold_same_input_twice() -> None:
    tags = {f"tag{i}-subject": 1 for i in range(20)}
    r1 = curate_clusters(tags)
    r2 = curate_clusters(tags)
    assert r1 == r2


def test_degraded_path_retains_shape_with_new_fields() -> None:
    result = curate_clusters({})
    assert result["degraded"] is True
    assert result["verdicts"] == []
    assert result["counts"]["total"] == 0
    assert result["counts"]["drop_by_cause"] == {
        "placeholder": 0,
        "bare-no-sibling": 0,
        "below-threshold": 0,
    }
    assert result["threshold_applied"] == 0
    assert result["threshold_auto"] is False
    assert result["nugget_drop_share"] == 0.0
