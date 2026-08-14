"""
coordinator_core.ops.ceremony.tests.test_consumed_handoff_stamp_multi_deliverable

Purpose: the grouping half of the follow-up-commit contract for a close that
consumed MORE THAN ONE baton. Pure frontmatter reads -- no git, no spawn, fast
tier. The git-backed half (does each group actually commit clean, does the
ungrouped set still refuse to guess) lives in
`test_consumed_handoff_stamp_multi_deliverable_commit.py`; it is a separate
file so its module-scoped spawn markers do not drag these tests out of the
fast tier with them.

Bug closed: `state/bug-backlog/2026-08-14-wsc-tail-cannot-stamp-a-two-baton-
pickup.yaml`. `/coordinator:pickup a AND b` mints one baton per artifact, each
with its own `deliverable_id`; the close then asked for ONE follow-up commit
naming every stamped handoff, and `commit_trailers.
_resolve_deliverable_id_from_paths` (tier 0) correctly refused to guess which
of two `deliverable_id` values that commit's `Deliverable-Id:` trailer should
carry -- raising `DivergentDeliverableIdError` AFTER the main ceremony commit
had already landed. The resolver's refusal is right and stays; the fix is
upstream of it, so the commit leg never asks one commit to carry two
deliverables.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony.consumed_handoff_stamp import (
    group_stamped_by_deliverable_id,
)


def _handoff(deliverable_id: str | None) -> str:
    fm = ["---", "kind: handoff"]
    if deliverable_id is not None:
        fm.append(f"deliverable_id: {deliverable_id}")
    fm += ["---", "", "body", ""]
    return "\n".join(fm)


def _write(root: Path, rel: str, deliverable_id: str | None) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_handoff(deliverable_id), encoding="utf-8")
    return rel


def test_group_partitions_one_group_per_deliverable_id(tmp_path):
    a = _write(tmp_path, "state/handoffs/a.md", "dlv-alpha-000001")
    b = _write(tmp_path, "state/handoffs/b.md", "dlv-beta-000002")
    a2 = _write(tmp_path, "state/handoffs/a2.md", "dlv-alpha-000001")

    groups = group_stamped_by_deliverable_id(tmp_path, [a, b, a2])

    assert groups == [
        ("dlv-alpha-000001", [a, a2]),
        ("dlv-beta-000002", [b]),
    ]


def test_group_single_deliverable_is_one_group(tmp_path):
    """The ordinary close is unchanged: one group, one commit, the same
    pathspec the pre-fix single-commit path used."""
    a = _write(tmp_path, "state/handoffs/a.md", "dlv-alpha-000001")
    b = _write(tmp_path, "state/handoffs/b.md", "dlv-alpha-000001")

    assert group_stamped_by_deliverable_id(tmp_path, [a, b]) == [
        ("dlv-alpha-000001", [a, b])
    ]


def test_group_collects_id_less_artifacts_under_the_empty_key(tmp_path):
    """An artifact carrying no `deliverable_id` groups under `""` -- tier 0
    abstains on it exactly as before and its commit falls through to the
    session-keyed tiers, rather than being folded into some other group's
    trailer. A literal `null` scalar reads as blank, matching
    `_read_deliverable_id_from_frontmatter`'s own blank-set."""
    none = _write(tmp_path, "state/handoffs/none.md", None)
    blank = _write(tmp_path, "state/handoffs/blank.md", "null")
    a = _write(tmp_path, "state/handoffs/a.md", "dlv-alpha-000001")

    assert group_stamped_by_deliverable_id(tmp_path, [none, a, blank]) == [
        ("", [none, blank]),
        ("dlv-alpha-000001", [a]),
    ]
