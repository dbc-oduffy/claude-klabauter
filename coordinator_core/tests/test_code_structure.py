from __future__ import annotations

from coordinator_core import code_structure
from coordinator_core.architecture_survey.structure import StructureUnavailable


def _repo(tmp_path):
    (tmp_path / "a.py").write_text("X = 1\n\ndef f():\n    pass\n\nclass C:\n    def m(self): pass\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Title\n\n```\n# not heading\n```\n\n## Sub ##\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def (:\n", encoding="utf-8")
    return tmp_path


def _post_for(root, rows, calls):
    def post(url, payload, timeout_s):
        name = payload["params"]["name"]
        calls.append((name, payload["params"]["arguments"], timeout_s))
        if name == "project_list_sources":
            return {"data": {"resolution": {"git_root": str(root)}}}
        return {"verdict": "ok", "data": {"results": rows}}

    return post


def _down(url, payload, timeout_s):
    raise StructureUnavailable("rag daemon unreachable (test)")


DOOR = "project_batched_structure"


def _with_door(monkeypatch, cost_class="within"):
    real = code_structure._load_costs()
    real["needs"][code_structure.NEED] = {"batched_door": DOOR}
    real["doors"][DOOR] = {"cost_class": cost_class}
    monkeypatch.setattr(code_structure, "_load_costs", lambda: real)


def test_no_batched_door_in_cost_record_means_no_call(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    calls: list = []
    out = code_structure.facts_for(root, ["a.py"], post=_post_for(root, [], calls))
    assert calls == []
    assert out["covered"] is False and out["reason"] == code_structure.PENDING_REASON
    assert set(out) == {"covered", "reason", "symbols", "referencers"}
    assert out["symbols"]["a.py"] == ["X", "f", "C"] and out["referencers"] == {"a.py": []}


def test_over_bar_door_is_not_called(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    _with_door(monkeypatch, "over-bar")
    calls: list = []
    out = code_structure.facts_for(root, ["a.py"], post=_post_for(root, [], calls))
    assert calls == [] and out["covered"] is False


def test_fallback_without_rag(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    _with_door(monkeypatch)
    out = code_structure.facts_for(root, ["a.py", "b.md", "bad.py", "missing.py", "c.txt"], post=_down)
    assert out["covered"] is False
    assert "unreachable" in out["reason"]
    assert out["symbols"] == {"a.py": ["X", "f", "C"], "b.md": ["Title", "Sub"], "bad.py": [], "missing.py": [], "c.txt": []}
    assert out["referencers"]["a.py"] == []


def test_rag_answer_is_covered_and_one_batched_call(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    _with_door(monkeypatch)
    calls: list = []
    rows = [{"file": "a.py", "symbols": [{"name": "f"}], "referencers": ["z.py"]}]
    out = code_structure.facts_for(root, ["a.py", "b.md"], post=_post_for(root, rows, calls))
    assert out["covered"] is True and out["reason"] is None
    assert out["symbols"]["a.py"] == ["f"] and out["referencers"]["a.py"] == ["z.py"]
    assert out["symbols"]["b.md"] == ["Title", "Sub"]
    structure_calls = [c for c in calls if c[0] == DOOR]
    assert len(structure_calls) == 1 and structure_calls[0][1] == {"paths": ["a.py", "b.md"]}
    assert all(c[2] <= 0.15 for c in calls)


def test_foreign_project_and_degraded_are_uncovered(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    _with_door(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    out = code_structure.facts_for(root, ["a.py"], post=_post_for(other, [], []))
    assert out["covered"] is False and "another project" in out["reason"]

    def degraded(url, payload, timeout_s):
        if payload["params"]["name"] == "project_list_sources":
            return {"data": {"resolution": {"git_root": str(root)}}}
        return {"verdict": "warming", "data": {}}

    out = code_structure.facts_for(root, ["a.py"], post=degraded)
    assert out["covered"] is False and out["symbols"]["a.py"] == ["X", "f", "C"]


def test_timeout_and_outside_cwd_never_raise(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _with_door(monkeypatch)

    def slow(url, payload, timeout_s):
        raise TimeoutError("t")

    monkeypatch.chdir(root)
    assert code_structure.facts_for(root, ["a.py"], post=slow)["covered"] is False
    monkeypatch.chdir(tmp_path.parent)
    out = code_structure.facts_for(root, ["a.py"], post=_post_for(root, [], []))
    assert out["covered"] is False and "outside repo_root" in out["reason"]
