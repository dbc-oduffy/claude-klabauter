"""Tests for "ceremony.wiki_update_candidates": registry, window, selection, ranking, caps.

Every case drives the registered handler against a real throwaway git repo under tmp_path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.wiki_update_candidates as wuc
from coordinator_core.ipc import _REGISTRY

_OP = "ceremony.wiki_update_candidates"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return res.stdout.strip()


def _commit(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _plan(repo: Path, name: str, *, status="landed", writes=("src/a.py",), tshirt=None, body="body") -> str:
    rel = f"docs/plans/2026-11/{name}.md"
    sizing = ""
    if tshirt:
        sz = repo / "state" / "sizings" / f"{name}.yaml"
        sz.parent.mkdir(parents=True, exist_ok=True)
        sz.write_text(f"estimate:\n  tshirt: {tshirt}\n  provisional: true\n", encoding="utf-8")
        sizing = f'sizing_object: "state/sizings/{name}.yaml"\n'
    items = "".join(f"    - {w}\n" for w in writes)
    text = (
        f"---\ntitle: {name}\nstatus: {status}\n{sizing}---\n\n# {name}\n\n{body}\n\n"
        f"```yaml plan-tasks\n- id: C1\n  writes:\n{items}  body: x\n```\n"
    )
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _wiki(repo: Path, name: str, cites: str) -> str:
    rel = f"docs/wiki/{name}.md"
    (repo / "docs" / "wiki").mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(f"# {name}\n\nSee `{cites}` for details.\n", encoding="utf-8")
    return rel


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _commit(root, "init")
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE", str(tmp_path / "cache"))
    return root


def _run(repo: Path, params=None) -> dict:
    return wuc._ceremony_wiki_update_candidates(params or {}, repo_root=repo / ".git")


def test_op_registered():
    assert _OP in _REGISTRY


def test_default_window_selects_completed_plan_and_citing_page(repo):
    _wiki(repo, "alpha", "src/a.py")
    _wiki(repo, "beta", "src/unrelated.py")
    plan = _plan(repo, "p1", tshirt="L")
    sha = _commit(repo)
    out = _run(repo)
    assert out["schema"] == "wiki-update-candidates" and out["schema_version"] == 1
    assert out["window"] == {"since": None, "head": sha, "source": "default-7d"}
    assert [pg["page"] for pg in out["pages"]] == ["docs/wiki/alpha.md"]
    entry = out["pages"][0]["plans"][0]
    assert entry["path"] == plan and entry["commit_sha"] == sha and entry["tshirt"] == "L"
    assert entry["preview"].startswith("---\ntitle: p1")
    assert out["preview_chars_total"] == len(entry["preview"])
    assert out["truncated"] == {"pages": 0, "plans": 0}


def test_non_complete_plan_and_uncited_plan_are_refused(repo):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "draft", status="executing")
    _plan(repo, "other", writes=("src/zzz.py",))
    _commit(repo)
    assert _run(repo)["pages"] == []


@pytest.mark.parametrize("status", ["landed", "implemented", "closed_partial", "closed"])
def test_each_completed_status_yields_a_page_and_open_statuses_do_not(repo, status):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "done", status=status)
    _plan(repo, "open1", status="draft", writes=("src/a.py",))
    _plan(repo, "open2", status="executing", writes=("src/a.py",))
    _commit(repo)
    plans = _run(repo)["pages"][0]["plans"]
    assert [pl["path"] for pl in plans] == ["docs/plans/2026-11/done.md"]


def test_completed_statuses_are_members_of_the_plan_schema_status_enum():
    schema_path = Path(wuc.__file__).parents[2] / "frontmatter" / "schemas" / "plan.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert wuc.COMPLETED_STATUSES <= set(schema["properties"]["status"]["enum"])
    assert "complete" not in wuc.COMPLETED_STATUSES


def test_hub_path_cited_by_many_pages_is_dropped_and_a_narrow_path_still_matches(repo):
    for i in range(wuc.HUB_CITING_PAGES + 1):
        _wiki(repo, f"hub{i}", "src/hub.py")
    _wiki(repo, "narrow", "src/narrow.py")
    _plan(repo, "ph", writes=("src/hub.py",), tshirt="XL")
    _plan(repo, "pn", writes=("src/narrow.py",), tshirt="S")
    _commit(repo)
    pages = _run(repo)["pages"]
    assert [pg["page"] for pg in pages] == ["docs/wiki/narrow.md"]
    assert [pl["path"] for pl in pages[0]["plans"]] == ["docs/plans/2026-11/pn.md"]


def test_path_cited_by_exactly_the_threshold_still_matches(repo):
    for i in range(wuc.HUB_CITING_PAGES):
        _wiki(repo, f"pg{i}", "src/a.py")
    _plan(repo, "p1")
    _commit(repo)
    assert len(_run(repo)["pages"]) == wuc.HUB_CITING_PAGES


def test_plan_completed_by_a_later_commit_is_attributed_to_that_commit(repo):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1", status="executing")
    _commit(repo)
    _plan(repo, "p1", status="implemented")
    done = _commit(repo)
    out = _run(repo)
    assert out["pages"][0]["plans"][0]["commit_sha"] == done


def test_deleted_plan_still_previews_from_its_completing_commit(repo):
    _wiki(repo, "alpha", "src/a.py")
    plan = _plan(repo, "gone", body="distinctive-body")
    sha = _commit(repo)
    (repo / plan).unlink()
    _commit(repo, "distill")
    entry = _run(repo)["pages"][0]["plans"][0]
    assert entry["commit_sha"] == sha and "distinctive-body" in entry["preview"]


def test_ranking_by_highest_tshirt_then_plan_count_then_page(repo):
    _wiki(repo, "small", "src/s.py")
    _wiki(repo, "big", "src/b.py")
    _wiki(repo, "many-a", "src/m.py")
    _wiki(repo, "many-b", "src/m.py")
    _plan(repo, "ps", writes=("src/s.py",), tshirt="S")
    _plan(repo, "pb", writes=("src/b.py",), tshirt="XL")
    _plan(repo, "pm1", writes=("src/m.py",), tshirt="S")
    _plan(repo, "pm2", writes=("src/m.py",))
    _commit(repo)
    pages = _run(repo)["pages"]
    assert [pg["page"] for pg in pages] == [
        "docs/wiki/big.md", "docs/wiki/many-a.md", "docs/wiki/many-b.md", "docs/wiki/small.md",
    ]
    assert [pl["tshirt"] for pl in pages[1]["plans"]] == ["S", None]


def test_caps_cut_from_the_bottom_and_are_counted(repo, monkeypatch):
    monkeypatch.setattr(wuc, "PAGES_CAP", 2)
    monkeypatch.setattr(wuc, "PLANS_CAP", 3)
    for i in range(4):
        _wiki(repo, f"pg{i}", f"src/f{i}.py")
        _plan(repo, f"q{i}", writes=(f"src/f{i}.py", "src/shared.py"), tshirt="M")
    _wiki(repo, "shared", "src/shared.py")
    _commit(repo)
    out = _run(repo)
    assert [pg["page"] for pg in out["pages"]] == ["docs/wiki/shared.md"]
    assert len(out["pages"][0]["plans"]) == 3
    assert out["truncated"] == {"pages": 4, "plans": 5}
    assert out["preview_chars_total"] == sum(len(pl["preview"]) for pg in out["pages"] for pl in pg["plans"])


def test_preview_is_cut_at_preview_chars(repo, monkeypatch):
    monkeypatch.setattr(wuc, "PREVIEW_CHARS", 50)
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1", body="x" * 500)
    _commit(repo)
    assert len(_run(repo)["pages"][0]["plans"][0]["preview"]) == 50


def test_since_param_bounds_the_window(repo):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "old")
    base = _commit(repo)
    _plan(repo, "new", tshirt="M")
    _commit(repo)
    out = _run(repo, {"since": base})
    assert out["window"]["source"] == "since-param" and out["window"]["since"] == base
    assert [pl["path"] for pl in out["pages"][0]["plans"]] == ["docs/plans/2026-11/new.md"]


def test_unknown_since_falls_back_to_default_window(repo):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1")
    _commit(repo)
    out = _run(repo, {"since": "deadbeef" * 5})
    assert out["window"]["source"] == "default-7d" and out["window"]["since"] is None
    assert len(out["pages"]) == 1


def test_ack_head_in_the_worklist_cache_is_the_default_window_and_is_not_advanced(repo, tmp_path):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "old")
    ack = _commit(repo)
    _plan(repo, "new")
    _commit(repo)
    cache = tmp_path / "cache" / "update-docs-worklist" / "state.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"ack_head": ack}), encoding="utf-8")
    before = cache.read_bytes()
    out = _run(repo)
    assert out["window"]["source"] == "ack_head" and out["window"]["since"] == ack
    assert [pl["path"] for pl in out["pages"][0]["plans"]] == ["docs/plans/2026-11/new.md"]
    assert cache.read_bytes() == before


def test_repo_root_param_queries_another_repo_and_rejects_a_missing_one(repo, tmp_path):
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1")
    _commit(repo)
    out = wuc._ceremony_wiki_update_candidates({"repo_root": str(repo)}, repo_root=tmp_path / "no" / ".git")
    assert len(out["pages"]) == 1
    with pytest.raises(ValueError):
        wuc._ceremony_wiki_update_candidates({"repo_root": str(tmp_path / "missing")}, repo_root=None)
    with pytest.raises(ValueError):
        wuc._ceremony_wiki_update_candidates({}, repo_root=None)


def test_spine_writes_reads_block_and_inline_forms_only_from_plan_tasks():
    text = (
        "writes: [outside.py]\n```yaml plan-tasks\n- id: A\n  writes: [\"a.py\", './b.py']\n"
        "- id: B\n  writes:\n    - c/d.py\n    - 'e.py'\n  writes_under:\n    - skip/\n```\n"
    )
    assert wuc._spine_writes(text) == ["a.py", "b.py", "c/d.py", "e.py"]
