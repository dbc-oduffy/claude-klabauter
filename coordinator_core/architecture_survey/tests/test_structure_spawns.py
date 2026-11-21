"""structure.fetch: file-index from git, edges from one graph call, dead-paths from a batched door; unfetched branches covered:false."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.architecture_survey import structure
from coordinator_core.architecture_survey.structure import fetch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXAMPLE_RETRIEVAL_REPO_HTTP_HOST", raising=False)
    monkeypatch.delenv("EXAMPLE_RETRIEVAL_REPO_HTTP_PORT", raising=False)


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_file_index_is_tracked_files_only_one_spawn_under_the_bar(tmp_path, monkeypatch):
    import time

    from coordinator_core.git import run as git_run

    spawns: list[list[str]] = []
    real = git_run.run_git

    def counting(argv, *a, **kw):
        spawns.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(git_run, "run_git", counting)
    start = time.process_time()
    result = fetch(REPO_ROOT, tmp_path / RUN_ID, PAGES, costs=_costs(batched=False, edges=False))
    elapsed_ms = (time.process_time() - start) * 1000
    need = result["needs"]["file-index"]
    assert need["covered"] is True and need["calls"] == 0
    rows = need["payload"]
    assert rows and all(set(r) == {"path", "system"} for r in rows)
    tracked = set(subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True, text=True,
                                 check=True).stdout.splitlines())
    assert {r["path"] for r in rows} <= tracked
    assert not [r for r in rows if "__pycache__" in r["path"] or ".mypy_cache" in r["path"]]
    assert len(spawns) == 1 and spawns[0][-2:] == ["ls-files", "-z"]
    assert elapsed_ms < 500, elapsed_ms


def test_file_index_never_lists_an_untracked_cache_dir(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "architecture").mkdir(parents=True)
    src = (REPO_ROOT / "docs" / "architecture" / "file-index.md").read_text(encoding="utf-8")
    (repo / "docs" / "architecture" / "file-index.md").write_text(src, encoding="utf-8")
    (repo / "coordinator" / "bin" / ".mypy_cache").mkdir(parents=True)
    (repo / "coordinator" / "bin" / ".mypy_cache" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "coordinator" / "bin" / "tool.py").write_text("x = 1\n", encoding="utf-8")
    for argv in (["init", "-q"], ["add", "docs", "coordinator/bin/tool.py"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    result = fetch(repo, tmp_path / RUN_ID, PAGES, costs=_costs(batched=False, edges=False))
    paths = {r["path"] for r in result["needs"]["file-index"]["payload"]}
    assert "coordinator/bin/tool.py" in paths
    assert not [p for p in paths if ".mypy_cache" in p]


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
