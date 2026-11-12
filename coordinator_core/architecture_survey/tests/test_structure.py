"""structure.fetch: dead-paths from a batched door only, every other branch written covered:false."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.architecture_survey import contract, structure
from coordinator_core.architecture_survey.structure import StructureUnavailable, fetch

REAL_COSTS = json.loads((Path(structure.__file__).parent / "rag_read_costs.json").read_text(encoding="utf-8"))
RUN_ID = "20261112T170000Z-ab12"
PAGES = ["docs/architecture/a.md", "docs/architecture/b.md"]


def _costs(batched: bool = True) -> dict:
    costs = copy.deepcopy(REAL_COSTS)
    if batched:
        costs["needs"]["dead-paths"]["batched_door"] = "project_doc_links_batch"
        costs["doors"]["project_doc_links_batch"] = {"cost_class": "within", "timeout_ms": 700}
    return costs


class FakePost:
    def __init__(self, repo_root: Path, *, doc_envelope: dict | None = None, git_root: str | None = None,
                 source_name: str = "claude-klabauter", exc: Exception | None = None):
        self.calls: list[str] = []
        self.repo_root = repo_root
        self.git_root = git_root
        self.source_name = source_name
        self.exc = exc
        self.doc_envelope = doc_envelope if doc_envelope is not None else {
            "data": {"results": [
                {"file": PAGES[0], "outbound": [
                    {"target": "gone.py", "link_kind": "path", "resolution": "target_missing"},
                    {"target": "ok.py", "link_kind": "path", "resolution": "resolved"},
                    {"target": "x", "link_kind": "path", "resolution": "unresolved"},
                    {"target": "y", "link_kind": "path", "resolution": "ambiguous"},
                ]},
                {"file": PAGES[1], "outbound": []},
            ]},
            "provenance": {"source_name": "claude-klabauter"},
        }

    def __call__(self, url, payload, timeout_s):
        door = payload["params"]["name"]
        self.calls.append(door)
        assert url == "http://127.0.0.1:8767/mcp"
        if door == "project_list_sources":
            if self.exc:
                raise self.exc
            return {"data": {"resolution": {"git_root": self.git_root or str(self.repo_root)}},
                    "provenance": {"source_name": self.source_name}}
        assert door == "project_doc_links_batch"
        assert payload["params"]["arguments"] == {"paths": PAGES}
        assert timeout_s == pytest.approx(0.7)
        if self.exc:
            raise self.exc
        return self.doc_envelope


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXAMPLE_RETRIEVAL_REPO_HTTP_HOST", raising=False)
    monkeypatch.delenv("EXAMPLE_RETRIEVAL_REPO_HTTP_PORT", raising=False)


def _run(tmp_path, post, costs=None):
    run_dir = tmp_path / RUN_ID
    return fetch(tmp_path, run_dir, PAGES, costs=costs or _costs(), post=post), run_dir


def test_batched_door_yields_dead_paths_from_one_call(tmp_path):
    post = FakePost(tmp_path)
    result, run_dir = _run(tmp_path, post)
    assert contract.validate_structure(result) == []
    assert json.loads((run_dir / "structure.json").read_text(encoding="utf-8")) == result
    need = result["needs"]["dead-paths"]
    assert need["covered"] and need["door"] == "project_doc_links_batch" and need["calls"] == 1
    assert [r["target"] for r in need["payload"]["dead"]] == ["gone.py", "x"]
    assert post.calls == ["project_list_sources", "project_doc_links_batch"]


def test_costs_without_batched_door_is_pending_and_calls_nothing(tmp_path):
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post, _costs(batched=False))
    need = result["needs"]["dead-paths"]
    assert need["covered"] is False and need["reason"] == "batched-doc-links-door-pending"
    assert post.calls == []
    assert contract.validate_structure(result) == []


def test_per_page_door_never_called_even_when_within(tmp_path):
    costs = _costs(batched=False)
    assert costs["doors"]["project_doc_links"]["cost_class"] == "within"
    post = FakePost(tmp_path)
    _run(tmp_path, post, costs)
    assert post.calls == []


def test_batched_door_over_bar_is_pending(tmp_path):
    costs = _costs()
    costs["doors"]["project_doc_links_batch"]["cost_class"] = "over-bar"
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post, costs)
    assert result["needs"]["dead-paths"]["reason"] == "batched-doc-links-door-pending"
    assert post.calls == []


def test_other_needs_written_uncovered_without_a_call(tmp_path):
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post)
    for name in ("system-map", "edges"):
        need = result["needs"][name]
        assert need["covered"] is False and need["calls"] == 0 and need["payload"] is None
        assert need["reason"].startswith("door-over-bar")
    for name in ("file-index", "last-touch"):
        assert result["needs"][name]["covered"] is False
        assert result["needs"][name]["reason"] == REAL_COSTS["needs"][name]["reason"]
    assert "project_architecture_graph" not in post.calls


def test_system_set_disagrees_does_not_block_dead_paths(tmp_path):
    assert REAL_COSTS["system_set"]["result"] == "disagrees"
    result, _ = _run(tmp_path, FakePost(tmp_path))
    assert result["needs"]["dead-paths"]["covered"] is True


def test_foreign_project_identity_raises_and_calls_only_identity(tmp_path):
    post = FakePost(tmp_path, git_root=str(tmp_path / "other"))
    with pytest.raises(StructureUnavailable, match="another project"):
        _run(tmp_path, post)
    assert post.calls == ["project_list_sources"]


@pytest.mark.parametrize("exc", [TimeoutError("t"), OSError("o"), ValueError("v"), KeyError("k")])
def test_identity_probe_failure_raises_structure_unavailable(tmp_path, exc):
    with pytest.raises(StructureUnavailable, match="identity probe failed"):
        _run(tmp_path, FakePost(tmp_path, exc=exc))


def test_envelope_naming_another_project_raises(tmp_path):
    env = {"data": {"results": []}, "provenance": {"source_name": "other"}}
    with pytest.raises(StructureUnavailable, match="another project"):
        _run(tmp_path, FakePost(tmp_path, doc_envelope=env))


def test_cwd_outside_repo_root_raises_and_calls_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    post = FakePost(repo)
    with pytest.raises(StructureUnavailable, match="working directory"):
        fetch(repo, tmp_path / RUN_ID, PAGES, costs=_costs(), post=post)
    assert post.calls == []


def test_cwd_under_repo_root_is_accepted(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    result, _ = _run(tmp_path, FakePost(tmp_path))
    assert result["needs"]["dead-paths"]["covered"]


def test_unreachable_daemon_propagates(tmp_path):
    post = FakePost(tmp_path, exc=StructureUnavailable("rag daemon unreachable"))
    with pytest.raises(StructureUnavailable, match="unreachable"):
        _run(tmp_path, post)


def test_timeout_on_dead_paths_is_covered_false_not_a_raise(tmp_path):
    class TimeoutOnDoor(FakePost):
        def __call__(self, url, payload, timeout_s):
            if payload["params"]["name"] == "project_doc_links_batch":
                self.calls.append("project_doc_links_batch")
                raise TimeoutError("slow")
            return super().__call__(url, payload, timeout_s)

    post = TimeoutOnDoor(tmp_path)
    result, _ = _run(tmp_path, post)
    need = result["needs"]["dead-paths"]
    assert need["covered"] is False and need["reason"] == "door-failed: TimeoutError"
    assert post.calls.count("project_doc_links_batch") == 1


def test_degraded_envelope_is_covered_false(tmp_path):
    env = {"verdict": "source_unspecified", "data": {}, "provenance": {"source_name": "claude-klabauter"}}
    result, _ = _run(tmp_path, FakePost(tmp_path, doc_envelope=env))
    assert result["needs"]["dead-paths"] == {
        "covered": False, "door": "project_doc_links_batch", "calls": 1, "reason": "door-degraded", "payload": None}


def test_address_expands_env_defaults(monkeypatch):
    assert structure._address(REAL_COSTS) == "http://127.0.0.1:8767/mcp"
    monkeypatch.setenv("EXAMPLE_RETRIEVAL_REPO_HTTP_PORT", "9000")
    assert structure._address(REAL_COSTS) == "http://127.0.0.1:9000/mcp"


def test_default_post_refused_connection_is_unavailable():
    with pytest.raises(StructureUnavailable, match="unreachable"):
        structure._default_post("http://127.0.0.1:1/mcp", {"jsonrpc": "2.0"}, 1.0)


def test_import_set_is_stdlib_plus_package():
    code = (
        "import sys; before=set(sys.modules); "
        "import coordinator_core.architecture_survey.structure; "
        "new={m.split('.')[0] for m in set(sys.modules)-before}; "
        "std=set(sys.stdlib_module_names)|{'coordinator_core'}; "
        "print(sorted(m for m in new if m not in std and not m.startswith('_')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[3])
    assert out.stdout.strip() == "[]"
