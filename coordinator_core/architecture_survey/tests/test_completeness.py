"""completeness.verify: misses fail, orphans and stale pages are findings only."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from coordinator_core.architecture_survey import completeness

TODAY = date(2026, 10, 12)


def _page(root: Path, name: str, stamp: str = "2026-10-12") -> str:
    rel = f"docs/architecture/systems/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nsystem: {name}\nlast_mapped: {stamp}\n---\nbody\n", encoding="utf-8")
    return rel


def _index(root: Path, names: list[str]) -> None:
    p = root / "docs/architecture/systems-index.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"| {n} | 1 out / 1 in |" for n in names)
    p.write_text(f"# Index\n\n| System | Cross |\n|---|---|\n{rows}\n", encoding="utf-8")


def _wide(root: Path) -> None:
    for f in ("connectivity-matrix.md", "cross-system-map.md", "file-index.md"):
        (root / "docs/architecture" / f).write_text("x\n", encoding="utf-8")


def test_complete(tmp_path):
    pages = [_page(tmp_path, "a"), _page(tmp_path, "b")]
    _index(tmp_path, ["a", "b"])
    _wide(tmp_path)
    r = completeness.verify(tmp_path, ["a", "b"], pages, mode="bootstrap", today=TODAY)
    assert r.decision.chosen == "complete"


def test_missing_page_is_incomplete(tmp_path):
    _page(tmp_path, "a")
    _index(tmp_path, ["a", "b"])
    r = completeness.verify(tmp_path, ["a", "b"], [], mode="targeted", today=TODAY)
    assert r.decision.chosen == "incomplete"
    assert "missing-page: b" in r.decision.evidence


def test_missing_index_row_is_incomplete(tmp_path):
    _page(tmp_path, "a")
    _page(tmp_path, "b")
    _index(tmp_path, ["a"])
    r = completeness.verify(tmp_path, ["a", "b"], [], mode="targeted", today=TODAY)
    assert r.decision.chosen == "incomplete"
    assert "missing-index-row: b" in r.decision.evidence


def test_atlas_wide_pages_required_on_refresh_not_targeted(tmp_path):
    _page(tmp_path, "a")
    _index(tmp_path, ["a"])
    assert completeness.verify(tmp_path, ["a"], [], mode="targeted", today=TODAY).decision.chosen == "complete"
    r = completeness.verify(tmp_path, ["a"], [], mode="refresh", today=TODAY)
    assert r.decision.chosen == "incomplete"
    assert "file-index.md" in r.decision.evidence


def test_written_page_with_old_stamp_is_incomplete(tmp_path):
    rel = _page(tmp_path, "a", stamp="2026-10-01")
    _index(tmp_path, ["a"])
    r = completeness.verify(tmp_path, ["a"], [rel], mode="targeted", today=TODAY)
    assert r.decision.chosen == "incomplete"


def test_stale_page_listed_not_failed(tmp_path):
    _page(tmp_path, "a", stamp="2026-01-01")
    _index(tmp_path, ["a"])
    r = completeness.verify(tmp_path, ["a"], [], mode="targeted", today=TODAY)
    assert r.decision.chosen == "complete"
    assert r.stale_pages == ("docs/architecture/systems/a.md",)


def test_orphan_page_listed_not_failed(tmp_path):
    _page(tmp_path, "a")
    _page(tmp_path, "gone")
    _index(tmp_path, ["a"])
    _wide(tmp_path)
    r = completeness.verify(tmp_path, ["a"], [], mode="refresh", today=TODAY)
    assert r.decision.chosen == "complete"
    assert r.orphan_pages == ("docs/architecture/systems/gone.md",)
    assert (tmp_path / "docs/architecture/systems/gone.md").exists()
