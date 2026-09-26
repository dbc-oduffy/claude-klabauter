
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
    a = _write(tmp_path, "state/handoffs/a.md", "dlv-alpha-000001")
    b = _write(tmp_path, "state/handoffs/b.md", "dlv-alpha-000001")

    assert group_stamped_by_deliverable_id(tmp_path, [a, b]) == [
        ("dlv-alpha-000001", [a, b])
    ]


def test_group_collects_id_less_artifacts_under_the_empty_key(tmp_path):
    none = _write(tmp_path, "state/handoffs/none.md", None)
    blank = _write(tmp_path, "state/handoffs/blank.md", "null")
    a = _write(tmp_path, "state/handoffs/a.md", "dlv-alpha-000001")

    assert group_stamped_by_deliverable_id(tmp_path, [none, a, blank]) == [
        ("", [none, blank]),
        ("dlv-alpha-000001", [a]),
    ]
