from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_match

_SHAPES = {
    "plain": "---\ntitle: Plain title here\nplan_id: pln-plain\nstatus: draft\n---\nbody\n",
    "quoted": "---\ntitle: 'It''s quoted'\nplan_id: \"pln-q\"\n---\n",
    "colon": "---\ntitle: \"Fix: the colon\"\nplan_id: pln-colon\n---\n",
    "colon_plain_err": "---\ntitle: Fix: the colon\nplan_id: pln-c2\n---\n",
    "no_plan_id": "---\ntitle: No id\n---\n",
    "int_title": "---\ntitle: 2026\nplan_id: pln-int\n---\n",
    "bool_title": "---\ntitle: yes\n---\n",
    "no_title": "---\nplan_id: pln-nt\nstatus: x\n---\n",
    "no_fm": "# just a heading\n",
    "bad_yaml": "---\ntitle: ok\nlist: [unclosed\n---\n",
    "crlf": "---\r\ntitle: Windows endings\r\nplan_id: pln-crlf\r\n---\r\nbody\r\n",
    "block_title": "---\ntitle: >\n  folded title\nplan_id: pln-blk\n---\n",
    "dup_key": "---\ntitle: first\ntitle: second\n---\n",
    "nested_title": "---\nmeta:\n  title: nested\ntitle: outer\n---\n",
    "unclosed": "---\ntitle: never closed\nplan_id: pln-unc\n",
    "nospace": "---\ntitle:tight\n---\n",
}


def _corpus(plans: Path, copies: int = 12) -> None:
    plans.mkdir(parents=True)
    for i in range(copies):
        for shape, text in _SHAPES.items():
            (plans / f"{shape}-{i:02d}.md").write_bytes(text.replace("\r\n", "\n").encode() if shape != "crlf" else text.encode())


def _reset() -> None:
    plan_match._ENTRY_CACHE.clear()
    plan_match._DISK_NAMES.clear()


def _no_yaml(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("yaml parsed a plan that the index already holds")

    monkeypatch.setattr(yaml, "load", boom)


def test_budget_exhausted_scan_converges_to_the_full_yaml_scan(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    _corpus(plans)
    cache = str(tmp_path / "cache")

    monkeypatch.setattr(plan_match, "_YAML_BUDGET_S", 1e9)
    _reset()
    reference = plan_match._collect_plans(plans)

    monkeypatch.setattr(plan_match, "_YAML_BUDGET_S", -1.0)
    _reset()
    plan_match._collect_plans(plans, cache)

    monkeypatch.setattr(plan_match, "_YAML_BUDGET_S", 1e9)
    _reset()
    plan_match._collect_plans(plans, cache)
    assert plan_match._collect_plans(plans, cache) == reference


def test_regex_read_agrees_with_yaml_where_it_answers(tmp_path):
    plans = tmp_path / "plans"
    _corpus(plans, copies=1)
    for p in sorted(plans.glob("*.md")):
        fm_text = plan_match._frontmatter_text(p)
        fast = plan_match._fast_fm(fm_text) if fm_text is not None else None
        if fast is None or not fast.get("title"):
            continue
        slow = plan_match._plan_entry(str(p), p.stem)
        if slow[:2] == ("skip", "parse error"):
            continue
        assert plan_match._entry_from_fm(fast, p.stem) == slow, p.name


def test_a_fresh_process_reads_the_persisted_index_without_parsing(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    _corpus(plans)
    cache = str(tmp_path / "cache")
    _reset()
    first = plan_match._collect_plans(plans, cache)
    assert (tmp_path / "cache" / plan_match._INDEX_FILENAME).is_file()

    for _ in range(3):
        _reset()
        plan_match._collect_plans(plans, cache)
    _reset()
    _no_yaml(monkeypatch)
    assert plan_match._collect_plans(plans, cache) == first


def test_changed_and_removed_plans_are_picked_up_from_the_index(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    cache = str(tmp_path / "cache")
    (plans / "a.md").write_text("---\ntitle: Alpha\n---\n", encoding="utf-8")
    (plans / "b.md").write_text("---\ntitle: Beta\n---\n", encoding="utf-8")
    _reset()
    assert [i["title"] for i in plan_match._collect_plans(plans, cache)] == ["Alpha", "Beta"]

    (plans / "a.md").write_text("---\ntitle: Alpha prime\n---\nmore\n", encoding="utf-8")
    (plans / "b.md").unlink()
    (plans / "c.md").write_text("---\ntitle: Gamma\n---\n", encoding="utf-8")
    _reset()
    assert [i["title"] for i in plan_match._collect_plans(plans, cache)] == ["Alpha prime", "Gamma"]
    blob = json.loads((tmp_path / "cache" / plan_match._INDEX_FILENAME).read_text(encoding="utf-8"))
    assert sorted(blob["e"]) == ["a.md", "c.md"]


def test_a_corrupt_index_reads_as_empty(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / plan_match._INDEX_FILENAME).write_text("{not json", encoding="utf-8")
    (plans / "a.md").write_text("---\ntitle: Alpha\n---\n", encoding="utf-8")
    _reset()
    assert [i["title"] for i in plan_match._collect_plans(plans, str(cache))] == ["Alpha"]


@pytest.mark.skipif(os.environ.get("CLAUDE_KLABAUTER_SKIP_TIMING") == "1", reason="timing pin disabled")
def test_cold_scan_of_a_large_corpus_is_inside_the_brightline(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    frontmatter = "".join(f"key_{n}: value number {n}\n" for n in range(25)) + "tags:\n  - a\n  - b\n"
    for i in range(2700):
        (plans / f"plan-{i:04d}.md").write_text(
            f"---\ntitle: Synthetic plan number {i}\nplan_id: pln-synth-{i:04d}\n{frontmatter}---\n" + "body\n" * 40,
            encoding="utf-8",
        )
    cache = str(tmp_path / "cache")
    _reset()
    start = time.process_time()
    items = plan_match._collect_plans(plans, cache)
    cold = time.process_time() - start
    assert len(items) == 2700
    assert cold < 0.5, f"cold scan took {cold:.3f}s process time"

    _reset()
    start = time.process_time()
    plan_match._collect_plans(plans, cache)
    assert time.process_time() - start < 0.5
