"""
coordinator_core.ops.tests.test_slug_prefix_family — pytest for the
slug-prefix-family collision predicate (C4:
docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md).

Pinned against this plan's own motivating incident (§ Problem's 40/42/45
triple) — `cascade_backstop_sweep`'s own test module (AC7) shares this exact
fixture so a drift between the two call sites is one shared test's
regression, not two independently-green suites silently disagreeing.

Run (from repo root): python3 -m pytest coordinator_core/ops/tests/test_slug_prefix_family.py -q
"""

from __future__ import annotations

from coordinator_core.ops.slug_prefix_family import (
    cluster_slug_prefix_families,
    is_slug_prefix_family,
)

# This plan's § Problem table, verbatim: one shared source string
# ("coordinator-ops-buildout-from-fence-inventory") cut at three different
# truncation lengths (40/42/45) before the mint-time hash suffix.
ID_45 = "dlv-coordinator-ops-buildout-from-fence-inventory-df74c5"
ID_42 = "dlv-coordinator-ops-buildout-from-fence-invent-903224"
ID_40 = "dlv-coordinator-ops-buildout-from-fence-inve-fc3678"


class TestIsSlugPrefixFamily:
    def test_the_403_triple_is_pairwise_related(self):
        assert is_slug_prefix_family(ID_45, ID_42) is True
        assert is_slug_prefix_family(ID_42, ID_45) is True
        assert is_slug_prefix_family(ID_45, ID_40) is True
        assert is_slug_prefix_family(ID_42, ID_40) is True

    def test_equal_ids_are_not_a_family(self):
        assert is_slug_prefix_family(ID_45, ID_45) is False

    def test_unrelated_slugs_are_not_a_family(self):
        assert is_slug_prefix_family("dlv-alpha-workstream-111111", "dlv-beta-workstream-222222") is False

    def test_a_common_short_word_prefix_is_not_a_false_positive(self):
        # "coord" is a literal prefix of "coordinator", but neither slug is a
        # PREFIX of the other in full — the shared root diverges immediately
        # after, so this must not read as one family.
        assert is_slug_prefix_family("dlv-coord-alpha-111111", "dlv-coordinator-beta-222222") is False

    def test_ids_with_no_hash_suffix_still_compare_on_their_slug(self):
        # mint-from-stub shape (`dlv-<stub_id>`, no trailing -<6hex>) — the
        # predicate must not raise, and still compares literally.
        assert is_slug_prefix_family("dlv-stub-alpha", "dlv-stub-alpha-extended") is True

    def test_empty_or_bare_prefix_never_matches(self):
        assert is_slug_prefix_family("dlv-", "dlv-anything-111111") is False
        assert is_slug_prefix_family("", "dlv-anything-111111") is False


class TestClusterSlugPrefixFamilies:
    def test_the_403_triple_clusters_as_one_family(self):
        groups = cluster_slug_prefix_families([ID_45, ID_42, ID_40])
        assert groups == [sorted([ID_45, ID_42, ID_40])]

    def test_a_singleton_with_no_family_partner_is_omitted(self):
        groups = cluster_slug_prefix_families([ID_45, ID_42, ID_40, "dlv-unrelated-workstream-999999"])
        assert groups == [sorted([ID_45, ID_42, ID_40])]

    def test_two_disjoint_families_cluster_separately(self):
        other_a = "dlv-second-family-workstream-aaaaaa"
        other_b = "dlv-second-family-work-bbbbbb"
        groups = cluster_slug_prefix_families([ID_45, ID_42, other_a, other_b])
        assert groups == sorted(
            [sorted([ID_45, ID_42]), sorted([other_a, other_b])]
        )

    def test_empty_corpus_is_empty(self):
        assert cluster_slug_prefix_families([]) == []

    def test_blank_and_duplicate_entries_are_ignored(self):
        groups = cluster_slug_prefix_families([ID_45, ID_45, "", "  ", ID_42])
        assert groups == [sorted([ID_45, ID_42])]
