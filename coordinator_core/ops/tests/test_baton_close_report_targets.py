"""Target done-ness readers for baton.close_report: each class, missing, unreadable, read-once."""

from __future__ import annotations

import builtins
from pathlib import Path

from coordinator_core.ops import baton_close_report_targets as t


def _h(**kw):
    base = {"path": "state/handoffs/b.md", "status": "open", "predecessor": None,
            "governing_plan": None, "origin_plan_id": None, "sizing_object": None,
            "roadmap_id": None, "origin_goal_id": []}
    base.update(kw)
    return base


def _w(root: Path, rel: str, text: str, crlf: bool = False) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if crlf:
        text = text.replace("\n", "\r\n")
    p.write_bytes(text.encode("utf-8"))


def _run(root, **kw):
    return t.target_evidence(str(root), {"b": _h(**kw)}).get("b")


def test_plan_done_not_done_missing_unreadable(tmp_path):
    _w(tmp_path, "docs/plans/a.md", "---\ntitle: x\nstatus: implemented  # note\n---\nbody\n")
    _w(tmp_path, "docs/plans/b.md", "---\nstatus: draft\n---\n")
    _w(tmp_path, "docs/plans/c.md", "---\ntitle: x\n---\n")
    for name, done, status in (("a", True, "implemented"), ("b", False, "draft"), ("c", False, None)):
        (ev,) = _run(tmp_path, governing_plan=f"docs/plans/{name}.md")
        assert (ev["class"], ev["done"], ev["target_status"]) == ("plan", done, status)
    (ev,) = _run(tmp_path, governing_plan="docs/plans/zz.md")
    assert ev["target_status"] == t.STATUS_MISSING and not ev["done"]


def test_plan_landed_is_not_done(tmp_path):
    _w(tmp_path, "docs/plans/a.md", "---\nstatus: landed\n---\n")
    (ev,) = _run(tmp_path, governing_plan="docs/plans/a.md")
    assert ev["target_status"] == "landed" and ev["done"] is False


def test_open_permission_error_reads_unreadable_not_missing(tmp_path, monkeypatch):
    _w(tmp_path, "docs/plans/a.md", "---\nstatus: implemented\n---\n")

    def deny(file, *a, **k):
        raise PermissionError(13, "denied", str(file))

    monkeypatch.setattr(t, "open", deny, raising=False)
    (ev,) = _run(tmp_path, governing_plan="docs/plans/a.md")
    assert ev["target_status"] == t.STATUS_UNREADABLE == "target_unreadable"
    assert ev["done"] is False


def test_legacy_unfenced_prose_status_is_none_not_done(tmp_path):
    _w(tmp_path, "docs/plans/a.md", "# Plan\n\n> **Status:** SHIPPED\n\nbody\n")
    (ev,) = _run(tmp_path, governing_plan="docs/plans/a.md")
    assert ev["target_status"] is None and ev["done"] is False


def test_plan_status_outside_frontmatter_ignored(tmp_path):
    _w(tmp_path, "docs/plans/a.md", "---\ntitle: x\n---\nstatus: landed\n")
    (ev,) = _run(tmp_path, governing_plan="docs/plans/a.md")
    assert ev["target_status"] is None and not ev["done"]


def test_sizing_done_and_indented_status_ignored(tmp_path):
    _w(tmp_path, "state/sizings/a.yaml", "schema: s\nstatus: shipped  # draft | sized\n")
    _w(tmp_path, "state/sizings/b.yaml", "schema: s\nem_analysis:\n  status: shipped\n")
    (ev,) = _run(tmp_path, sizing_object="state/sizings/a.yaml")
    assert ev["done"] and ev["target_status"] == "shipped"
    (ev,) = _run(tmp_path, sizing_object="state/sizings/b.yaml")
    assert not ev["done"] and ev["target_status"] is None


def test_roadmap_done_and_not_done(tmp_path):
    _w(tmp_path, "state/roadmap/r1/OVERVIEW.md", "---\nroadmap_id: r1\nstatus: shipped\n---\n")
    _w(tmp_path, "state/roadmap/r2/OVERVIEW.md", "---\nstatus: final-approved\n---\n")
    (ev,) = _run(tmp_path, roadmap_id="r1")
    assert ev["done"] and ev["target_path"] == "state/roadmap/r1/OVERVIEW.md"
    (ev,) = _run(tmp_path, roadmap_id="r2")
    assert not ev["done"]
    (ev,) = _run(tmp_path, roadmap_id="nope")
    assert ev["target_status"] == t.STATUS_MISSING
    (ev,) = _run(tmp_path, roadmap_id="../x")
    assert not ev["done"]


def test_goal_resolved_by_id_field_not_filename(tmp_path):
    _w(tmp_path, "state/goals/2026-07-10-file-name.yaml",
       'schema: goal\nid: "goal-a"\nstatus: active\n')
    evs = _run(tmp_path, origin_goal_id=["goal-a", "goal-zz"])
    assert evs[0]["target_path"] == "state/goals/2026-07-10-file-name.yaml"
    assert evs[0]["target_status"] == "active" and not evs[0]["done"]
    assert evs[1]["target_status"] == t.STATUS_MISSING


def test_goal_done_when_vocabulary_names_it(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "GOAL_DONE", frozenset({"achieved"}))
    _w(tmp_path, "state/goals/g.yaml", "id: goal-a\nstatus: achieved\n")
    (ev,) = _run(tmp_path, origin_goal_id=["goal-a"])
    assert ev["done"]


def test_origin_plan_id_resolved_through_plan_id_pass(tmp_path):
    _w(tmp_path, "docs/plans/a.md", '---\nplan_id: "pln-1"\nstatus: implemented\n---\n')
    (ev,) = _run(tmp_path, origin_plan_id="pln-1")
    assert ev["field"] == "origin_plan_id" and ev["target_path"] == "docs/plans/a.md" and ev["done"]
    (ev,) = _run(tmp_path, origin_plan_id="pln-missing")
    assert ev["target_status"] == t.STATUS_MISSING and not ev["done"]


def test_same_plan_by_both_fields_yields_one_item(tmp_path):
    _w(tmp_path, "docs/plans/a.md", '---\nplan_id: pln-1\nstatus: implemented\n---\n')
    evs = _run(tmp_path, governing_plan="docs/plans/a.md", origin_plan_id="pln-1")
    assert len(evs) == 1


def test_crlf_records(tmp_path):
    _w(tmp_path, "docs/plans/a.md", "---\nstatus: implemented\n---\n", crlf=True)
    _w(tmp_path, "state/sizings/a.yaml", "status: shipped\n", crlf=True)
    assert _run(tmp_path, governing_plan="docs/plans/a.md")[0]["done"]
    assert _run(tmp_path, sizing_object="state/sizings/a.yaml")[0]["done"]


def test_quoted_status_unquoted(tmp_path):
    _w(tmp_path, "state/sizings/a.yaml", "status: 'shipped'\n")
    assert _run(tmp_path, sizing_object="state/sizings/a.yaml")[0]["done"]


def test_target_cited_by_two_batons_read_once(tmp_path, monkeypatch):
    _w(tmp_path, "docs/plans/a.md", "---\nstatus: implemented\n---\n")
    opened: list[str] = []
    real = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real(file, *a, **k)

    monkeypatch.setattr(t, "open", spy, raising=False)
    heads = {"b1": _h(governing_plan="docs/plans/a.md"), "b2": _h(governing_plan="docs/plans/a.md")}
    out = t.target_evidence(str(tmp_path), heads)
    assert len(out["b1"]) == len(out["b2"]) == 1
    assert sum(p.endswith("a.md") for p in opened) == 1


def test_no_citation_absent_and_path_escape_not_read(tmp_path):
    assert t.target_evidence(str(tmp_path), {"b": _h()}) == {}
    (ev,) = _run(tmp_path, governing_plan="../outside.md")
    assert ev["target_status"] == t.STATUS_MISSING


def test_module_imports_no_yaml_or_subprocess():
    import ast

    tree = ast.parse(Path(t.__file__).read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not mods & {"yaml", "subprocess"}
