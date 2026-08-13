"""
coordinator_core.ops.ceremony.tests.test_renderers_handoff_lineage — coverage
for the handoff -> handoff lineage join (``predecessor``,
``additional_predecessors``, ``origin_handoff``, ``forked_from``) added to
``coordinator_core.ops.ceremony.renderers`` (``_join_handoff_lineage``,
``render_repo_section``'s Handoff Lineage remainder pointer,
``render_handoff_lineage_markdown``).

Design source: state/handoffs/2026-07-25_005016_handoff-lineage-visibility.md
— extends the plans<->handoffs join added by e9f4928e/914f99eb (see
test_renderers_plans_join.py's TestArchivedResolution, the pattern this
mirrors). ``gate_dependency`` is deliberately excluded from this join (D1 —
free text, not a path); no test here exercises it as a resolvable field.

``_HANDOFF_LINEAGE_FIELDS`` is derived from ``coordinator_core.dag.
EDGE_KIND_META`` (round-2 code review, 2026-07-25) rather than hand-restated,
so field ORDER in the rendered file follows ``EDGE_KIND_META``'s dict order,
not a fixed literal sequence. Section-boundary assertions below use
``_section()`` (regex up to the next ``## `` header or end-of-string) rather
than ``str.split`` against an assumed adjacent header, so they stay correct
regardless of field order or a future ``EDGE_KIND_META`` addition.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C8b
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# Real-git spawn is load-bearing: several fixtures here build an actual repo
# and drive `build_git_history_cache` / `_GitHistoryCacheProvider` against it
# to assert lineage joins against real commit history, not a mocked log --
# no mock stands in for the ancestry-walk behaviour under test. Per-test
# repo fixtures (not hoisted) since several tests mutate history.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core import dag as dag_module
from coordinator_core.dag import EDGE_KIND_META, build_git_history_cache
from coordinator_core.ops.ceremony import renderers as renderers_module
from coordinator_core.ops.ceremony.renderers import (
    HANDOFF_LINEAGE_GENERATED_MARKER,
    _classify_handoff_location,
    _GitHistoryCacheProvider,
    _join_handoff_lineage,
    _join_plans_to_handoffs,
    _normalize_lineage_value,
    render_handoff_lineage_markdown,
    render_repo_section,
)


def _section(markdown: str, header: str) -> str:
    """Extract the body of one ``## <header>`` section from a rendered
    lineage/index doc — everything up to the next ``## `` header or
    end-of-string. Order-independent, unlike a chained ``str.split`` against
    an assumed adjacent header name (which silently merges an intervening
    section's rows into the wrong count the moment section order changes —
    see the module docstring)."""
    match = re.search(
        rf"^## {re.escape(header)}\n(.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"section '## {header}' not found in:\n{markdown}"
    return match.group(1)


def _write_handoff(root: Path, name: str, *, extra_frontmatter: str = "", created: str = "2026-01-01") -> None:
    handoffs_dir = root / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / name).write_text(
        f"---\ncreated: {created}\ndeployment_state: in_flight\n{extra_frontmatter}---\nBody.\n",
        encoding="utf-8",
    )


def _write_archived_handoff(root: Path, rel_dir: str, name: str) -> str:
    archive_dir = root / "archive" / "handoffs" / rel_dir if rel_dir else root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(
        "---\ncreated: 2026-01-01\ndeployment_state: consumed\n---\nBody.\n",
        encoding="utf-8",
    )
    return "/".join(p for p in ("archive", "handoffs", rel_dir, name) if p)


def _write_non_handoff(root: Path, rel_path: str) -> None:
    fpath = root / rel_path
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("# Not a handoff\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tier-3 (git-history) fixtures — mirrors coordinator_core/tests/
# test_dag_resolve_target_tier3_dedup.py's real-repo helper convention:
# _resolve_candidate_path's tier-3 fall-through delegates to dag.resolve_target,
# whose contract is defined in terms of actual `git log --all` behaviour, not
# a mockable interface, so these tests need a genuine git repo, not tmp_path
# used as a bare directory (every other fixture in this module deliberately
# does NOT git-init tmp_path, since tiers 1/2 need no git at all).
# ---------------------------------------------------------------------------

def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)


def _commit_path(root: Path, rel_path: str) -> None:
    subprocess.run(["git", "add", "--", rel_path], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"commit {rel_path}"],
        cwd=root, check=True,
    )


def _prune_handoff(root: Path, name: str) -> None:
    """Write ``state/handoffs/<name>``, commit it, then delete it and commit
    the deletion — leaves it disk-absent but git-history-tracked (the
    "pruned but recoverable" case tier 3 exists to surface)."""
    rel_path = f"state/handoffs/{name}"
    _write_handoff(root, name)
    _commit_path(root, rel_path)
    (root / rel_path).unlink()
    _commit_path(root, rel_path)


class TestNormalizeLineageValue:
    def test_none_sentinel_word_is_not_declared(self):
        assert _normalize_lineage_value("none") is None
        assert _normalize_lineage_value("None") is None
        assert _normalize_lineage_value("NONE") is None

    def test_yaml_null_already_python_none(self):
        assert _normalize_lineage_value(None) is None

    def test_tilde_and_null_word_sentinels(self):
        assert _normalize_lineage_value("~") is None
        assert _normalize_lineage_value("null") is None

    def test_real_value_passes_through(self):
        assert _normalize_lineage_value("state/handoffs/target.md") == "state/handoffs/target.md"

    def test_no_inline_comment_stripping_at_this_layer(self):
        """Comment-stripping is the upstream parser's job
        (``coordinator_core.dag._strip_inline_comment``, quote-aware) — by
        the time a value reaches ``_normalize_lineage_value`` a real trailing
        comment is already gone, so this function must NOT do a second,
        non-quote-aware strip of its own. A literal `` #`` substring that
        survived upstream parsing (e.g. embedded in an already-unquoted
        value) is real path content here, not a comment, and must pass
        through unchanged."""
        assert (
            _normalize_lineage_value("state/handoffs/target.md  # canonical path")
            == "state/handoffs/target.md  # canonical path"
        )

    def test_empty_string_is_not_declared(self):
        assert _normalize_lineage_value("") is None
        assert _normalize_lineage_value("   ") is None


class TestEachFieldLiveArchivedGone:
    def test_predecessor_resolves_live(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md")
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: target.md\n")

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "live"
        assert child["fields"]["predecessor"]["resolved_path"] == "state/handoffs/target.md"

    def test_origin_handoff_resolves_archived(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "", "origin.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter=f"origin_handoff: {archived_path}\n",
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["origin_handoff"]["resolution_state"] == "archived"
        assert child["fields"]["origin_handoff"]["archived_path"] == archived_path

    def test_forked_from_resolves_gone(self, tmp_path: Path):
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter="forked_from: state/handoffs/does-not-exist.md\n",
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["forked_from"]["resolution_state"] == "gone"
        assert child["fields"]["forked_from"]["declared"] == "state/handoffs/does-not-exist.md"

    def test_undeclared_field_has_no_declared_value(self, tmp_path: Path):
        _write_handoff(tmp_path, "solo.md")

        joined = _join_handoff_lineage(tmp_path)
        solo = next(r for r in joined if r["path"].endswith("solo.md"))

        for field in ("predecessor", "origin_handoff", "forked_from"):
            assert solo["fields"][field]["declared"] is None


class TestBothPathShapes:
    def test_bare_basename_and_repo_relative_in_same_field(self, tmp_path: Path):
        _write_handoff(tmp_path, "bare-target.md")
        archived_path = _write_archived_handoff(tmp_path, "2026-07", "archived-target.md")
        _write_handoff(tmp_path, "bare-child.md", extra_frontmatter="predecessor: bare-target.md\n")
        _write_handoff(
            tmp_path, "path-child.md",
            extra_frontmatter=f"predecessor: {archived_path}\n",
        )

        joined = _join_handoff_lineage(tmp_path)
        bare_child = next(r for r in joined if r["path"].endswith("bare-child.md"))
        path_child = next(r for r in joined if r["path"].endswith("path-child.md"))

        assert bare_child["fields"]["predecessor"]["resolution_state"] == "live"
        assert path_child["fields"]["predecessor"]["resolution_state"] == "archived"
        assert path_child["fields"]["predecessor"]["archived_path"] == archived_path


class TestArchivedTreeShapes:
    def test_month_subfoldered_archive_resolves(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "2026-07", "target.md")
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: target.md\n")

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "archived"
        assert child["fields"]["predecessor"]["archived_path"] == archived_path

    def test_flat_archive_resolves(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "", "target.md")
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: target.md\n")

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["archived_path"] == archived_path


class TestInvalidTarget:
    def test_target_outside_both_trees_is_invalid_not_gone(self, tmp_path: Path):
        _write_non_handoff(tmp_path, "docs/problems/2026-07-02-some-problem.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter="forked_from: docs/problems/2026-07-02-some-problem.md\n",
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["forked_from"]["resolution_state"] == "invalid-target"
        assert child["fields"]["forked_from"]["resolved_path"] is None
        assert child["fields"]["forked_from"]["archived_path"] is None

    def test_classify_handoff_location_returns_invalid_target_directly(self, tmp_path: Path):
        target = tmp_path / "docs" / "problems" / "foo.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo\n", encoding="utf-8")

        assert _classify_handoff_location(target, tmp_path) == "invalid-target"


class TestPlansJoinNoRegression:
    """D3: extending _classify_handoff_location to return "invalid-target"
    instead of None must not change plans-join output — _join_plans_to_handoffs
    maps that state back onto its existing "gone", byte-identical to before
    the classifier extension."""

    def test_plan_predecessor_pointing_at_non_handoff_file_is_gone(self, tmp_path: Path):
        _write_non_handoff(tmp_path, "docs/problems/2026-07-02-some-problem.md")
        plans_dir = tmp_path / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "2026-07-19-example-plan.md").write_text(
            "---\ntitle: Example\nstatus: draft\n"
            "predecessor_handoff: docs/problems/2026-07-02-some-problem.md\n---\nBody.\n",
            encoding="utf-8",
        )

        from coordinator_core.ops.ceremony.renderers import _collect_plans_with_parse_errors

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(tmp_path), tmp_path)
        plan = next(j for j in joined if j["path"].endswith("2026-07-19-example-plan.md"))

        assert plan["resolution_state"] == "gone"
        assert plan["resolved_handoff_path"] is None
        assert plan["archived_handoff_path"] is None

    def test_plans_index_still_renders_invalid_target_as_unlinked(self, tmp_path: Path):
        from coordinator_core.ops.ceremony.renderers import render_plans_index_markdown

        _write_non_handoff(tmp_path, "docs/problems/2026-07-02-some-problem.md")
        plans_dir = tmp_path / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "2026-07-19-example-plan.md").write_text(
            "---\ntitle: Example\nstatus: draft\n"
            "predecessor_handoff: docs/problems/2026-07-02-some-problem.md\n---\nBody.\n",
            encoding="utf-8",
        )

        index = render_plans_index_markdown(tmp_path)
        unlinked_section = index.split("## Unlinked", 1)[1]
        linked_section = index.split("## Linked", 1)[1].split("## Archived", 1)[0]
        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]

        assert "2026-07-19-example-plan.md" in unlinked_section
        assert "2026-07-19-example-plan.md" not in linked_section
        assert "2026-07-19-example-plan.md" not in archived_section


class TestGateDependencyExcluded:
    def test_gate_dependency_never_appears_in_join_output(self, tmp_path: Path):
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter='gate_dependency: "PM revisits the enforcement-mechanism choice"\n',
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert set(child["fields"].keys()) == set(EDGE_KIND_META.keys())
        assert "gate_dependency" not in child["fields"]

    def test_gate_dependency_never_rendered(self, tmp_path: Path):
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter='gate_dependency: "PM revisits the enforcement-mechanism choice"\n',
        )

        output = render_handoff_lineage_markdown(tmp_path)

        assert "gate_dependency" not in output
        assert "PM revisits" not in output


class TestRenderHandoffLineageMarkdown:
    def test_generated_marker_present(self, tmp_path: Path):
        _write_handoff(tmp_path, "solo.md")

        output = render_handoff_lineage_markdown(tmp_path)

        assert HANDOFF_LINEAGE_GENERATED_MARKER in output
        assert "## predecessor" in output
        assert "## origin_handoff" in output
        assert "## forked_from" in output

    def test_undeclared_field_produces_no_row(self, tmp_path: Path):
        _write_handoff(tmp_path, "solo.md")

        output = render_handoff_lineage_markdown(tmp_path)
        predecessor_section = _section(output, "predecessor")

        assert "solo.md" not in predecessor_section

    def test_declared_field_produces_a_row(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md")
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: target.md\n")

        output = render_handoff_lineage_markdown(tmp_path)
        predecessor_section = _section(output, "predecessor")

        assert "child.md" in predecessor_section
        assert "live" in predecessor_section


class TestTrackerPointerAndCountReconciliation:
    def test_no_lineage_issues_omits_section_entirely(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md")
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: target.md\n")

        tracker = render_repo_section(tmp_path)

        assert "## Handoff Lineage" not in tracker

    def test_gone_and_archived_counts_reconcile_with_rendered_file(self, tmp_path: Path):
        archived_path = _write_archived_handoff(tmp_path, "", "archived-target.md")
        _write_handoff(
            tmp_path, "archived-child.md",
            extra_frontmatter=f"predecessor: {archived_path}\n",
        )
        _write_handoff(
            tmp_path, "gone-child.md",
            extra_frontmatter="origin_handoff: state/handoffs/deleted.md\n",
        )
        _write_non_handoff(tmp_path, "docs/problems/some-problem.md")
        _write_handoff(
            tmp_path, "invalid-child.md",
            extra_frontmatter="forked_from: docs/problems/some-problem.md\n",
        )

        tracker = render_repo_section(tmp_path)
        lineage_md = render_handoff_lineage_markdown(tmp_path)

        assert "## Handoff Lineage (no live tracker row)" in tracker
        assert "1 link resolves to an archived handoff" in tracker
        assert "1 link points at a target no longer on disk" in tracker
        assert "1 link points at a file that is not a handoff" in tracker
        assert "— see state/handoff-lineage.md" in tracker

        # Reconcile: exactly one row per field-table for each of the three
        # cases constructed above.
        predecessor_section = _section(lineage_md, "predecessor")
        origin_section = _section(lineage_md, "origin_handoff")
        forked_section = _section(lineage_md, "forked_from")

        def _row_count(section: str) -> int:
            return sum(1 for line in section.splitlines() if line.startswith("| ["))

        assert "archived-child.md" in predecessor_section
        assert _row_count(predecessor_section) == 1
        assert "gone-child.md" in origin_section
        assert _row_count(origin_section) == 1
        assert "invalid-child.md" in forked_section
        assert _row_count(forked_section) == 1

    def test_live_only_lineage_omits_from_pointer_but_appears_in_file(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md")
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: target.md\n")

        tracker = render_repo_section(tmp_path)
        lineage_md = render_handoff_lineage_markdown(tmp_path)

        assert "## Handoff Lineage" not in tracker
        predecessor_section = _section(lineage_md, "predecessor")
        assert "child.md" in predecessor_section
        assert "live" in predecessor_section

    def test_none_sentinel_predecessor_does_not_count_as_gone(self, tmp_path: Path, monkeypatch):
        _write_handoff(tmp_path, "child.md", extra_frontmatter="predecessor: none\n")

        spawn_count = [0]
        orig_run = dag_module.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag_module.subprocess, "run", counting_run)

        tracker = render_repo_section(tmp_path)
        lineage_md = render_handoff_lineage_markdown(tmp_path)

        assert "## Handoff Lineage" not in tracker
        predecessor_section = _section(lineage_md, "predecessor")
        assert "child.md" not in predecessor_section
        assert spawn_count[0] == 0, (
            "a sentinel value must short-circuit before _resolve_candidate_path "
            f"ever reaches tier 3, got {spawn_count[0]} git subprocess spawn(s)"
        )

    def test_null_sentinel_with_trailing_comment_does_not_count_as_gone(self, tmp_path: Path, monkeypatch):
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter="origin_handoff: null  # session opened on a cross-repo memo baton\n",
        )

        spawn_count = [0]
        orig_run = dag_module.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag_module.subprocess, "run", counting_run)

        tracker = render_repo_section(tmp_path)
        lineage_md = render_handoff_lineage_markdown(tmp_path)

        assert "## Handoff Lineage" not in tracker
        origin_section = _section(lineage_md, "origin_handoff")
        assert "child.md" not in origin_section
        assert spawn_count[0] == 0, (
            "a sentinel value must short-circuit before _resolve_candidate_path "
            f"ever reaches tier 3, got {spawn_count[0]} git subprocess spawn(s)"
        )


class TestFieldsSSOTSync:
    """The lineage field set must stay derived from ``EDGE_KIND_META`` (the
    SSOT ``coordinator_core.dag`` documents as authoritative for which
    frontmatter fields are lineage edges) rather than a second, hand-rolled
    restatement that can silently drift out of sync — that drift (missing
    ``additional_predecessors``) is exactly what the round-2 code review
    caught."""

    def test_handoff_lineage_fields_matches_edge_kind_meta_membership(self):
        from coordinator_core.ops.ceremony.renderers import _HANDOFF_LINEAGE_FIELDS

        assert set(_HANDOFF_LINEAGE_FIELDS) == set(EDGE_KIND_META.keys())

    def test_gate_dependency_is_not_an_edge_kind_meta_member(self):
        # Confirms D1 (gate_dependency's free-text exclusion) and the
        # EDGE_KIND_META SSOT agree independently — this join's exclusion of
        # gate_dependency falls out of deriving from EDGE_KIND_META, it is
        # not a separate carve-out that could drift from D1.
        assert "gate_dependency" not in EDGE_KIND_META


class TestAdditionalPredecessorsMulti:
    """``additional_predecessors`` is the one ``EDGE_KIND_META`` field with
    ``multi: True`` — an array of handoff references, each resolved
    independently, rendering one row per declared entry rather than one row
    per handoff."""

    def test_multi_field_present_in_join_output(self, tmp_path: Path):
        _write_handoff(tmp_path, "target.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter="additional_predecessors:\n  - target.md\n",
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert isinstance(child["fields"]["additional_predecessors"], list)
        assert len(child["fields"]["additional_predecessors"]) == 1
        assert child["fields"]["additional_predecessors"][0]["resolution_state"] == "live"

    def test_each_entry_resolves_independently_across_all_four_states(self, tmp_path: Path):
        _write_handoff(tmp_path, "live-target.md")
        archived_path = _write_archived_handoff(tmp_path, "", "archived-target.md")
        _write_non_handoff(tmp_path, "docs/problems/some-problem.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter=(
                "additional_predecessors:\n"
                "  - live-target.md\n"
                f"  - {archived_path}\n"
                "  - state/handoffs/deleted.md\n"
                "  - docs/problems/some-problem.md\n"
            ),
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))
        entries = child["fields"]["additional_predecessors"]

        assert [e["resolution_state"] for e in entries] == [
            "live",
            "archived",
            "gone",
            "invalid-target",
        ]
        # Order preserved — matches declaration order, so a caller rendering
        # "one row per entry" gets a stable, reproducible sequence.
        assert entries[0]["declared"] == "live-target.md"
        assert entries[2]["declared"] == "state/handoffs/deleted.md"

    def test_sentinel_entries_are_dropped_not_represented(self, tmp_path: Path):
        _write_handoff(tmp_path, "live-target.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter=(
                "additional_predecessors:\n"
                "  - live-target.md\n"
                "  - none\n"
            ),
        )

        joined = _join_handoff_lineage(tmp_path)
        child = next(r for r in joined if r["path"].endswith("child.md"))
        entries = child["fields"]["additional_predecessors"]

        assert len(entries) == 1
        assert entries[0]["declared"] == "live-target.md"

    def test_undeclared_multi_field_resolves_to_empty_list(self, tmp_path: Path):
        _write_handoff(tmp_path, "solo.md")

        joined = _join_handoff_lineage(tmp_path)
        solo = next(r for r in joined if r["path"].endswith("solo.md"))

        assert solo["fields"]["additional_predecessors"] == []

    def test_multi_field_renders_one_row_per_entry(self, tmp_path: Path):
        _write_handoff(tmp_path, "live-target.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter=(
                "additional_predecessors:\n"
                "  - live-target.md\n"
                "  - state/handoffs/deleted.md\n"
            ),
        )

        output = render_handoff_lineage_markdown(tmp_path)
        section = _section(output, "additional_predecessors")

        assert "child.md" in section
        assert "live" in section
        assert "gone" in section
        rows = [line for line in section.splitlines() if line.startswith("| [")]
        assert len(rows) == 2

    def test_count_reconciliation_with_multiple_entries_on_one_handoff(self, tmp_path: Path):
        _write_handoff(tmp_path, "live-target.md")
        archived_path = _write_archived_handoff(tmp_path, "", "archived-target.md")
        _write_handoff(
            tmp_path, "child.md",
            extra_frontmatter=(
                "additional_predecessors:\n"
                "  - live-target.md\n"
                f"  - {archived_path}\n"
                "  - state/handoffs/deleted-one.md\n"
                "  - state/handoffs/deleted-two.md\n"
            ),
        )

        tracker = render_repo_section(tmp_path)
        lineage_md = render_handoff_lineage_markdown(tmp_path)

        # 1 archived + 2 gone (live doesn't appear in the remainder pointer).
        assert "1 link resolves to an archived handoff" in tracker
        assert "2 links point at a target no longer on disk" in tracker

        section = _section(lineage_md, "additional_predecessors")
        rows = [line for line in section.splitlines() if line.startswith("| [")]
        assert len(rows) == 4


class TestTier3PrunedResolution:
    """Tier 3 (git-history) fall-through — spinoff of the parent
    handoff-lineage-visibility baton, adopting ``dag.resolve_target``'s
    existing 3-tier resolver rather than a new one. State backlink:
    state/handoffs/2026-07-25-git-history-resolution-tier-for-handoff-.md"""

    def test_predecessor_disk_absent_but_git_tracked_resolves_pruned(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")
        _write_handoff(root, "child.md", extra_frontmatter="predecessor: pruned-target.md\n")

        joined = _join_handoff_lineage(root)
        child = next(r for r in joined if r["path"].endswith("child.md"))
        field = child["fields"]["predecessor"]

        assert field["resolution_state"] == "pruned"
        assert field["resolved_path"] is None
        assert field["archived_path"] is None
        assert field["declared"] == "pruned-target.md"

    def test_ref_unresolvable_in_all_three_tiers_stays_gone(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _write_handoff(
            root, "child.md",
            extra_frontmatter="predecessor: state/handoffs/never-existed.md\n",
        )

        joined = _join_handoff_lineage(root)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "gone"

    def test_pruned_rendered_as_plain_text_not_a_link(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")
        _write_handoff(root, "child.md", extra_frontmatter="predecessor: pruned-target.md\n")

        output = render_handoff_lineage_markdown(root)
        section = _section(output, "predecessor")

        assert "pruned" in section
        assert "pruned-target.md" in section
        assert "[pruned-target.md]" not in section


class TestGitHistoryCacheThreading:
    """The cache is mandatory when NEEDED, lazy when it isn't (spec §2,
    follow-up round) — these tests assert the actual subprocess-spawn
    behaviour, not just resolved values, since a correct-but-slow
    implementation would pass every other test in this module."""

    def test_cache_hit_avoids_any_subprocess_spawn(self, tmp_path: Path, monkeypatch):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")
        # Declared as the repo-relative form (not a bare basename) so
        # resolve_target's tier-3 check asks the cache the SAME key the
        # cache is keyed on directly — a bare-basename declared value would
        # legitimately cost one spawn resolving the raw (uncached) basename
        # before falling through to a re-derived, cache-hit candidate; that
        # is correct "cache miss falls through" behaviour, not what this
        # test is isolating.
        _write_handoff(
            root, "child.md",
            extra_frontmatter="predecessor: state/handoffs/pruned-target.md\n",
        )

        provider = _GitHistoryCacheProvider(str(root))
        # Force-build once up front (out of the counted window below) so
        # this test isolates "reuse after the first build", not the build
        # itself — see TestLazyCacheBuild below for the first-build story.
        built = provider.get()
        assert built is not None
        assert "state/handoffs/pruned-target.md" in built

        spawn_count = [0]
        orig_run = dag_module.subprocess.run

        def counting_run(argv, *a, **kw):
            spawn_count[0] += 1
            return orig_run(argv, *a, **kw)

        monkeypatch.setattr(dag_module.subprocess, "run", counting_run)

        joined = _join_handoff_lineage(root, git_history_cache=provider)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "pruned"
        assert spawn_count[0] == 0, (
            "expected zero git subprocess spawns when the target path is "
            f"already present in a pre-built git_history_cache, got {spawn_count[0]}"
        )

    def test_none_cache_falls_back_to_per_call_check_not_a_crash(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")
        _write_handoff(root, "child.md", extra_frontmatter="predecessor: pruned-target.md\n")

        joined = _join_handoff_lineage(root, git_history_cache=None)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "pruned"

    def test_cache_miss_falls_through_never_fast_rejects(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _write_handoff(root, "seed.md")
        _commit_path(root, "state/handoffs/seed.md")

        # Cache built BEFORE pruned-target.md is ever added to history — a
        # genuine MISS, not an empty/None cache.
        stale_cache = build_git_history_cache(str(root))
        assert stale_cache is not None
        assert "state/handoffs/pruned-target.md" not in stale_cache

        _prune_handoff(root, "pruned-target.md")
        _write_handoff(root, "child.md", extra_frontmatter="predecessor: pruned-target.md\n")

        provider = _GitHistoryCacheProvider(str(root))
        provider._built = True
        provider._cache = stale_cache  # simulate a pre-built stale cache

        joined = _join_handoff_lineage(root, git_history_cache=provider)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "pruned", (
            "a cache MISS must fall through to the per-call check (fast-path "
            "ACCEPT only, never fast-path REJECT)"
        )


class TestLazyCacheBuild:
    """Follow-up round (coordinator feedback): the cache must be built on
    the FIRST tier-3 need, not unconditionally at render start — a render
    where every candidate resolves in tier 1/2 must spawn zero subprocesses,
    and a render with multiple tier-3 candidates must build the cache
    exactly once, reusing the first result (including a None/failed build)
    for every subsequent candidate."""

    def test_all_clean_resolution_never_builds_cache(self, tmp_path: Path, monkeypatch):
        root = tmp_path
        _init_git_repo(root)
        _write_handoff(root, "target.md")
        _write_handoff(root, "child.md", extra_frontmatter="predecessor: target.md\n")

        build_calls = [0]
        orig_build = renderers_module.build_git_history_cache

        def counting_build(repo_root, *a, **kw):
            build_calls[0] += 1
            return orig_build(repo_root, *a, **kw)

        monkeypatch.setattr(renderers_module, "build_git_history_cache", counting_build)

        provider = _GitHistoryCacheProvider(str(root))
        joined = _join_handoff_lineage(root, git_history_cache=provider)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "live"
        assert build_calls[0] == 0, (
            "expected the cache to never be built when every candidate "
            f"resolves in tier 1/2, got {build_calls[0]} build(s)"
        )

    def test_multiple_tier3_candidates_build_cache_exactly_once(
        self, tmp_path: Path, monkeypatch
    ):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-one.md")
        _prune_handoff(root, "pruned-two.md")
        _write_handoff(
            root, "child.md",
            extra_frontmatter=(
                "predecessor: state/handoffs/pruned-one.md\n"
                "origin_handoff: state/handoffs/pruned-two.md\n"
            ),
        )

        build_calls = [0]
        orig_build = renderers_module.build_git_history_cache

        def counting_build(repo_root, *a, **kw):
            build_calls[0] += 1
            return orig_build(repo_root, *a, **kw)

        monkeypatch.setattr(renderers_module, "build_git_history_cache", counting_build)

        provider = _GitHistoryCacheProvider(str(root))
        joined = _join_handoff_lineage(root, git_history_cache=provider)
        child = next(r for r in joined if r["path"].endswith("child.md"))

        assert child["fields"]["predecessor"]["resolution_state"] == "pruned"
        assert child["fields"]["origin_handoff"]["resolution_state"] == "pruned"
        assert build_calls[0] == 1, (
            "expected exactly ONE cache build across two tier-3 candidates "
            f"in one render, got {build_calls[0]}"
        )

    def test_none_result_from_build_is_memoized_not_retried(self, tmp_path: Path, monkeypatch):
        """The subtlest claim in ``_GitHistoryCacheProvider``'s own docstring:
        a ``None`` result (git failure) is built once and then memoized —
        ``_built`` is a separate flag from "is ``_cache`` still ``None``?"
        precisely so a failed build is never retried on a later ``.get()``
        call in the same render."""
        root = tmp_path

        build_calls = [0]

        def none_build(repo_root, *a, **kw):
            build_calls[0] += 1
            return None

        monkeypatch.setattr(renderers_module, "build_git_history_cache", none_build)

        provider = _GitHistoryCacheProvider(str(root))

        first = provider.get()
        second = provider.get()

        assert first is None
        assert second is None
        assert build_calls[0] == 1, (
            "expected the failed build to be attempted exactly once and "
            f"memoized, got {build_calls[0]} call(s)"
        )


class TestPrunedCountReconciliation:
    def test_pruned_counts_reconcile_between_tracker_and_lineage_file(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")
        _write_handoff(root, "child.md", extra_frontmatter="predecessor: pruned-target.md\n")

        tracker = render_repo_section(root)
        lineage_md = render_handoff_lineage_markdown(root)

        assert (
            "1 link points at a target pruned from disk but recoverable from git history"
            in tracker
        )
        assert "— see state/handoff-lineage.md" in tracker

        predecessor_section = _section(lineage_md, "predecessor")
        rows = [line for line in predecessor_section.splitlines() if line.startswith("| [")]
        assert len(rows) == 1
        assert "child.md" in predecessor_section
        assert "pruned" in predecessor_section

    def test_pruned_and_gone_coexist_and_both_reconcile(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")
        _write_handoff(
            root, "pruned-child.md", extra_frontmatter="predecessor: pruned-target.md\n"
        )
        _write_handoff(
            root, "gone-child.md",
            extra_frontmatter="predecessor: state/handoffs/never-existed.md\n",
        )

        tracker = render_repo_section(root)
        lineage_md = render_handoff_lineage_markdown(root)

        assert (
            "1 link points at a target pruned from disk but recoverable from git history"
            in tracker
        )
        assert "1 link points at a target no longer on disk" in tracker

        predecessor_section = _section(lineage_md, "predecessor")
        rows = [line for line in predecessor_section.splitlines() if line.startswith("| [")]
        assert len(rows) == 2
        assert "pruned-child.md" in predecessor_section
        assert "gone-child.md" in predecessor_section


class TestPlansJoinPrunedCollapse:
    """D5 — the plans join collapses "pruned" to "gone" explicitly, the same
    treatment ``TestPlansJoinNoRegression`` above verifies for
    "invalid-target". ``docs/plans/INDEX.md`` gains no fourth/fifth section."""

    def test_plan_predecessor_pruned_collapses_to_gone(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")

        plans_dir = root / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "2026-07-19-example-plan.md").write_text(
            "---\ntitle: Example\nstatus: draft\n"
            "predecessor_handoff: pruned-target.md\n---\nBody.\n",
            encoding="utf-8",
        )

        from coordinator_core.ops.ceremony.renderers import _collect_plans_with_parse_errors

        joined = _join_plans_to_handoffs(_collect_plans_with_parse_errors(root), root)
        plan = next(j for j in joined if j["path"].endswith("2026-07-19-example-plan.md"))

        assert plan["resolution_state"] == "gone"
        assert plan["resolved_handoff_path"] is None
        assert plan["archived_handoff_path"] is None

    def test_plans_index_places_pruned_predecessor_in_unlinked_only(self, tmp_path: Path):
        root = tmp_path
        _init_git_repo(root)
        _prune_handoff(root, "pruned-target.md")

        plans_dir = root / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "2026-07-19-example-plan.md").write_text(
            "---\ntitle: Example\nstatus: draft\n"
            "predecessor_handoff: pruned-target.md\n---\nBody.\n",
            encoding="utf-8",
        )

        from coordinator_core.ops.ceremony.renderers import render_plans_index_markdown

        index = render_plans_index_markdown(root)
        unlinked_section = index.split("## Unlinked", 1)[1]
        linked_section = index.split("## Linked", 1)[1].split("## Archived", 1)[0]
        archived_section = index.split("## Archived", 1)[1].split("## Unlinked", 1)[0]

        assert "2026-07-19-example-plan.md" in unlinked_section
        assert "2026-07-19-example-plan.md" not in linked_section
        assert "2026-07-19-example-plan.md" not in archived_section
        # No new/fourth section — the collapse means only three sections ever
        # exist (Linked/Archived/Unlinked), independent of the target's own
        # filename happening to contain the word "pruned".
        assert index.count("## ") == 3
