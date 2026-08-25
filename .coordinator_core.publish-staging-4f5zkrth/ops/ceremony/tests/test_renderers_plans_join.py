"""
coordinator_core.ops.ceremony.tests.test_renderers_plans_join — coverage for the
``docs/plans/*.md`` <-> ``state/handoffs/*.md`` join added to
``coordinator_core.ops.ceremony.renderers`` (``_join_plans_to_handoffs``,
``render_plans_index_markdown``).

``render_repo_section`` (the tracker's compact remainder-pointer consumer of this
join) was removed 2026-08-14 along with the handoff-tracker render path -- see
``docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md``
§ C2. Coverage that existed solely to exercise the tracker's own rendered text
was removed with it; join/index coverage that did not depend on the tracker
survives unchanged.

Design source: state/handoffs/2026-07-25_000921_slate-tracker-and-registry-sync.md
(EM design calls override the baton's own Item B menu — see that handoff for
the "why" behind the compact-pointer-in-tracker / full-index-in-INDEX.md split,
and why the hand-authored docs/plans/README.md is not a generator target).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C8b
"""

from __future__ import annotations

import os
import re

from pathlib import Path

from coordinator_core.ops.ceremony.renderers import (
    _PLAN_SIDECAR_SUFFIXES,
    PLANS_INDEX_DIR,
    _collect_plans_with_parse_errors,
    _is_plan_sidecar,
    _join_plans_to_handoffs,
    _plans_index_marker,
    _unlinked_reason,
    render_plans_index_markdown,
)

_MD_LINK_TARGET_RE = re.compile(r"\]\(([^)]+)\)")


def _write_handoff(root: Path, name: str, *, created: str, deployment_state: str = "in_flight") -> None:
    handoffs_dir = root / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / name).write_text(
        f"---\ncreated: {created}\ndeployment_state: {deployment_state}\n---\nBody.\n",
        encoding="utf-8",
    )


def _write_plan(root: Path, name: str, body: str) -> None:
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / name).write_text(body, encoding="utf-8")


class TestUnlinkedRemainder:
    def test_unlinked_plan_lands_in_index_unlinked_section_not_linked(self, tmp_path: Path):
        _write_plan(
            tmp_path,
            "2026-07-19-orphan-plan.md",
            "---\ntitle: Orphan\nstatus: draft\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)

        unlinked_section = index.split("## Unlinked", 1)[1]
        assert "2026-07-19-orphan-plan.md" in unlinked_section
        linked_section = index.split("## Linked", 1)[1].split("## Unlinked", 1)[0]
        assert "2026-07-19-orphan-plan.md" not in linked_section

    def test_index_distinguishes_dangling_predecessor_from_absent_one(self, tmp_path: Path):
        _write_plan(
            tmp_path,
            "2026-07-19-dangling-plan.md",
            "---\ntitle: Dangling\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/does-not-exist.md\n---\nBody.\n",
        )
        _write_plan(
            tmp_path,
            "2026-07-19-orphan-plan.md",
            "---\ntitle: Orphan\nstatus: draft\n---\nBody.\n",
        )

        unlinked_section = render_plans_index_markdown(tmp_path).split("## Unlinked", 1)[1]
        dangling_row = next(
            ln for ln in unlinked_section.splitlines() if "2026-07-19-dangling-plan.md" in ln
        )
        orphan_row = next(
            ln for ln in unlinked_section.splitlines() if "2026-07-19-orphan-plan.md" in ln
        )

        assert "dangling" in dangling_row
        assert "state/handoffs/does-not-exist.md" in dangling_row
        assert "no `predecessor_handoff:` declared" in orphan_row
        assert "dangling" not in orphan_row

class TestSidecarExclusion:
    def test_all_nine_sidecar_suffixes_excluded(self, tmp_path: Path):
        assert len(_PLAN_SIDECAR_SUFFIXES) == 9
        _write_plan(tmp_path, "2026-07-19-base-plan.md", "---\nstatus: draft\n---\nBody.\n")
        for suffix in _PLAN_SIDECAR_SUFFIXES:
            _write_plan(
                tmp_path,
                f"2026-07-19-base-plan{suffix}.md",
                "---\nstatus: draft\n---\nSidecar body.\n",
            )

        records = _collect_plans_with_parse_errors(tmp_path)

        assert len(records) == 1
        assert records[0]["path"].endswith("2026-07-19-base-plan.md")


    def test_directory_index_files_are_not_counted_as_plans(self, tmp_path: Path):
        _write_plan(tmp_path, "2026-07-19-real-plan.md", "---\nstatus: draft\n---\nBody.\n")
        _write_plan(tmp_path, "INDEX.md", "# Plans Index\n")
        _write_plan(tmp_path, "README.md", "# Slate index\n")

        records = _collect_plans_with_parse_errors(tmp_path)

        # The generated index must not count itself as a plan.
        assert [Path(r["path"]).name for r in records] == ["2026-07-19-real-plan.md"]


def _write_archived_handoff(root: Path, rel_dir: str, name: str, *, created: str) -> str:
    """Write a handoff under ``archive/handoffs/<rel_dir>/<name>`` (``rel_dir``
    may be ``""`` for the flat layout). Returns the repo-relative path."""
    archive_dir = root / "archive" / "handoffs" / rel_dir if rel_dir else root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(
        f"---\ncreated: {created}\ndeployment_state: consumed\n---\nBody.\n",
        encoding="utf-8",
    )
    return "/".join(p for p in ("archive", "handoffs", rel_dir, name) if p)


class TestArchivedResolution:
    def test_flat_archive_target_resolves_archived(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "", "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-flat-archived-plan.md",
            "---\ntitle: Flat\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/target.md\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)

        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]
        assert "2026-07-19-flat-archived-plan.md" in archived_section
        assert archived_path in archived_section
        # Not double-counted as gone/unlinked.
        unlinked_section = index.split("## Unlinked", 1)[1]
        assert "2026-07-19-flat-archived-plan.md" not in unlinked_section

    def test_month_subfoldered_archive_target_resolves_archived(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "2026-07", "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-subfoldered-plan.md",
            "---\ntitle: Subfoldered\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/target.md\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)

        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]
        assert "2026-07-19-subfoldered-plan.md" in archived_section
        assert archived_path in archived_section

    def test_bare_basename_predecessor_resolves(self, tmp_path: Path):
        _write_archived_handoff(tmp_path, "2026-07", "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-bare-basename-plan.md",
            "---\ntitle: Bare\nstatus: draft\n"
            "predecessor_handoff: target.md\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)

        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]
        assert "2026-07-19-bare-basename-plan.md" in archived_section

    def test_value_already_pointing_at_archive_resolves(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "2026-07", "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-already-archive-plan.md",
            f"---\ntitle: Already\nstatus: draft\n"
            f"predecessor_handoff: {archived_path}\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)

        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]
        assert "2026-07-19-already-archive-plan.md" in archived_section

    def test_live_target_still_wins_over_same_basename_in_archive(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md", created="2026-01-01")
        _write_archived_handoff(tmp_path, "", "target.md", created="2025-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-live-plan.md",
            "---\ntitle: Live\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/target.md\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        j = joined[0]
        assert j["resolution_state"] == "live"
        assert j["resolved_handoff_path"] == "state/handoffs/target.md"

    def test_tracker_and_index_counts_reconcile(self, tmp_path: Path):
        _write_handoff(tmp_path, "live-target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-live-plan.md",
            "---\nstatus: draft\npredecessor_handoff: state/handoffs/live-target.md\n---\nBody.\n",
        )
        _write_archived_handoff(tmp_path, "", "archived-target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-archived-plan.md",
            "---\nstatus: draft\npredecessor_handoff: state/handoffs/archived-target.md\n---\nBody.\n",
        )
        _write_plan(
            tmp_path,
            "2026-07-19-gone-plan.md",
            "---\nstatus: draft\npredecessor_handoff: state/handoffs/deleted.md\n---\nBody.\n",
        )
        _write_plan(
            tmp_path,
            "2026-07-19-absent-plan.md",
            "---\nstatus: draft\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)

        linked_section = index.split("## Linked", 1)[1].split("## Archived", 1)[0]
        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]
        unlinked_section = index.split("## Unlinked", 1)[1]

        # Each linked/archived row carries two links (plan + resolved
        # handoff); unlinked rows carry one (plan only, no resolvable target).
        assert linked_section.count(".md]") == 2
        assert archived_section.count(".md]") == 2
        assert unlinked_section.count(".md]") == 2


class TestIndexLinksResolveFromOwnDirectory:
    """Regression for the doubled-prefix link bug: ``INDEX.md`` lives AT
    ``docs/plans/INDEX.md``, so a link target must resolve relative to
    ``docs/plans/``, not repo root. A raw repo-root-relative target (the old
    behavior) would make ``docs/plans/foo.md`` resolve to
    ``docs/plans/docs/plans/foo.md`` and ``state/handoffs/bar.md`` resolve to
    ``docs/plans/state/handoffs/bar.md`` — both dead links."""

    def test_plan_and_handoff_link_targets_resolve_from_docs_plans(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-example-plan.md",
            "---\ntitle: Example\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/target.md\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)
        linked_section = index.split("## Linked", 1)[1].split("## Archived", 1)[0]

        import re

        targets = re.findall(r"\]\(([^)]+)\)", linked_section)
        assert targets, "expected at least one markdown link in the Linked section"

        for target in targets:
            assert not target.startswith("docs/plans/"), target
            assert not target.startswith("state/"), target
            resolved = (tmp_path / "docs" / "plans" / target).resolve()
            assert resolved.is_file(), f"{target} does not resolve to a real file from docs/plans/"

        # The handoff link specifically must climb out of docs/plans/ (two
        # levels: docs/plans -> docs -> repo root -> state/handoffs).
        handoff_target = next(t for t in targets if t.endswith("target.md") and "example-plan" not in t)
        assert handoff_target.startswith("../../state/handoffs/"), handoff_target

    def test_archived_handoff_link_target_resolves_from_docs_plans(self, tmp_path: Path):
        _write_archived_handoff(tmp_path, "", "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-flat-archived-plan.md",
            "---\ntitle: Flat\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/target.md\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)
        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]

        import re

        targets = re.findall(r"\]\(([^)]+)\)", archived_section)
        assert targets

        for target in targets:
            resolved = (tmp_path / "docs" / "plans" / target).resolve()
            assert resolved.is_file(), f"{target} does not resolve to a real file from docs/plans/"

        archive_target = next(t for t in targets if "archive/handoffs" in t)
        assert archive_target.startswith("../../archive/handoffs/"), archive_target


class TestParseErrorStub:
    def test_unparseable_plan_frontmatter_surfaces_stub_not_dropped(self, tmp_path: Path):
        _write_plan(tmp_path, "2026-07-19-broken-plan.md", "no frontmatter here at all\n")

        records = _collect_plans_with_parse_errors(tmp_path)

        assert len(records) == 1
        assert records[0]["frontmatter"] is None
        assert records[0]["path"].endswith("2026-07-19-broken-plan.md")

        # A parse-error plan has no predecessor_handoff -> counts as unlinked,
        # not silently dropped from the rendered index.
        index = render_plans_index_markdown(tmp_path)
        assert "2026-07-19-broken-plan.md" in index.split("## Unlinked", 1)[1]


# ---------------------------------------------------------------------------
# staff-eng review (2026-08-06) F3 — pinning tests for previously-untested
# behavior: the structural D1 sidecar rule, the D2 deliverable_id join
# (including F1's ambiguity handling), _unlinked_reason, and the D4 marker.
# ---------------------------------------------------------------------------


def _write_handoff_with_deliverable(
    root: Path,
    name: str,
    *,
    created: str,
    deliverable_id: str,
    deployment_state: str = "in_flight",
) -> str:
    """Write a LIVE handoff declaring ``deliverable_id``. Returns the
    repo-relative path."""
    handoffs_dir = root / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / name).write_text(
        f"---\ncreated: {created}\ndeployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n---\nBody.\n",
        encoding="utf-8",
    )
    return f"state/handoffs/{name}"


def _write_archived_handoff_with_deliverable(
    root: Path, name: str, *, created: str, deliverable_id: str
) -> str:
    """Write an ARCHIVED (flat-layout) handoff declaring ``deliverable_id``.
    Returns the repo-relative path."""
    archive_dir = root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(
        f"---\ncreated: {created}\ndeployment_state: consumed\n"
        f"deliverable_id: {deliverable_id}\n---\nBody.\n",
        encoding="utf-8",
    )
    return f"archive/handoffs/{name}"


class TestIsPlanSidecarStructuralRule:
    """D1 — pins ``_is_plan_sidecar``'s structural (dot-prefix) rule
    independently of the exact-suffix allowlist."""

    def test_companion_file_with_base_present_is_a_sidecar(self, tmp_path: Path):
        plans_dir = tmp_path / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "foo.md").write_text("base\n", encoding="utf-8")
        (plans_dir / "foo.bar.md").write_text("companion\n", encoding="utf-8")

        existing = frozenset({"foo.md", "foo.bar.md"})
        assert _is_plan_sidecar("foo.bar.md", existing) is True
        assert _is_plan_sidecar("foo.md", existing) is False

    def test_companion_file_with_base_absent_is_a_plan(self, tmp_path: Path):
        # Pins the deliberate asymmetry: the structural rule only fires when
        # the base plan actually exists on disk.
        existing = frozenset({"foo.bar.md"})
        assert _is_plan_sidecar("foo.bar.md", existing) is False

    def test_allowlisted_suffix_is_sidecar_even_without_base(self, tmp_path: Path):
        # Pins the retained allowlist independently of the structural rule —
        # test_all_nine_sidecar_suffixes_excluded (above) now passes for two
        # independent reasons and no longer discriminates the allowlist on
        # its own.
        existing = frozenset({"foo.review.md"})
        assert _is_plan_sidecar("foo.review.md", existing) is True

    def test_literal_reported_defect_timestamped_coverage_check(self, tmp_path: Path):
        existing = frozenset(
            {"foo.md", "foo.plan-coverage-check.2026-07-01T08-31-09Z.md"}
        )
        assert (
            _is_plan_sidecar("foo.plan-coverage-check.2026-07-01T08-31-09Z.md", existing)
            is True
        )

    def test_mid_length_prefix_match_is_a_sidecar(self, tmp_path: Path):
        # Only foo.a.md exists; foo.a.b.c.md's stem token-prefix "foo.a"
        # matches it, so it is a sidecar via the structural rule even though
        # neither "foo" nor "foo.a.b" exist as files.
        existing = frozenset({"foo.a.md", "foo.a.b.c.md"})
        assert _is_plan_sidecar("foo.a.b.c.md", existing) is True

    def test_directory_does_not_seed_existing_names(self, tmp_path: Path):
        # A directory literally named "alpha.md" must not count as an
        # existing FILE for the structural rule's purposes —
        # _collect_plans_with_parse_errors's existing_names is filtered to
        # is_file() entries specifically so this can't happen. Pin the two
        # halves separately: (1) the collector's real plan (alpha.beta.md)
        # keeps its parsed frontmatter, i.e. is NOT reclassified as a
        # sidecar of the directory; (2) in isolation, _is_plan_sidecar WOULD
        # misclassify it if fed a directory-seeded existing_names set,
        # confirming the collector's is_file() filter is load-bearing, not
        # incidental.
        _write_plan(tmp_path, "alpha.beta.md", "---\nstatus: draft\n---\nBody.\n")
        (tmp_path / "docs" / "plans" / "alpha.md").mkdir()

        records = {Path(r["path"]).name: r for r in _collect_plans_with_parse_errors(tmp_path)}

        assert records["alpha.beta.md"]["frontmatter"] is not None
        assert records["alpha.beta.md"]["frontmatter"].get("status") == "draft"

        assert _is_plan_sidecar("alpha.beta.md", frozenset({"alpha.md"})) is True


class TestDeliverableIdJoin:
    """D2 — pins ``_join_plans_to_handoffs``'s ``deliverable_id`` edge,
    including F1's ambiguity handling."""

    def test_single_live_match_resolves_linked(self, tmp_path: Path):
        _write_handoff_with_deliverable(
            tmp_path, "h1.md", created="2026-01-01", deliverable_id="dlv-x"
        )
        _write_plan(
            tmp_path,
            "2026-07-19-p1.md",
            "---\nstatus: draft\ndeliverable_id: dlv-x\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        assert len(joined) == 1
        j = joined[0]
        assert j["resolution_state"] == "live"
        assert j["resolution_method"] == "deliverable_id"
        assert j["resolved_handoff_path"] == "state/handoffs/h1.md"
        assert j["ambiguous_deliverable_id_count"] == 0

    def test_archived_only_match_resolves_archived(self, tmp_path: Path):
        archived_path = _write_archived_handoff_with_deliverable(
            tmp_path, "h1.md", created="2026-01-01", deliverable_id="dlv-x"
        )
        _write_plan(
            tmp_path,
            "2026-07-19-p1.md",
            "---\nstatus: draft\ndeliverable_id: dlv-x\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        j = joined[0]
        assert j["resolution_state"] == "archived"
        assert j["archived_handoff_path"] == archived_path
        assert j["resolved_handoff_path"] is None
        assert j["resolution_method"] == "deliverable_id"

    def test_three_way_ambiguous_match_is_unlinked_not_silently_picked(self, tmp_path: Path):
        # Non-negotiable per F1: 3 live handoffs sharing one deliverable_id
        # must NOT collapse to a single arbitrary edge.
        for i in range(3):
            _write_handoff_with_deliverable(
                tmp_path, f"h{i}.md", created="2026-01-01", deliverable_id="dlv-x"
            )
        _write_plan(
            tmp_path,
            "2026-07-19-p1.md",
            "---\nstatus: draft\ndeliverable_id: dlv-x\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        j = joined[0]
        assert j["resolution_state"] == "gone"
        assert j["resolution_method"] is None
        assert j["ambiguous_deliverable_id_count"] == 3
        assert _unlinked_reason(j) == "deliverable_id `dlv-x` is ambiguous across 3 handoffs"

    def test_live_beats_archived_on_same_id(self, tmp_path: Path):
        _write_handoff_with_deliverable(
            tmp_path, "h-live.md", created="2026-01-01", deliverable_id="dlv-x"
        )
        _write_archived_handoff_with_deliverable(
            tmp_path, "h-archived.md", created="2025-01-01", deliverable_id="dlv-x"
        )
        _write_plan(
            tmp_path,
            "2026-07-19-p1.md",
            "---\nstatus: draft\ndeliverable_id: dlv-x\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        j = joined[0]
        assert j["resolution_state"] == "live"
        assert j["resolved_handoff_path"] == "state/handoffs/h-live.md"
        assert j["ambiguous_deliverable_id_count"] == 0

    def test_resolvable_predecessor_beats_different_target_deliverable_id(self, tmp_path: Path):
        _write_handoff(tmp_path, "predecessor-target.md", created="2026-01-01")
        _write_handoff_with_deliverable(
            tmp_path, "deliverable-target.md", created="2026-01-01", deliverable_id="dlv-x"
        )
        _write_plan(
            tmp_path,
            "2026-07-19-p1.md",
            "---\nstatus: draft\ndeliverable_id: dlv-x\n"
            "predecessor_handoff: state/handoffs/predecessor-target.md\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        j = joined[0]
        assert j["resolution_method"] == "predecessor_handoff"
        assert j["resolved_handoff_path"] == "state/handoffs/predecessor-target.md"

    def test_dangling_predecessor_falls_through_to_deliverable_id(self, tmp_path: Path):
        _write_handoff_with_deliverable(
            tmp_path, "deliverable-target.md", created="2026-01-01", deliverable_id="dlv-x"
        )
        _write_plan(
            tmp_path,
            "2026-07-19-p1.md",
            "---\nstatus: draft\ndeliverable_id: dlv-x\n"
            "predecessor_handoff: state/handoffs/does-not-exist.md\n---\nBody.\n",
        )

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        j = joined[0]
        assert j["resolution_method"] == "deliverable_id"
        assert j["resolution_state"] != "gone"
        assert j["resolved_handoff_path"] == "state/handoffs/deliverable-target.md"

    def test_null_absent_empty_and_literal_null_deliverable_id_attempt_no_join(
        self, tmp_path: Path
    ):
        _write_handoff_with_deliverable(
            tmp_path, "h1.md", created="2026-01-01", deliverable_id="null"
        )
        for i, body in enumerate(
            [
                "---\nstatus: draft\ndeliverable_id: null\n---\nBody.\n",
                "---\nstatus: draft\n---\nBody.\n",
                '---\nstatus: draft\ndeliverable_id: ""\n---\nBody.\n',
                '---\nstatus: draft\ndeliverable_id: "null"\n---\nBody.\n',
            ]
        ):
            _write_plan(tmp_path, f"2026-07-19-p{i}.md", body)

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)

        assert len(joined) == 4
        for j in joined:
            assert j["resolution_method"] is None
            assert j["resolution_state"] == "gone"
            assert j["ambiguous_deliverable_id_count"] == 0


class TestUnlinkedReasonRegression:
    def test_nothing_declared_keeps_original_exact_string(self, tmp_path: Path):
        j = {
            "declared_predecessor": None,
            "declared_deliverable_id": None,
            "ambiguous_deliverable_id_count": 0,
        }
        assert _unlinked_reason(j) == "no `predecessor_handoff:` declared"

    def test_dangling_predecessor_and_unmatched_deliverable_id_both_clauses_in_order(
        self, tmp_path: Path
    ):
        j = {
            "declared_predecessor": "state/handoffs/gone.md",
            "declared_deliverable_id": "dlv-y",
            "ambiguous_deliverable_id_count": 0,
        }
        reason = _unlinked_reason(j)
        assert reason == (
            "dangling — `state/handoffs/gone.md` no longer exists on disk "
            "under state/handoffs/ or archive/handoffs/ (git history is the "
            "only remaining trace); deliverable_id `dlv-y` matches no handoff"
        )


class TestPlansIndexMarker:
    def test_no_provenance_supplied_uses_unknown_defaults(self):
        marker = _plans_index_marker(None, None)
        assert "(source: unknown | generated: unknown)" in marker

    def test_both_supplied_are_verbatim_and_deterministic(self):
        marker_a = _plans_index_marker("abc1234", "2026-08-06T00:00:00Z")
        marker_b = _plans_index_marker("abc1234", "2026-08-06T00:00:00Z")
        assert "(source: abc1234 | generated: 2026-08-06T00:00:00Z)" in marker_a
        assert marker_a == marker_b


class TestPlansIndexLinkTargetsResolveOnDisk:
    """AC: every emitted link target must RESOLVE ON DISK relative to
    docs/plans/INDEX.md's own directory — the generating rule Defect 1 in
    cross-repo/inbox/2026-08-06-market-intelligence-em-update-docs-distill-
    ceremony-defects.md reported broken (markdown resolves a relative target
    against the CONTAINING FILE's directory, not repo root)."""

    def test_linked_and_unlinked_and_archived_link_targets_resolve(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md", created="2026-01-01")
        _write_plan(
            tmp_path,
            "2026-07-19-linked-plan.md",
            "---\ntitle: Linked\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/target.md\n---\nBody.\n",
        )
        _write_plan(
            tmp_path,
            "2026-07-19-orphan-plan.md",
            "---\ntitle: Orphan\nstatus: draft\n---\nBody.\n",
        )
        archived_path = _write_archived_handoff(tmp_path, "", "archived.md", created="2026-01-02")
        _write_plan(
            tmp_path,
            "2026-07-19-archived-plan.md",
            "---\ntitle: Archived\nstatus: draft\n"
            "predecessor_handoff: state/handoffs/archived.md\n---\nBody.\n",
        )

        index = render_plans_index_markdown(tmp_path)
        out_dir = tmp_path / PLANS_INDEX_DIR
        targets = _MD_LINK_TARGET_RE.findall(index)
        assert targets, "expected at least one markdown link target in the rendered index"

        checked_archived = False
        for target in targets:
            resolved = os.path.normpath(str(out_dir / target))
            assert os.path.isfile(resolved), f"{target!r} did not resolve on disk from {PLANS_INDEX_DIR}"
            if resolved == os.path.normpath(str(tmp_path / archived_path)):
                checked_archived = True
        assert checked_archived
