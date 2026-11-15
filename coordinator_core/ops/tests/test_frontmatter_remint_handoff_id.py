"""handoff_id resolver for frontmatter.remint: derive, refuse referenced / colliding / underivable."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import resolve_repo_name
from coordinator_core.ops.emit.sections.handoffs import _derive_handoff_id
from coordinator_core.ops.frontmatter_remint import handoff_id
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved


def _write(root: Path, rel: str, handoff_id_value: str | None, extra: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    hid = f"handoff_id: {handoff_id_value}\n" if handoff_id_value is not None else ""
    p.write_text(f"---\ntitle: x\n{hid}{extra}---\nbody\n", encoding="utf-8")
    return p


def _expected(root: Path, name: str) -> str:
    return _derive_handoff_id(resolve_repo_name(root), name)[0]


def test_resolves_wire_derived_id(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/2026-10-14-gap-fill-host-asks.md", "hnd-gap-fill-host-asks")
    res = handoff_id.resolve(p, {"handoff_id": "hnd-gap-fill-host-asks"}, tmp_path)
    assert res == Resolved({"handoff_id": _expected(tmp_path, p.name)})


def test_absent_id_resolves(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    assert isinstance(handoff_id.resolve(p, {}, tmp_path), Resolved)


@pytest.mark.parametrize(
    "key", ["predecessor_id", "origin_handoff_id", "predecessor", "forked_from"]
)
def test_refuses_when_old_id_referenced(tmp_path: Path, key: str) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    _write(tmp_path, "state/handoffs/other.md", "hnd-other-abcdef", f"{key}: hnd-a-thing\n")
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"


def test_refuses_when_referenced_from_archive_list_field(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    _write(
        tmp_path,
        "archive/handoffs/2026-10/o.md",
        "hnd-o-abcdef",
        "additional_predecessors:\n  - hnd-a-thing\n",
    )
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-referenced"


def test_unreferenced_old_id_is_not_refused(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    _write(tmp_path, "state/handoffs/other.md", "hnd-other-abcdef", "predecessor_id: hnd-zzz-123456\n")
    assert isinstance(handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path), Resolved)


def test_refuses_collision_with_other_holder(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", "hnd-a-thing")
    held = _expected(tmp_path, p.name)
    _write(tmp_path, "archive/handoffs/2026-10/zzz.md", held)
    res = handoff_id.resolve(p, {"handoff_id": "hnd-a-thing"}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-collision"


def test_already_holding_derived_id_is_not_a_collision(tmp_path: Path) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    mine = _expected(tmp_path, p.name)
    p.write_text(f"---\ntitle: x\nhandoff_id: {mine}\n---\nb\n", encoding="utf-8")
    assert handoff_id.resolve(p, {"handoff_id": mine}, tmp_path) == Resolved({"handoff_id": mine})


def test_refuses_underivable_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    monkeypatch.setattr(
        handoff_id, "_derive_handoff_id", lambda repo, path: ("hnd-placeholder-replace-with-x-abcdef", "derived")
    )
    res = handoff_id.resolve(p, {}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-underivable"


def test_refuses_underivable_when_slug_is_not_alnum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    monkeypatch.setattr(handoff_id, "_derive_handoff_id", lambda repo, path: ("hnd-Bad_Slug-abcdef", "derived"))
    res = handoff_id.resolve(p, {}, tmp_path)
    assert isinstance(res, Refused) and res.reason == "handoff-id-underivable"


def test_repo_label_is_the_emitting_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "state/handoffs/a-thing.md", None)
    seen: list[str] = []

    def spy(repo: str, path: str):
        seen.append(repo)
        return _derive_handoff_id(repo, path)

    monkeypatch.setattr(handoff_id, "_derive_handoff_id", spy)
    handoff_id.resolve(p, {}, tmp_path)
    assert seen == [resolve_repo_name(tmp_path)]
