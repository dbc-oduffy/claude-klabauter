"""s_plan_path never yields a doubled date prefix, and leaves a single-dated stem untouched."""

from coordinator_core.ops.dispatch_emit.sizing_fire import s_plan_path


def test_single_dated_stem_unchanged():
    assert s_plan_path("state/sizings/2026-10-28-foo-abc123.yaml") == "docs/plans/2026-10-28-foo-abc123.md"


def test_doubled_date_prefix_collapses():
    assert s_plan_path("state/sizings/2026-10-28-2026-10-28-foo.yaml") == "docs/plans/2026-10-28-foo.md"


def test_inner_date_not_collapsed():
    assert s_plan_path("a/2026-10-28-foo-2026-10-28.yaml") == "docs/plans/2026-10-28-foo-2026-10-28.md"
