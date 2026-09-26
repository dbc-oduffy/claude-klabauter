"""
coordinator_core.ops.tests.test_deferral_detect_partial_strangle

Tests for the pure classify_partial_strangles() decision core of the
"deferral.detect_partial_strangle" op, plus the fs/scan I/O boundary
(_scan_manifest_candidates / _make_shipped_check / _make_planned_check) and the
registered-op wiring (_handler).

Coverage (acceptance-mapped, design.md § Detector 1):
  (a) DR-210-shaped fixture WITH manifest + memo-tool-rebuild-shaped plan mentioning
      list/draft/compose -> CLEAN (self-test of the real seeded manifest + real plan).
  (b) Same manifest WITHOUT the plan -> flags UNPLANNED {list, draft, compose} (proves
      pre-plan catch).
  (c) A docs/decisions/*.md doc with no strangler-endpoint fence -> not a candidate at all
      (opt-in discovery; never surfaced as indeterminate, never a false clean).
  (c2) A docs/decisions/*.md doc WITH the fence but unparseable YAML -> indeterminate +
      notice (the only shape indeterminate fires post-2026-07-21-pivot).
  (d) Mixed multi-strangler arbitration (finding > indeterminate > clean).
  (e) Pure core exercised directly with injected callables.
  (f) Registered handler (_handler) exercised directly (plain sync call), including a
      real-tree smoke test against THIS repo's actual seeded DR-210 manifest, and a
      real-tree noise-regression test proving the ~25-review-sidecar / ~13-legacy-plan
      filename-glob wall this pivot closed does not recur.
  (g) A manifest can never live in docs/plans/ (structural closure of the self-satisfying
      planned_check hazard — manifest home and planned-evidence scan are disjoint).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.ops.deferral_detect_partial_strangle import (
    _extract_fenced_manifest_block,
    _handler,
    _make_planned_check,
    _make_shipped_check,
    _parse_manifest_candidate,
    _scan_manifest_candidates,
    classify_partial_strangles,
)


DR210_MANIFEST_ENTRY = {
    "kind": "manifest",
    "path": "docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md",
    "strangler_id": "DR-210",
    "slug": "claude-klabauter-native-tooling-ownership-strangler",
    "declared_verbs": ["list", "draft", "compose", "send"],
    "shipped_native_op": {"send": "coordinator_core/ops/fleet/memo_send.py"},
}


class TestClassifyPartialStranglesClean:
    def test_all_declared_verbs_shipped_or_planned_is_clean(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert result["state"] == "clean"

    def test_clean_carries_neither_offer_nor_notice(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert "offer" not in result
        assert "notice" not in result
        assert "findings" not in result
        assert "notices" not in result

    def test_no_manifests_at_all_is_clean(self):
        result = classify_partial_strangles(
            [],
            shipped_check=lambda path: False,
            planned_check=lambda verb, strangler: False,
        )
        assert result["state"] == "clean"


class TestClassifyPartialStranglesFindings:
    def test_unplanned_verbs_flag_partial_strangles_found(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: False,
        )
        assert result["state"] == "partial_strangles_found"

    def test_unplanned_set_is_declared_minus_shipped_minus_planned(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: False,
        )
        finding = result["findings"][0]
        assert finding["shipped"] == ["send"]
        assert finding["planned"] == []
        assert finding["unplanned"] == ["list", "draft", "compose"]

    def test_offer_present_and_names_strangler_and_unplanned_set(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: False,
        )
        assert "offer" in result
        assert result["offer"]
        assert "DR-210" in result["offer"]
        assert "list" in result["offer"]
        assert "draft" in result["offer"]
        assert "compose" in result["offer"]
        assert "UNPLANNED" in result["offer"]

    def test_partial_planning_leaves_only_the_unplanned_remainder(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: verb in ("list", "draft"),
        )
        finding = result["findings"][0]
        assert set(finding["planned"]) == {"list", "draft"}
        assert finding["unplanned"] == ["compose"]

    def test_shipped_check_receives_the_declared_shipped_path(self):
        seen_paths = []

        def _shipped_check(path):
            seen_paths.append(path)
            return False

        classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=_shipped_check,
            planned_check=lambda verb, strangler: True,
        )
        assert seen_paths == ["coordinator_core/ops/fleet/memo_send.py"]

    def test_verb_absent_from_shipped_native_op_never_counted_shipped(self):
        # "list" has no shipped_native_op entry at all in DR210_MANIFEST_ENTRY —
        seen_paths = []

        def _shipped_check(path):
            seen_paths.append(path)
            return True

        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=_shipped_check,
            planned_check=lambda verb, strangler: False,
        )
        finding = result["findings"][0]
        assert seen_paths == ["coordinator_core/ops/fleet/memo_send.py"]
        assert "list" not in finding["shipped"]
        assert "list" in finding["unplanned"]

    def test_planned_check_not_consulted_for_already_shipped_verb(self):
        seen_verbs = []

        def _planned_check(verb, strangler):
            seen_verbs.append(verb)
            return False

        classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: True,
            planned_check=_planned_check,
        )
        assert "send" not in seen_verbs

    def test_offer_names_covering_plan_via_attribution(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: (
                "docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md"
                if verb in ("list", "draft")
                else None
            ),
        )
        # "compose" is UNPLANNED here (planned_check returns None for it) so the finding
        assert "via 2026-07-21-memo-tool-rebuild-full-ownership" in result["offer"]
        finding = result["findings"][0]
        assert set(finding["planned"]) == {"list", "draft"}

    def test_offer_falls_back_gracefully_when_planned_check_returns_truthy_non_string(self):
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: True if verb == "list" else None,
        )
        assert "via" not in result["offer"]
        finding = result["findings"][0]
        assert "list" in finding["planned"]


class TestClassifyPartialStranglesIndeterminate:
    def test_no_manifest_block_is_indeterminate(self):
        entry = {
            "kind": "indeterminate",
            "path": "docs/decisions/DR-999-some-strangler.md",
            "slug": "some-strangler",
        }
        result = classify_partial_strangles(
            [entry],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert result["state"] == "indeterminate"

    def test_indeterminate_carries_notice_not_offer(self):
        entry = {
            "kind": "indeterminate",
            "path": "docs/decisions/DR-999-some-strangler.md",
            "slug": "some-strangler",
        }
        result = classify_partial_strangles(
            [entry],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert "notice" in result
        assert result["notice"]
        assert "offer" not in result
        assert "strangler-endpoint" in result["notice"]

    def test_indeterminate_never_collapses_to_clean(self):
        entry = {
            "kind": "indeterminate",
            "path": "docs/decisions/DR-999-some-strangler.md",
            "slug": "some-strangler",
        }
        result = classify_partial_strangles(
            [entry],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert result["state"] != "clean"


class TestClassifyPartialStranglesMixedArbitration:
    def test_finding_plus_indeterminate_state_stays_finding(self):
        indeterminate_entry = {
            "kind": "indeterminate",
            "path": "docs/decisions/DR-999-some-strangler.md",
            "slug": "some-strangler",
        }
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY, indeterminate_entry],
            shipped_check=lambda path: path == "coordinator_core/ops/fleet/memo_send.py",
            planned_check=lambda verb, strangler: False,
        )
        assert result["state"] == "partial_strangles_found"
        assert len(result["findings"]) == 1
        assert "notices" in result
        assert len(result["notices"]) == 1

    def test_clean_plus_indeterminate_state_is_indeterminate(self):
        indeterminate_entry = {
            "kind": "indeterminate",
            "path": "docs/decisions/DR-999-some-strangler.md",
            "slug": "some-strangler",
        }
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY, indeterminate_entry],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert result["state"] == "indeterminate"
        assert "findings" not in result

    def test_two_findings_both_carried_in_offer_and_findings_list(self):
        other_manifest = {
            "kind": "manifest",
            "path": "docs/decisions/DR-888-other-strangler.md",
            "strangler_id": "DR-888",
            "slug": "other-strangler",
            "declared_verbs": ["read", "write"],
            "shipped_native_op": {},
        }
        result = classify_partial_strangles(
            [DR210_MANIFEST_ENTRY, other_manifest],
            shipped_check=lambda path: False,
            planned_check=lambda verb, strangler: False,
        )
        assert result["state"] == "partial_strangles_found"
        assert len(result["findings"]) == 2
        assert "DR-210" in result["offer"]
        assert "DR-888" in result["offer"]

    def test_multiple_indeterminate_stranglers_pluralized_notice(self):
        e1 = {"kind": "indeterminate", "path": "docs/decisions/DR-A.md", "slug": "a"}
        e2 = {"kind": "indeterminate", "path": "docs/decisions/DR-B.md", "slug": "b"}
        result = classify_partial_strangles(
            [e1, e2],
            shipped_check=lambda path: True,
            planned_check=lambda verb, strangler: True,
        )
        assert result["state"] == "indeterminate"
        assert len(result["notices"]) == 2


MANIFEST_BLOCK_TEXT = """# Some DR

## Strangler endpoint manifest (machine-readable)

```yaml strangler-endpoint
strangler_id: DR-210
declared_verbs: [list, draft, compose, send]
shipped_native_op:
  send: coordinator_core/ops/fleet/memo_send.py
```

## Other section
"""


class TestExtractFencedManifestBlock:
    def test_extracts_block_content(self):
        block = _extract_fenced_manifest_block(MANIFEST_BLOCK_TEXT)
        assert block is not None
        assert "strangler_id: DR-210" in block
        assert "declared_verbs" in block

    def test_returns_none_when_absent(self):
        assert _extract_fenced_manifest_block("# No manifest here\n") is None


class TestParseManifestCandidate:
    def test_manifest_present_parses_fields(self):
        candidate = _parse_manifest_candidate(
            "docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md",
            MANIFEST_BLOCK_TEXT,
        )
        assert candidate["kind"] == "manifest"
        assert candidate["strangler_id"] == "DR-210"
        assert candidate["declared_verbs"] == ["list", "draft", "compose", "send"]
        assert candidate["shipped_native_op"] == {"send": "coordinator_core/ops/fleet/memo_send.py"}
        assert candidate["slug"] == "claude-klabauter-native-tooling-ownership-strangler"

    def test_manifest_absent_is_indeterminate(self):
        candidate = _parse_manifest_candidate(
            "docs/decisions/DR-999-some-strangler.md", "# No manifest block\n"
        )
        assert candidate["kind"] == "indeterminate"
        assert candidate["slug"] == "DR-999-some-strangler"

    def test_unparseable_yaml_is_indeterminate_not_a_crash(self):
        bad_text = "```yaml strangler-endpoint\n:::not valid yaml:::\n  - [unterminated\n```\n"
        candidate = _parse_manifest_candidate("docs/decisions/DR-777-bad.md", bad_text)
        assert candidate["kind"] == "indeterminate"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_dr210_fixture(repo_root: Path, ship_send: bool = True) -> None:
    dr_text = (
        "---\ntitle: fixture DR\n---\n\n"
        "# DR-210 fixture\n\n"
        "## Strangler endpoint manifest (machine-readable)\n\n"
        "```yaml strangler-endpoint\n"
        "strangler_id: DR-210\n"
        "declared_verbs: [list, draft, compose, send]\n"
        "shipped_native_op:\n"
        "  send: coordinator_core/ops/fleet/memo_send.py\n"
        "```\n"
    )
    _write(
        repo_root / "docs" / "decisions" / "DR-210-claude-klabauter-native-tooling-ownership-strangler.md",
        dr_text,
    )
    if ship_send:
        _write(
            repo_root / "coordinator_core" / "ops" / "fleet" / "memo_send.py",
            "# fixture native op\n",
        )


def _seed_memo_tool_rebuild_plan(repo_root: Path) -> None:
    plan_text = (
        "---\ntitle: memo tool rebuild\n---\n\n"
        "# Memo-tool rebuild\n\n"
        "This plan references DR-210 and plans the `list`, `draft`, and `compose` verbs "
        "for native ownership. See docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md.\n"
    )
    _write(repo_root / "docs" / "plans" / "2026-07-21-memo-tool-rebuild-full-ownership.md", plan_text)


class TestScanManifestCandidatesRealFixture(object):
    def test_dr210_fixture_with_plan_reads_clean_end_to_end(self, tmp_path):
        _seed_dr210_fixture(tmp_path)
        _seed_memo_tool_rebuild_plan(tmp_path)

        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert len(manifests) == 1
        assert manifests[0]["kind"] == "manifest"

        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)
        assert result["state"] == "clean"

    def test_dr210_fixture_without_plan_flags_unplanned(self, tmp_path):
        _seed_dr210_fixture(tmp_path)

        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)

        assert result["state"] == "partial_strangles_found"
        finding = result["findings"][0]
        assert finding["strangler_id"] == "DR-210"
        assert set(finding["unplanned"]) == {"list", "draft", "compose"}
        assert finding["shipped"] == ["send"]

    def test_docs_decisions_doc_with_no_fence_is_not_a_candidate_at_all(self, tmp_path):
        _write(
            tmp_path / "docs" / "decisions" / "DR-999-legacy-strangler.md",
            "---\ntitle: legacy strangler\n---\n\n# DR-999 — legacy strangler\n\nNo manifest here.\n",
        )

        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert manifests == []

        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)
        assert result["state"] == "clean"

    def test_docs_decisions_doc_with_fence_but_unparseable_yaml_is_indeterminate(self, tmp_path):
        _write(
            tmp_path / "docs" / "decisions" / "DR-999-broken-strangler.md",
            (
                "---\ntitle: broken strangler\n---\n\n# DR-999\n\n"
                "```yaml strangler-endpoint\n:::not valid yaml:::\n  - [unterminated\n```\n"
            ),
        )

        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert len(manifests) == 1
        assert manifests[0]["kind"] == "indeterminate"

        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)
        assert result["state"] == "indeterminate"
        assert "notice" in result

    def test_no_filename_filter_arbitrary_decisions_basename_with_fence_is_discovered(
        self, tmp_path
    ):
        _write(
            tmp_path / "docs" / "decisions" / "DR-500-totally-unrelated-name.md",
            (
                "---\ntitle: unrelated filename\n---\n\n# DR-500\n\n"
                "```yaml strangler-endpoint\n"
                "strangler_id: DR-500\n"
                "declared_verbs: [alpha]\n"
                "```\n"
            ),
        )
        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert len(manifests) == 1
        assert manifests[0]["kind"] == "manifest"
        assert manifests[0]["strangler_id"] == "DR-500"

    def test_mixed_multi_strangler_real_fixture_arbitration(self, tmp_path):
        _seed_dr210_fixture(tmp_path)
        _seed_memo_tool_rebuild_plan(tmp_path)
        _write(
            tmp_path / "docs" / "decisions" / "DR-000-unrelated-no-fence.md",
            "---\ntitle: unrelated\n---\n\n# DR-000\n\nNothing strangler-shaped here at all.\n",
        )
        _write(
            tmp_path / "docs" / "decisions" / "DR-777-broken.md",
            (
                "---\ntitle: broken\n---\n\n# DR-777\n\n"
                "```yaml strangler-endpoint\n:::not valid yaml:::\n  - [unterminated\n```\n"
            ),
        )
        _write(
            tmp_path / "docs" / "decisions" / "DR-strang-99-other-strangler.md",
            (
                "---\ntitle: other strangler\n---\n\n# strang-99\n\n"
                "```yaml strangler-endpoint\n"
                "strangler_id: strang-99\n"
                "declared_verbs: [alpha, beta]\n"
                "```\n"
            ),
        )

        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert len(manifests) == 3
        kinds = sorted(m["kind"] for m in manifests)
        assert kinds == ["indeterminate", "manifest", "manifest"]

        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)

        assert result["state"] == "partial_strangles_found"
        strangler_ids = {f["strangler_id"] for f in result["findings"]}
        assert "strang-99" in strangler_ids
        assert "DR-210" not in strangler_ids
        assert "notices" in result
        assert any(n["path"].endswith("DR-777-broken.md") for n in result["notices"])
        assert not any(n["path"].endswith("DR-000-unrelated-no-fence.md") for n in result["notices"])

    def test_manifest_cannot_live_in_docs_plans_by_construction(self, tmp_path):
        _write(
            tmp_path / "docs" / "plans" / "2026-07-21-strang-manifest-in-plan.md",
            (
                "---\ntitle: plan-hosted manifest\n---\n\n# strang-plan-hosted\n\n"
                "```yaml strangler-endpoint\n"
                "strangler_id: strang-plan-hosted\n"
                "declared_verbs: [gamma, delta]\n"
                "```\n"
            ),
        )
        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert manifests == []

        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)
        assert result["state"] == "clean"

    def test_no_docs_dirs_at_all_is_clean_no_crash(self, tmp_path):
        manifests, _scan_errors = _scan_manifest_candidates(tmp_path)
        assert manifests == []
        shipped_check = _make_shipped_check(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        result = classify_partial_strangles(manifests, shipped_check, planned_check)
        assert result["state"] == "clean"


class TestMakeShippedCheck:
    def test_true_when_path_exists_relative_to_repo_root(self, tmp_path):
        _write(tmp_path / "coordinator_core" / "ops" / "fleet" / "memo_send.py", "# x\n")
        shipped_check = _make_shipped_check(tmp_path)
        assert shipped_check("coordinator_core/ops/fleet/memo_send.py") is True

    def test_false_when_path_absent(self, tmp_path):
        shipped_check = _make_shipped_check(tmp_path)
        assert shipped_check("coordinator_core/ops/fleet/memo_send.py") is False


class TestMakePlannedCheck:
    def test_returns_covering_plan_path_when_plan_references_strangler_and_verb(self, tmp_path):
        _seed_memo_tool_rebuild_plan(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        expected = "docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md"
        assert planned_check("list", "DR-210") == expected
        assert planned_check("draft", "DR-210") == expected
        assert planned_check("compose", "DR-210") == expected

    def test_none_when_verb_absent_from_referencing_plan(self, tmp_path):
        _seed_memo_tool_rebuild_plan(tmp_path)
        planned_check = _make_planned_check(tmp_path)
        assert planned_check("nonexistent_verb", "DR-210") is None

    def test_none_when_no_plans_dir(self, tmp_path):
        planned_check = _make_planned_check(tmp_path)
        assert planned_check("list", "DR-210") is None

    def test_word_boundary_avoids_substring_false_positive(self, tmp_path):
        _write(
            tmp_path / "docs" / "plans" / "2026-07-21-unrelated.md",
            "References DR-210 and a checklist of items, but never the bare word.\n",
        )
        planned_check = _make_planned_check(tmp_path)
        assert planned_check("list", "DR-210") is None


class TestHandlerWiring:
    def test_handler_smoke_against_fixture_tree(self, tmp_path, monkeypatch):
        _seed_dr210_fixture(tmp_path)
        _seed_memo_tool_rebuild_plan(tmp_path)
        result = _handler({}, repo_root=tmp_path)
        assert result["state"] == "clean"

    def test_handler_falls_back_to_cwd_when_repo_root_is_none(self, tmp_path, monkeypatch):
        _seed_dr210_fixture(tmp_path)
        _seed_memo_tool_rebuild_plan(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = _handler({}, repo_root=None)
        assert result["state"] == "clean"

    def test_handler_flags_unplanned_without_plan(self, tmp_path):
        _seed_dr210_fixture(tmp_path)
        result = _handler({}, repo_root=tmp_path)
        assert result["state"] == "partial_strangles_found"
        assert set(result["findings"][0]["unplanned"]) == {"list", "draft", "compose"}

    def test_handler_against_real_repo_tree_self_test(self):
        repo_root = Path(__file__).resolve().parents[3]
        result = _handler({}, repo_root=repo_root)
        assert result["state"] in ("clean", "partial_strangles_found", "indeterminate")
        finding_ids = {f["strangler_id"] for f in result.get("findings", [])}
        assert "DR-210" not in finding_ids

    def test_handler_against_real_repo_tree_emits_no_review_sidecar_or_legacy_plan_noise(self):
        repo_root = Path(__file__).resolve().parents[3]
        result = _handler({}, repo_root=repo_root)

        noisy_suffixes = (
            ".patrik-review.md",
            ".sonnet-review.md",
            ".prior-art-check.md",
            ".plan-coverage-check.md",
            ".phase0.md",
        )
        notices = result.get("notices", [])
        for notice in notices:
            assert not notice["path"].endswith(noisy_suffixes), notice["path"]
        assert len(notices) == 0
        assert result["state"] != "indeterminate"


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_decisions_dir_degrades_not_silently_clean(tmp_path):
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    (decisions_dir / "DR-000-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = decisions_dir.stat().st_mode
    os.chmod(decisions_dir, 0o000)
    try:
        manifests, scan_errors = _scan_manifest_candidates(tmp_path)
    finally:
        os.chmod(decisions_dir, original_mode)

    assert manifests == []
    assert scan_errors, "scan_errors must be non-empty when docs/decisions/ cannot be listed"
    assert any(str(decisions_dir) in e for e in scan_errors)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_handler_flags_scan_incomplete_on_unreadable_decisions_dir(tmp_path):
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    (decisions_dir / "DR-000-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = decisions_dir.stat().st_mode
    os.chmod(decisions_dir, 0o000)
    try:
        result = _handler({}, repo_root=tmp_path)
    finally:
        os.chmod(decisions_dir, original_mode)

    assert result["scan_incomplete"] is True, (
        "scan_incomplete must be True when docs/decisions/ cannot be scanned — "
        f"got {result.get('scan_incomplete')!r}"
    )
    assert result["scan_errors"], "scan_errors must be non-empty on an unscannable docs/decisions/"


def test_handler_scan_incomplete_false_on_clean_scan(tmp_path):
    result = _handler({}, repo_root=tmp_path)
    assert result["scan_incomplete"] is False
    assert result["scan_errors"] == []


# a confident UNPLANNED finding instead of an indeterminate one. Mirrors the


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_plans_dir_reports_scan_error_via_make_planned_check(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "2026-07-21-unreachable.md").write_text("DR-210 list\n", encoding="utf-8")

    original_mode = plans_dir.stat().st_mode
    os.chmod(plans_dir, 0o000)
    try:
        scan_errors: list = []
        planned_check = _make_planned_check(tmp_path, scan_errors)
        result = planned_check("list", "DR-210")
    finally:
        os.chmod(plans_dir, original_mode)

    assert result is None
    assert scan_errors, "scan_errors must be non-empty when docs/plans/ cannot be listed"
    assert any(str(plans_dir) in e for e in scan_errors)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_handler_flags_scan_incomplete_on_unreadable_plans_dir(tmp_path):
    _seed_dr210_fixture(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    original_mode = plans_dir.stat().st_mode
    os.chmod(plans_dir, 0o000)
    try:
        result = _handler({}, repo_root=tmp_path)
    finally:
        os.chmod(plans_dir, original_mode)

    assert result["scan_incomplete"] is True, (
        "scan_incomplete must be True when docs/plans/ cannot be scanned — "
        f"got {result.get('scan_incomplete')!r}"
    )
    assert result["scan_errors"], "scan_errors must be non-empty on an unscannable docs/plans/"
