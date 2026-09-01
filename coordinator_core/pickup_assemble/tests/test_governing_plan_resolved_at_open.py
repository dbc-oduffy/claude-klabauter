"""`/pickup` resolves the governing plan, through the SAME resolver as close-out.

`/pickup` is the moment a session inherits work, and it resolved the plan
nowhere: `resolve_governing_plan_with_source` had zero consumers in this
package, so the two ceremonies could disagree about which plan a baton governs
while reading the same frontmatter field.

Calling the shared resolver rather than growing a second precedence is the
point. C10 already reduced that ladder to two caller overrides plus the
baton's own stamped `governing_plan` -- "no ladder, no join, no scan is
attempted at any price" -- so consuming it costs no scan and no spawn. The
deliverable_id join it used to carry (leg 3.5) was deleted for returning
confident wrong answers, and must not be reintroduced here.

Negative-spec:
  - `source` must be carried, never flattened away. "No plan was named" and
    "a plan was named and is not on disk" are different facts, and collapsing
    both to `None` is what made the absence illegible.
  - A dangling citation must NOT fall through to some other plan. An
    explicit-but-wrong override resolves to nothing, per the resolver's own
    terminal-override rule.

Run: python -m pytest coordinator_core/pickup_assemble/tests/test_governing_plan_resolved_at_open.py -q
"""

from __future__ import annotations

import inspect

from pathlib import Path

import coordinator_core.pickup_assemble as pa


def _plan(repo: Path, name: str) -> None:
    d = repo / "docs" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("---\ntitle: p\n---\n\nBody.\n", encoding="utf-8")


def test_a_stamped_plan_resolves_with_its_source(tmp_path):
    _plan(tmp_path, "p1.md")
    got = pa.compute_governing_plan_resolution(
        tmp_path, {"governing_plan": "docs/plans/p1.md"}
    )
    assert got["rel"] == "docs/plans/p1.md"
    assert got["slug"] == "p1"
    assert got["source"] == "handoff_frontmatter"


def test_no_plan_named_is_distinguishable_from_a_bad_one(tmp_path):
    absent = pa.compute_governing_plan_resolution(tmp_path, {})
    dangling = pa.compute_governing_plan_resolution(
        tmp_path, {"governing_plan": "docs/plans/nope.md"}
    )
    assert absent["rel"] is None and dangling["rel"] is None
    # Same `rel`, different `source` — which is the whole reason `source` is
    # carried. A caller that only reads `rel` cannot tell a baton that named
    # no plan from one whose citation is broken.
    assert absent["source"] != dangling["source"]
    assert absent["source"] == "none"
    assert dangling["source"] == "handoff_frontmatter_not_found"


def test_a_dangling_citation_never_falls_through_to_another_plan(tmp_path):
    # A real plan exists; the baton names a different, missing one. Resolving
    # to the real one would be the confident-wrong-answer failure C10 deleted
    # the join legs to prevent.
    _plan(tmp_path, "real.md")
    got = pa.compute_governing_plan_resolution(
        tmp_path, {"governing_plan": "docs/plans/missing.md"}
    )
    assert got["rel"] is None


def test_the_stamped_field_is_the_only_input(tmp_path):
    # Review: overengineering-reviewer (finding 2). This REPLACES a test that
    # pinned a `decisions` override beating the stamp. That channel is
    # close-out's, unreachable from `/pickup`, and was the single way this
    # resolution could disagree with `compute_sizing_disposition`'s reading of
    # the same field in the same payload. The signature no longer accepts it,
    # so what needs pinning is that one stamped field decides the answer.
    _plan(tmp_path, "stamped.md")
    got = pa.compute_governing_plan_resolution(
        tmp_path, {"governing_plan": "docs/plans/stamped.md"}
    )
    assert got["slug"] == "stamped"
    assert got["source"] == "handoff_frontmatter"
    assert "decisions" not in inspect.signature(
        pa.compute_governing_plan_resolution
    ).parameters
