"""structure.fetch: file-index from git, edges from one graph call, dead-paths from a batched door; unfetched branches covered:false."""
from __future__ import annotations

import copy
import json
import urllib.error
from pathlib import Path

import pytest

from coordinator_core.architecture_survey import contract, structure
from coordinator_core.architecture_survey.structure import StructureUnavailable, fetch

REAL_COSTS = json.loads((Path(structure.__file__).parent / "rag_read_costs.json").read_text(encoding="utf-8"))
RUN_ID = "20261112T170000Z-ab12"
PAGES = ["docs/architecture/a.md", "docs/architecture/b.md"]


GRAPH = {"data": {"systems": [{"name": "coordinator_core"}, {"name": "docs"}],
                  "edges": [{"from_system": "docs", "to_system": "coordinator_core", "weight": 3}],
                  "limits": {"excludes": "dynamic imports"}},
         "provenance": {"source_name": "claude-klabauter"}}


def _costs(batched: bool = True, edges: bool = True) -> dict:
    costs = copy.deepcopy(REAL_COSTS)
    if not edges:
        costs["needs"]["edges"]["fetch"] = False
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
        self.graph_envelope = copy.deepcopy(GRAPH)
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
        if door == "project_architecture_graph":
            return self.graph_envelope
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
    assert post.calls == ["project_list_sources", "project_doc_links_batch", "project_architecture_graph"]


def test_costs_without_batched_door_is_pending_and_calls_nothing(tmp_path):
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post, _costs(batched=False, edges=False))
    need = result["needs"]["dead-paths"]
    assert need["covered"] is False and need["reason"] == "batched-doc-links-door-pending"
    assert post.calls == []
    assert contract.validate_structure(result) == []


def test_per_page_door_never_called_even_when_within(tmp_path):
    costs = _costs(batched=False, edges=False)
    assert costs["doors"]["project_doc_links"]["cost_class"] == "within"
    post = FakePost(tmp_path)
    _run(tmp_path, post, costs)
    assert post.calls == []


def test_batched_door_over_bar_is_pending(tmp_path):
    costs = _costs()
    costs["doors"]["project_doc_links_batch"]["cost_class"] = "over-bar"
    costs["needs"]["edges"]["fetch"] = False
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post, costs)
    assert result["needs"]["dead-paths"]["reason"] == "batched-doc-links-door-pending"
    assert post.calls == []


def test_edges_and_system_map_come_from_one_graph_call(tmp_path):
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post)
    assert contract.validate_structure(result) == []
    edges, smap = result["needs"]["edges"], result["needs"]["system-map"]
    assert edges["covered"] and edges["door"] == "project_architecture_graph" and edges["calls"] == 1
    assert edges["payload"]["edges"][0]["weight"] == 3
    assert smap["covered"] and smap["calls"] == 0
    assert [s["name"] for s in smap["payload"]["systems"]] == ["coordinator_core", "docs"]
    assert post.calls.count("project_architecture_graph") == 1


def test_edges_without_the_fetch_opt_in_stay_door_over_bar_and_uncalled(tmp_path):
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post, _costs(edges=False))
    for name in ("system-map", "edges"):
        need = result["needs"][name]
        assert need["covered"] is False and need["calls"] == 0 and need["payload"] is None
        assert need["reason"].startswith("door-over-bar")
    assert "project_architecture_graph" not in post.calls


def test_edges_opt_in_refused_when_caller_cost_over_the_bar(tmp_path):
    costs = _costs()
    costs["doors"]["project_architecture_graph"]["caller_process_ms_median"] = 900
    post = FakePost(tmp_path)
    result, _ = _run(tmp_path, post, costs)
    assert result["needs"]["edges"]["covered"] is False
    assert "project_architecture_graph" not in post.calls


def test_degraded_graph_is_uncovered_not_a_raise(tmp_path):
    post = FakePost(tmp_path)
    post.graph_envelope = {"verdict": "warming", "data": {}, "provenance": {"source_name": "claude-klabauter"}}
    result, _ = _run(tmp_path, post)
    assert result["needs"]["edges"]["covered"] is False and result["needs"]["edges"]["reason"] == "door-degraded"
    assert result["needs"]["system-map"]["covered"] is False


def test_graph_envelope_naming_another_project_raises(tmp_path):
    post = FakePost(tmp_path)
    post.graph_envelope = {"data": {}, "provenance": {"source_name": "other"}}
    with pytest.raises(StructureUnavailable, match="another project"):
        _run(tmp_path, post)


def test_last_touch_stays_uncovered_with_its_recorded_reason(tmp_path):
    result, _ = _run(tmp_path, FakePost(tmp_path))
    need = result["needs"]["last-touch"]
    assert need["covered"] is False and need["reason"] == REAL_COSTS["needs"]["last-touch"]["reason"]


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_file_index_uncovered_when_the_recorded_atlas_is_unreadable(tmp_path):
    result = fetch(tmp_path, tmp_path / RUN_ID, PAGES, costs=_costs(batched=False, edges=False))
    need = result["needs"]["file-index"]
    assert need["covered"] is False and need["payload"] is None and need["reason"]


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


def test_default_post_refused_connection_is_unavailable(monkeypatch):
    # A real closed port is not refused promptly on Windows (SYN retries run ~2s, past the
    # timeout, surfacing TimeoutError); inject the refusal so the mapping is tested, not the OS.
    def _refuse(*_a, **_k):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(structure.urllib.request, "urlopen", _refuse)
    with pytest.raises(StructureUnavailable, match="unreachable"):
        structure._default_post("http://127.0.0.1:1/mcp", {"jsonrpc": "2.0"}, 1.0)


def test_edges_only_fetch_names_its_repo_and_skips_the_cwd_identity_probe(tmp_path):
    seen: list[dict] = []

    class GraphOnly(FakePost):
        def __call__(self, url, payload, timeout_s):
            seen.append(payload["params"]["arguments"])
            self.calls.append(payload["params"]["name"])
            env = copy.deepcopy(GRAPH)
            env["provenance"]["source_name"] = tmp_path.name
            return env

    post = GraphOnly(tmp_path)
    result, _ = _run(tmp_path, post, _costs(batched=False))
    assert post.calls == ["project_architecture_graph"]
    assert seen == [{"repo": tmp_path.name}]
    assert result["needs"]["edges"]["covered"] is True


def test_edges_only_fetch_refuses_an_envelope_for_another_project(tmp_path):
    class Foreign(FakePost):
        def __call__(self, url, payload, timeout_s):
            return copy.deepcopy(GRAPH)

    with pytest.raises(StructureUnavailable, match="another project"):
        _run(tmp_path, Foreign(tmp_path), _costs(batched=False))
