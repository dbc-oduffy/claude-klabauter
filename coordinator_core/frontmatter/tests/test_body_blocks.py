"""
Tests for coordinator_core.frontmatter.body_blocks.

Covers the located/absent/malformed trichotomy against the shared
plan-tasks-spine fixture corpus — a SINGLE canonical fixture set under
`coordinator/bin/tests/fixtures/plan-tasks-spine/`, held in common with
Coordinator-claude's `_locate_tasks_block` reference (exercised by
`coordinator/bin/tests/test_plan_tasks_spine_and_harvest.py`). Both suites
parametrize over the SAME `FIXTURE_EXPECTATIONS` table
(`coordinator/bin/tests/fixtures/plan-tasks-spine/fixture_expectations.py`)
so a future divergence between the two locators fails a test instead of
surviving in a docstring — see that module's docstring for the incident
(commit `08cbf4bd`) this consolidation guards against recurring. Prior to
this consolidation, two SEPARATE, partly-overlapping fixture dirs existed
(one per locator); no test could ever fail on their disagreement because
no test ever ran both locators over the same file.

Also pins the span contract: the returned span is the fence-BODY span
(`match.span(1)`), not the whole-fenced-block span.

Spec backlinks:
  coordinator_core/frontmatter/body_blocks.py
  coordinator/bin/coordinator-harvest-deferrals (coordinator-claude, lines 317-372)
  coordinator/bin/tests/fixtures/plan-tasks-spine/fixture_expectations.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core.frontmatter.body_blocks import (
    LocateStatus,
    locate_fenced_block,
)

# ---------------------------------------------------------------------------
# Canonical fixture corpus — the ONE dir both locators are held to.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = _REPO_ROOT / "coordinator" / "bin" / "tests" / "fixtures" / "plan-tasks-spine"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text()


# Load the shared expectation table by file path — this fixtures dir is not
# a package (no __init__.py, deliberately: it is a fixture corpus, not
# importable library code), so a plain `import` statement cannot reach it
# from either consuming test file. Both suites resolve it this same way.
_exp_spec = importlib.util.spec_from_file_location(
    "plan_tasks_spine_fixture_expectations", _FIXTURES_DIR / "fixture_expectations.py"
)
_exp_mod = importlib.util.module_from_spec(_exp_spec)  # type: ignore[arg-type]
_exp_spec.loader.exec_module(_exp_mod)  # type: ignore[union-attr]

FIXTURE_EXPECTATIONS = _exp_mod.FIXTURE_EXPECTATIONS
LocateOutcome = _exp_mod.LocateOutcome


def _expected_status(fixture_name: str) -> LocateStatus:
    """Translate the shared table's locator-agnostic `LocateOutcome` into
    this module's own `LocateStatus` at the assertion site (per the shared
    table's docstring) — the two enums' `.value` strings are pinned equal
    by construction, so this is a value lookup, not a mapping table (a
    second table is exactly what this consolidation is fixing).
    """
    return LocateStatus(FIXTURE_EXPECTATIONS[fixture_name].outcome.value)


# ===========================================================================
# Named, content-bearing tests (span/body assertions, not just status)
# ===========================================================================


def test_zero_fenced_blocks_is_absent():
    source = _read_fixture("zero-fenced-blocks.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.ABSENT
    assert result.body is None
    assert result.span is None


def test_heading_without_fence_is_malformed():
    # The '## Tasks' heading's own section contains no fence; a fence exists
    # but under a later, different heading — containment bounds the search.
    source = _read_fixture("heading-without-fence.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.MALFORMED
    assert result.body is None
    assert result.span is None


def test_multiple_fenced_blocks_is_malformed():
    # Negative-spec: HTML-comment blanking narrows false positives, it does
    # not weaken the genuine-duplicate guard — two REAL (non-comment) fenced
    # blocks anywhere in the document must still count as 2 post-blanking
    # and stay MALFORMED.
    source = _read_fixture("multiple-fenced-blocks.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.MALFORMED
    assert result.body is None
    assert result.span is None


def test_valid_single_is_located():
    source = _read_fixture("valid-single.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.LOCATED
    assert result.body is not None
    assert '- id: "t1"' in result.body
    assert result.span is not None


def test_valid_single_span_roundtrips_to_body_not_whole_block():
    source = _read_fixture("valid-single.md")
    result = locate_fenced_block(source)
    start, end = result.span

    assert source[start:end] == result.body

    # Negative-spec: the span must exclude the fence opener/closer markers.
    assert "```yaml plan-tasks" not in source[start:end]
    assert "```" not in source[start:end]

    # The characters immediately surrounding the span are the fence markers,
    # confirming the span is body-only (exclusive), not the whole block.
    assert source[start - len("```yaml plan-tasks\n") : start] == "```yaml plan-tasks\n"
    assert source[end : end + len("\n```")] == "\n```"


def test_default_params_match_plan_tasks_pair():
    source = _read_fixture("valid-single.md")
    explicit = locate_fenced_block(
        source, heading="Tasks", info_string="yaml plan-tasks"
    )
    default = locate_fenced_block(source)
    assert explicit == default


def test_valid_spine_with_deferrals_is_located():
    source = _read_fixture("valid-spine-with-deferrals.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.LOCATED
    assert result.body is not None
    assert "id: C1" in result.body
    assert source[result.span[0] : result.span[1]] == result.body


def test_malformed_row_fixture_is_located_despite_row_schema_issue():
    # malformed-row.md's D1 row is missing required fields (change_kind,
    # surface) — that is a row-SCHEMA concern (see
    # coordinator/bin/tests/test_plan_tasks_spine_and_harvest.py::
    # test_malformed_row_fails_schema_validation), not a locate-rule one:
    # the fence itself is well-formed and singular, so this locates cleanly.
    source = _read_fixture("malformed-row.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.LOCATED
    assert result.body is not None
    assert source[result.span[0] : result.span[1]] == result.body


def test_zero_blocks_with_deferred_marker_is_absent():
    # The harvest CLI escalates this shape to a loud, non-zero-exit failure
    # (belt-and-suspenders silent-loss guard) — that escalation is a
    # harvest-CLI-specific policy layered on top of the locate result, not
    # part of the locate rule itself, which reports plain ABSENT here (no
    # fenced block exists anywhere in the document).
    source = _read_fixture("zero-blocks-with-deferred-marker.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.ABSENT
    assert result.body is None
    assert result.span is None


def test_html_comment_blanking_locates_real_spine():
    # Fix 1: the unedited coordinator-doc-new template comment (embedding a
    # literal ```yaml plan-tasks``` token) sits directly under '## Tasks',
    # above the real fence. Unblanked, that comment both counts as a second
    # fence and as non-blank intervening content — either alone would have
    # tripped the old guards. Blanked, this locates cleanly.
    source = _read_fixture("template-comment-with-deferral.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.LOCATED
    assert result.body is not None
    assert "id: D1" in result.body
    assert source[result.span[0] : result.span[1]] == result.body


def test_load_bearing_prose_between_heading_and_fence_locates():
    # Fix 2 (containment, not adjacency): load-bearing prose (a
    # pinned-interface paragraph, a wave map) between the heading and the
    # fence must not MALFORMED as long as the fence stays inside the
    # '## Tasks' section — bounded here by the trailing
    # '## Some Later Section' heading.
    source = _read_fixture("prose-between-heading-and-fence.md")
    result = locate_fenced_block(source)
    assert result.status is LocateStatus.LOCATED
    assert result.body is not None
    assert "id: D1" in result.body
    assert source[result.span[0] : result.span[1]] == result.body


# ===========================================================================
# Inline-source parameterization tests (not fixture-file-based — these
# exercise the `heading=`/`info_string=` parameters directly, so they have
# no fixture-corpus counterpart).
# ===========================================================================


def test_parameterized_heading_and_info_string():
    source = (
        "## Other\n\n"
        "```yaml other-spine\n"
        "- id: \"x\"\n"
        "```\n"
    )
    result = locate_fenced_block(source, heading="Other", info_string="yaml other-spine")
    assert result.status is LocateStatus.LOCATED
    assert result.body == '- id: "x"'
    start, end = result.span
    assert source[start:end] == result.body


def test_parameterized_still_enforces_exactly_one():
    source = (
        "## Other\n\n"
        "```yaml other-spine\n"
        "- id: \"x\"\n"
        "```\n\n"
        "```yaml other-spine\n"
        "- id: \"y\"\n"
        "```\n"
    )
    result = locate_fenced_block(source, heading="Other", info_string="yaml other-spine")
    assert result.status is LocateStatus.MALFORMED


def test_parameterized_still_enforces_containment():
    # Prose between the heading and the fence no longer trips MALFORMED
    # (Fix 2) as long as the fence stays inside the heading's section.
    source = (
        "## Other\n\n"
        "Some prose that no longer breaks the located result.\n\n"
        "```yaml other-spine\n"
        "- id: \"x\"\n"
        "```\n"
    )
    result = locate_fenced_block(source, heading="Other", info_string="yaml other-spine")
    assert result.status is LocateStatus.LOCATED
    assert result.body == '- id: "x"'


def test_parameterized_fence_outside_section_is_malformed():
    # The fence lives under a DIFFERENT heading than the requested one —
    # containment still bounds the search, so this must stay MALFORMED.
    source = (
        "## Other\n\n"
        "Some prose, no fence in this section.\n\n"
        "## Elsewhere\n\n"
        "```yaml other-spine\n"
        "- id: \"x\"\n"
        "```\n"
    )
    result = locate_fenced_block(source, heading="Other", info_string="yaml other-spine")
    assert result.status is LocateStatus.MALFORMED


# ===========================================================================
# Full-corpus matrix — parametrized directly over the SHARED table, so any
# fixture added to `fixture_expectations.py` is automatically covered here
# without a second, hand-maintained parametrize list.
# ===========================================================================


@pytest.mark.parametrize("fixture_name", sorted(FIXTURE_EXPECTATIONS))
def test_fixture_set_matrix(fixture_name):
    source = _read_fixture(fixture_name)
    result = locate_fenced_block(source)
    assert result.status is _expected_status(fixture_name)
