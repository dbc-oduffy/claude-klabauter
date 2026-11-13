"""Tests for "ceremony.plan_wiki_map": registry, view path, git-fallback mapping, the spawn
ceiling (constant in plan count) and the DR-344 500ms process-time bar at default params.

`weekly_doc_files.subprocess.run` is replaced by a counting git double, so nothing here
spawns a process.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.plan_wiki_map as pwm
from coordinator_core.ipc import _REGISTRY
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import weekly_doc_files as wdf
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_OP = "ceremony.plan_wiki_map"


class _FakeGit:
    """Counting `subprocess.run` double answering log, diff-tree and cat-file --batch."""

    def __init__(self, plans: dict[str, str]):
        self.plans = plans  # plan path -> text (deleted at its own sha)
        self.shas = {p: f"{i:040x}" for i, p in enumerate(sorted(plans), start=1)}
        self.calls: list[list[str]] = []

    def __call__(self, argv, input=None, **kwargs):
        sub = argv[3]
        self.calls.append(list(argv))
        if sub == "log":
            out = "".join(f"{sha}\tsubject {p}\n" for p, sha in self.shas.items())
            return subprocess.CompletedProcess(argv, 0, out, "")
        if sub == "diff-tree":
            by_sha = {sha: p for p, sha in self.shas.items()}
            out = "".join(f"{sha}\n{by_sha[sha]}\n" for sha in input.split())
            return subprocess.CompletedProcess(argv, 0, out, "")
        assert sub == "cat-file"
        out = b""
        for ref in input.decode().split():
            rev, _, plan = ref.partition(":")
            text = self.plans.get(plan) if rev.endswith("^") else None
            if text is None:
                out += f"{ref} missing\n".encode()
            else:
                body = text.encode()
                out += f"{'a' * 40} blob {len(body)}\n".encode() + body + b"\n"
        return subprocess.CompletedProcess(argv, 0, out, b"")


def _repo(tmp_path: Path, pages: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    wiki = root / "docs" / "wiki"
    wiki.mkdir(parents=True)
    for name, text in pages.items():
        (wiki / name).write_text(text, encoding="utf-8")
    return root


def _plan_text(*writes: str) -> str:
    return "tasks:\n- id: C1\n  writes: [" + ", ".join(writes) + "]\n" + "body " * 4000


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_PLAN_WIKI_MAP_CACHE", str(tmp_path / "cache"))


def _fallback(monkeypatch, plans):
    git = _FakeGit(plans)
    monkeypatch.setattr(wdf.subprocess, "run", git)
    return git


def test_op_registered_with_scope_and_module():
    assert _OP in _REGISTRY
    assert OP_KEY_SCOPE[_OP] == "common_dir"
    assert OP_MODULE_MAP[_OP] == "coordinator_core.ops.ceremony.plan_wiki_map"


def test_repo_root_none_raises():
    with pytest.raises(ValueError):
        pwm._ceremony_plan_wiki_map({}, repo_root=None)


def test_fallback_maps_touched_path_to_wiki_pages_and_filters_unimpactful(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a.md": "see coordinator_core/foo.py here", "b.md": "unrelated"})
    plans = {
        "docs/plans/p-hit.md": _plan_text("coordinator_core/foo.py"),
        "docs/plans/p-miss.md": _plan_text("coordinator_core/other.py"),
        "docs/plans/p-noscope.md": "no spine rows",
    }
    _fallback(monkeypatch, plans)
    out = pwm.plan_wiki_map("2026-11-01", "2026-11-14", root=root)
    assert out["schema_version"] == 1
    assert out["source"] == "git-fallback"
    assert out["deleted_plans"] == "unavailable_in_fallback"
    assert [p["plan"] for p in out["plans"]] == ["docs/plans/p-hit.md"]
    item = out["plans"][0]
    assert item["wiki_pages"] == ["docs/wiki/a.md"]
    assert item["deleted"] is True
    assert 0 < len(item["preview"]) <= wdf.PREVIEW_CAP
    assert out["truncated"] is False


def test_max_plans_truncates_in_plan_path_order(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a.md": "coordinator_core/foo.py"})
    plans = {f"docs/plans/p{i}.md": _plan_text("coordinator_core/foo.py") for i in range(5)}
    _fallback(monkeypatch, plans)
    out = pwm.plan_wiki_map("2026-11-01", "2026-11-14", 2, root=root)
    assert [p["plan"] for p in out["plans"]] == ["docs/plans/p0.md", "docs/plans/p1.md"]
    assert out["truncated"] is True


def test_candidate_cap_clips_the_window_and_reports_truncated(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a.md": "coordinator_core/foo.py"})
    plans = {f"docs/plans/p{i}.md": _plan_text("coordinator_core/foo.py") for i in range(5)}
    _fallback(monkeypatch, plans)
    monkeypatch.setattr(pwm, "MAX_CANDIDATES", 3)
    out = pwm.plan_wiki_map("2026-11-01", "2026-11-14", root=root)
    assert [p["plan"] for p in out["plans"]] == [f"docs/plans/p{i}.md" for i in range(3)]
    assert out["truncated"] is True


def _view_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE plans_completed (plan, sha, summary, sizing, ranked, completed_at);
        CREATE TABLE plan_footprint (plan, path);
        CREATE TABLE paths_to_docs (path, doc);
        CREATE TABLE plan_preview (plan, sha, preview);
        INSERT INTO plans_completed VALUES
          ('docs/plans/big.md', 's1', 'big one', 'L', 1, '2026-11-10'),
          ('docs/plans/small.md', 's2', 'small one', 'S', 2, '2026-11-11'),
          ('docs/plans/mapped.md', 's3', 'mapped', 'S', 3, '2026-11-12'),
          ('docs/plans/old.md', 's4', 'outside window', 'XL', 4, '2026-10-01');
        INSERT INTO plan_footprint VALUES ('docs/plans/mapped.md', 'x/y.py'), ('docs/plans/small.md', 'z.py');
        INSERT INTO paths_to_docs VALUES
          ('x/y.py', 'docs/wiki/w.md'), ('x/y.py', 'docs/atlas/sys.md');
        INSERT INTO plan_preview VALUES
          ('docs/plans/big.md', 's1', 'BIG'), ('docs/plans/mapped.md', 's3', 'MAPPED');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_view_path_reads_only_views_no_git(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("git spawned on the view path")

    monkeypatch.setattr(wdf.subprocess, "run", _boom)
    root = _repo(tmp_path, {})
    db = _view_db(tmp_path / "doc_files.sqlite")
    out = pwm.plan_wiki_map("2026-11-01", "2026-11-14", root=root, index_path=db)
    assert out["source"] == "doc_files"
    assert "deleted_plans" not in out
    assert [p["plan"] for p in out["plans"]] == ["docs/plans/big.md", "docs/plans/mapped.md"]
    big, mapped = out["plans"]
    assert big["sizing"] == "L" and big["wiki_pages"] == [] and big["preview"] == "BIG"
    assert big["completing_sha"] == "s1"
    assert mapped["wiki_pages"] == ["docs/wiki/w.md"]
    assert mapped["atlas_systems"] == ["docs/atlas/sys.md"]
    assert mapped["preview"] == "MAPPED"
    assert "atlas_systems" not in big


def test_200_plan_fixture_spawn_count_constant_under_bar_and_deterministic(tmp_path, monkeypatch):
    pages = {f"page{i}.md": f"covers coordinator_core/mod{i}.py and docs/x{i}.md" for i in range(228)}
    root = _repo(tmp_path, pages)
    plans = {
        f"docs/plans/2026-11/plan-{i:03d}.md": _plan_text(f"coordinator_core/mod{i}.py")
        for i in range(200)
    }
    git = _fallback(monkeypatch, plans)
    first = pwm.plan_wiki_map("2026-11-01", "2026-11-14", 50, root=root)  # warms the wiki cache
    assert [c[3] for c in git.calls] == ["log", "diff-tree", "cat-file"]
    assert len(first["plans"]) == 50 and first["truncated"] is True

    git.calls.clear()
    started = time.process_time()
    second = pwm.plan_wiki_map("2026-11-01", "2026-11-14", 50, root=root)
    elapsed = time.process_time() - started
    assert len(git.calls) <= 3
    assert elapsed < 0.5
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert all(len(p["preview"]) <= wdf.PREVIEW_CAP for p in second["plans"])


def test_handler_default_params_runs_against_main_worktree(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a.md": "coordinator_core/foo.py"})
    (root / ".git").mkdir()
    _fallback(monkeypatch, {"docs/plans/p.md": _plan_text("coordinator_core/foo.py")})
    out = pwm._ceremony_plan_wiki_map({}, repo_root=root / ".git")
    assert out["schema_version"] == 1
    assert out["plans"][0]["wiki_pages"] == ["docs/wiki/a.md"]
