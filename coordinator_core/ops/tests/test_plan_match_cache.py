from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops import plan_match


def _plan(d: Path, name: str, title: str, body: str = "") -> Path:
    p = d / name
    p.write_text(f"---\ntitle: {title}\nplan_id: {name[:-3]}\n---\n{body}", encoding="utf-8")
    return p


def test_unchanged_plans_are_not_reparsed_and_changed_ones_are(tmp_path, monkeypatch):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    a = _plan(plans, "a.md", "Alpha")
    _plan(plans, "b.md", "Beta")
    plan_match._ENTRY_CACHE.clear()
    calls: list[str] = []
    real = yaml.load
    monkeypatch.setattr(yaml, "load", lambda s, Loader=None: (calls.append(s), real(s, Loader=Loader))[1])

    assert [i["title"] for i in plan_match._collect_plans(plans)] == ["Alpha", "Beta"]
    assert len(calls) == 2
    plan_match._collect_plans(plans)
    assert len(calls) == 2

    a.write_text("---\ntitle: Alpha2\nplan_id: a\n---\nlonger body\n", encoding="utf-8")
    assert [i["title"] for i in plan_match._collect_plans(plans)] == ["Alpha2", "Beta"]
    assert len(calls) == 3


def test_body_beyond_the_frontmatter_is_never_read(tmp_path, monkeypatch):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    _plan(plans, "big.md", "Big", body="x" * (plan_match._HEAD_CHUNK * 40))
    plan_match._ENTRY_CACHE.clear()
    reads: list[int] = []
    real_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (reads.append(1), real_text(self, *a, **k))[1])
    real_open = plan_match._open_binary

    class _Spy:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            out = self._fh.read(n)
            reads.append(len(out))
            return out

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    monkeypatch.setattr(plan_match, "_open_binary", lambda *a, **k: _Spy(real_open(*a, **k)))
    assert plan_match._collect_plans(plans)[0]["title"] == "Big"
    assert sum(reads) <= plan_match._HEAD_CHUNK * 2
