"""`read_spine(plan_edge_cache=)`: a caller-owned edge cache loads a shared target once."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import spine_read
from coordinator_core.ops.dispatch_emit.spine_read import read_spine


def _plan(path, rows_yaml: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows_yaml + "\n```\n", encoding="utf-8"
    )
    return path


def _dependent(chunk_id: str) -> str:
    return (
        f"- id: {chunk_id}\n  title: dependent\n  surface: b.py\n  writes: [b.py]\n"
        "  depends_on_plan:\n    - {plan: docs/plans/pred.md, chunk: P1}\n"
    )


def _fixture(tmp_path):
    (tmp_path / ".git").mkdir()
    _plan(
        tmp_path / "docs" / "plans" / "pred.md",
        "- id: P1\n  title: pred-target\n  surface: a.py\n  writes: [a.py]\n"
        "  disposition: coded\n",
    )
    return (
        _plan(tmp_path / "docs" / "plans" / "one.md", _dependent("D1")),
        _plan(tmp_path / "docs" / "plans" / "two.md", _dependent("D2")),
    )


def _spy(monkeypatch):
    seen: list[str] = []
    real = spine_read.load_rows_memo

    def spy(source):
        seen.append(source)
        return real(source)

    monkeypatch.setattr(spine_read, "load_rows_memo", spy)
    return seen


def _shape(rows):
    return [(r.id, r.status) if hasattr(r, "status") else r.id for r in rows]


def test_shared_cache_loads_target_once(tmp_path, monkeypatch):
    one, two = _fixture(tmp_path)
    seen = _spy(monkeypatch)
    cache: dict = {}
    shared = [read_spine(one, plan_edge_cache=cache), read_spine(two, plan_edge_cache=cache)]
    assert sum("pred-target" in s for s in seen) == 1

    seen.clear()
    unshared = [read_spine(one), read_spine(two)]
    assert sum("pred-target" in s for s in seen) == 2
    assert [_shape(r) for r in shared] == [_shape(r) for r in unshared]


def test_shared_cache_with_landed_plan_chunks_raises(tmp_path):
    one, _ = _fixture(tmp_path)
    with pytest.raises(ValueError):
        read_spine(one, plan_edge_cache={}, landed_plan_chunks={("docs/plans/pred.md", "P1"): "abc"})


def test_default_cache_is_per_call(tmp_path, monkeypatch):
    one, _ = _fixture(tmp_path)
    seen = _spy(monkeypatch)
    read_spine(one)
    read_spine(one)
    assert sum("pred-target" in s for s in seen) == 2


def test_own_plan_path_resolved_once_per_path(tmp_path):
    spine_read._absolute_plan_key.cache_clear()
    writes = ["docs/plans/one.md", "x.py"]
    for _ in range(5):
        spine_read._route_own_plan_body_write(writes, tmp_path / "docs" / "plans" / "one.md")
    info = spine_read._absolute_plan_key.cache_info()
    assert (info.misses, info.hits) == (1, 4)
