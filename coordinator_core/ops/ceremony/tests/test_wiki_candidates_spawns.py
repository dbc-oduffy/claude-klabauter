"""Tests for "ceremony.wiki_candidates": registry, window, selection, ranking, caps, spawn shape.

Every case drives the registered handler against a real throwaway git repo under tmp_path, with
ledger rows appended through `plan_completion_ledger` (the production writer's row shape).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.wiki_candidates as wc
from coordinator_core import plan_completion_ledger as ledger

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP = "ceremony.wiki_candidates"
_WIKI = "docs" + "/wiki/"
_NOW = datetime.now(timezone.utc)


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


def _plan(repo: Path, name: str, *, writes=("src/a.py",), tshirt=None, body: str = "body") -> str:
    """Write and commit one plan, append its ledger row; returns the plan path."""
    rel = f"docs/plans/2026-11/{name}.md"
    sizing = ""
    if tshirt:
        sz = repo / "state" / "sizings" / f"{name}.yaml"
        sz.parent.mkdir(parents=True, exist_ok=True)
        sz.write_text(f"estimate:\n  tshirt: {tshirt}\n  provisional: true\n", encoding="utf-8")
        sizing = f'sizing_object: "state/sizings/{name}.yaml"\n'
    items = "".join(f"    - {w}\n" for w in writes)
    text = (
        f"---\ntitle: {name}\nstatus: landed\n{sizing}---\n\n# {name}\n\n{body}\n\n"
        f"```yaml plan-tasks\n- id: C1\n  writes:\n{items}  body: x\n```\n"
    )
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    sha = _commit(repo, name)
    _row(repo, rel, text, sha, tshirt=tshirt, writes=list(writes))
    return rel


def _row(repo, rel, text, sha, *, tshirt=None, writes=(), status: str = "landed", at=None) -> None:
    row = ledger.make_row(
        rel, status, (at or _NOW).strftime("%Y-%m-%dT%H:%M:%SZ"), sha, text.encode("utf-8"),
        tshirt, list(writes),
    )
    ledger.append_row(repo, row)


def _wiki(repo: Path, name: str, cites: str) -> str:
    rel = f"{_WIKI}{name}.md"
    (repo / "docs" / "wiki").mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(f"# {name}\n\nSee `{cites}` for details.\n", encoding="utf-8")
    return rel


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _commit(root, "init")
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE", str(tmp_path / "cache"))
    return root


def _run(repo: Path, params=None) -> dict:
    return wc._ceremony_wiki_candidates(params or {}, repo_root=repo / ".git")


def test_default_window_selects_ledger_plan_and_citing_page(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _wiki(repo, "beta", "src/unrelated.py")
    plan = _plan(repo, "p1", tshirt="L")
    sha = _git(repo, "rev-parse", "HEAD")
    out = _run(repo)
    assert out["schema"] == "wiki-update-candidates" and out["schema_version"] == 1
    assert out["window"] == {"since": None, "head": sha, "source": "default-7d"}
    assert [pg["page"] for pg in out["pages"]] == [_WIKI + "alpha.md"]
    entry = out["pages"][0]["plans"][0]
    assert entry["path"] == plan and entry["commit_sha"] == sha and entry["tshirt"] == "L"
    assert entry["preview"].startswith("# p1") and "status:" not in entry["preview"]
    assert out["preview_chars_total"] == len(entry["preview"])
    assert out["truncated"] == {"pages": 0, "plans": 0}


def test_uncited_plan_and_non_completed_row_are_refused(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "other", writes=("src/zzz.py",))
    text = "---\nstatus: blocked\n---\n\nx\n"
    _row(repo, "docs/plans/2026-11/b.md", text, "a" * 40, writes=["src/a.py"], status="blocked")
    _commit(repo)
    assert _run(repo)["pages"] == []


def test_hub_path_cited_by_many_pages_is_dropped_and_a_narrow_path_still_matches(repo) -> None:
    for i in range(wc.HUB_CITING_PAGES + 1):
        _wiki(repo, f"hub{i}", "src/hub.py")
    _wiki(repo, "narrow", "src/narrow.py")
    _plan(repo, "ph", writes=("src/hub.py",), tshirt="XL")
    _plan(repo, "pn", writes=("src/narrow.py",), tshirt="S")
    pages = _run(repo)["pages"]
    assert [pg["page"] for pg in pages] == [_WIKI + "narrow.md"]
    assert [pl["path"] for pl in pages[0]["plans"]] == ["docs/plans/2026-11/pn.md"]


def test_path_cited_by_exactly_the_threshold_still_matches(repo) -> None:
    for i in range(wc.HUB_CITING_PAGES):
        _wiki(repo, f"pg{i}", "src/a.py")
    _plan(repo, "p1")
    assert len(_run(repo)["pages"]) == wc.HUB_CITING_PAGES


def test_reopened_plan_uses_its_latest_ledger_row(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1", body="first-body")
    rel = _plan(repo, "p1", body="second-body")
    done = _git(repo, "rev-parse", "HEAD")
    entry = _run(repo)["pages"][0]["plans"]
    assert len(entry) == 1 and entry[0]["path"] == rel and entry[0]["commit_sha"] == done
    assert "second-body" in entry[0]["preview"]


def test_deleted_plan_still_previews_from_its_blob(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    plan = _plan(repo, "gone", body="distinctive-body")
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / plan).unlink()
    _commit(repo, "distill")
    entry = _run(repo)["pages"][0]["plans"][0]
    assert entry["commit_sha"] == sha and "distinctive-body" in entry["preview"]


def test_ranking_by_highest_tshirt_then_plan_count_then_page(repo) -> None:
    _wiki(repo, "small", "src/s.py")
    _wiki(repo, "big", "src/b.py")
    _wiki(repo, "many-a", "src/m.py")
    _wiki(repo, "many-b", "src/m.py")
    _plan(repo, "ps", writes=("src/s.py",), tshirt="S")
    _plan(repo, "pb", writes=("src/b.py",), tshirt="XL")
    _plan(repo, "pm1", writes=("src/m.py",), tshirt="S")
    _plan(repo, "pm2", writes=("src/m.py",))
    pages = _run(repo)["pages"]
    assert [pg["page"] for pg in pages] == [
        _WIKI + "big.md", _WIKI + "many-a.md", _WIKI + "many-b.md", _WIKI + "small.md",
    ]
    assert [pl["tshirt"] for pl in pages[1]["plans"]] == ["S", None]


def test_caps_cut_from_the_bottom_and_are_counted(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "PAGES_CAP", 2)
    monkeypatch.setattr(wc, "PLANS_CAP", 3)
    for i in range(4):
        _wiki(repo, f"pg{i}", f"src/f{i}.py")
        _plan(repo, f"q{i}", writes=(f"src/f{i}.py", "src/shared.py"), tshirt="M")
    _wiki(repo, "shared", "src/shared.py")
    _commit(repo)
    out = _run(repo)
    assert [pg["page"] for pg in out["pages"]] == [_WIKI + "shared.md"]
    assert len(out["pages"][0]["plans"]) == 3
    assert out["truncated"] == {"pages": 4, "plans": 5}
    assert out["preview_chars_total"] == sum(len(pl["preview"]) for pg in out["pages"] for pl in pg["plans"])


def test_preview_is_cut_at_preview_chars(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "PREVIEW_CHARS", 50)
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1", body="x" * 500)
    assert len(_run(repo)["pages"][0]["plans"][0]["preview"]) == 50


def test_window_is_by_completed_at_default_seven_days(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    text = "---\nstatus: landed\n---\n\n# old\n"
    old = _NOW - timedelta(days=30)
    _row(repo, "docs/plans/2026-11/old.md", text, "b" * 40, writes=["src/a.py"], at=old)
    _commit(repo)
    assert _run(repo)["pages"] == []
    assert len(_run(repo, {"since": old.strftime("%Y-%m-%d")})["pages"]) == 1


def test_since_ledger_sha_selects_rows_after_it(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "old")
    base = _git(repo, "rev-parse", "HEAD")
    _plan(repo, "new", tshirt="M")
    out = _run(repo, {"since": base})
    assert out["window"]["source"] == "since-param" and out["window"]["since"] == base
    assert [pl["path"] for pl in out["pages"][0]["plans"]] == ["docs/plans/2026-11/new.md"]


def test_unknown_since_sha_and_garbage_fall_back_to_default_window(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1")
    for bad in ("deadbeef" * 5, "not-a-date"):
        out = _run(repo, {"since": bad})
        assert out["window"]["source"] == "default-7d" and out["window"]["since"] is None
        assert len(out["pages"]) == 1


def test_ack_head_mapped_to_its_ledger_row_is_the_default_window_and_is_not_advanced(repo, tmp_path: Path) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "old")
    ack = _git(repo, "rev-parse", "HEAD")
    _plan(repo, "new")
    cache = tmp_path / "cache" / "update-docs-worklist" / "state.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"ack_head": ack}), encoding="utf-8")
    before = cache.read_bytes()
    out = _run(repo)
    assert out["window"]["source"] == "ack_head" and out["window"]["since"] == ack
    assert [pl["path"] for pl in out["pages"][0]["plans"]] == ["docs/plans/2026-11/new.md"]
    assert cache.read_bytes() == before


def test_ack_head_absent_from_the_ledger_falls_back_to_default_window(repo, tmp_path: Path) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1")
    cache = tmp_path / "cache" / "update-docs-worklist" / "state.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"ack_head": "c" * 40}), encoding="utf-8")
    out = _run(repo)
    assert out["window"]["source"] == "default-7d" and len(out["pages"]) == 1


def test_no_history_walk_and_constant_spawn_kinds(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _wiki(repo, "alpha", "src/a.py")
    for i in range(3):
        _plan(repo, f"p{i}")
    seen = []
    real = wc.run_git

    def spy(args, **kw):
        seen.append(args[0])
        return real(args, **kw)

    monkeypatch.setattr(wc, "run_git", spy)
    _run(repo)
    assert sorted(seen) == ["cat-file", "rev-parse"]


def test_empty_ledger_returns_an_empty_reply_without_grep_or_catfile(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    real = wc.run_git
    monkeypatch.setattr(wc, "run_git", lambda args, **kw: (seen.append(args[0]), real(args, **kw))[1])
    out = _run(repo)
    assert out["pages"] == [] and seen == ["rev-parse", "ls-tree"]


def test_repo_root_param_queries_another_repo_and_rejects_a_missing_one(repo, tmp_path: Path) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1")
    out = wc._ceremony_wiki_candidates({"repo_root": str(repo)}, repo_root=tmp_path / "no" / ".git")
    assert len(out["pages"]) == 1
    with pytest.raises(ValueError):
        wc._ceremony_wiki_candidates({"repo_root": str(tmp_path / "missing")}, repo_root=None)
    with pytest.raises(ValueError):
        wc._ceremony_wiki_candidates({}, repo_root=None)


def test_dispatch_key_cwd_subdir_linked_worktree_and_param_give_identical_output(repo, tmp_path: Path) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1", tshirt="L")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    sub = repo / "src"
    sub.mkdir()

    via_common_dir = wc._ceremony_wiki_candidates({}, repo_root=repo / ".git")
    via_worktree_root = wc._ceremony_wiki_candidates({}, repo_root=repo)
    via_subdir = wc._ceremony_wiki_candidates({}, repo_root=sub)
    via_linked = wc._ceremony_wiki_candidates({}, repo_root=linked)
    via_param = wc._ceremony_wiki_candidates({"repo_root": str(repo)}, repo_root=None)
    via_param_linked = wc._ceremony_wiki_candidates({"repo_root": str(linked)}, repo_root=None)

    assert via_common_dir["pages"] and via_common_dir["preview_chars_total"] > 0
    for other in (via_worktree_root, via_subdir, via_linked, via_param, via_param_linked):
        assert other == via_common_dir


def _scan_plan(repo: Path, name: str, *, status: str = "landed", day: datetime = _NOW) -> str:
    """Commit one dated plan with no ledger row (the ledger-less repo shape); returns its path."""
    rel = f"docs/plans/2026-11/{day.strftime('%Y-%m-%d')}-{name}.md"
    text = (
        f"---\ntitle: {name}\nstatus: {status}\n---\n\n# {name}\n\nbody\n\n"
        "```yaml plan-tasks\n- id: C1\n  writes:\n    - src/a.py\n  body: x\n```\n"
    )
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    _commit(repo, name)
    return rel


def test_empty_reason_ledger_missing_only_when_the_git_scan_finds_nothing(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _scan_plan(repo, "draft", status="draft")
    _scan_plan(repo, "stale", day=_NOW - timedelta(days=60))
    out = _run(repo)
    assert out["pages"] == [] and out["empty_reason"] == "ledger_missing"


def test_ledger_less_repo_selects_completed_plan_from_git_by_the_citation_rule(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _wiki(repo, "beta", "src/unrelated.py")
    plan = _scan_plan(repo, "done")
    _scan_plan(repo, "draft", status="draft")
    sha = _git(repo, "rev-parse", "HEAD")
    assert not (repo / ledger.LEDGER).exists()
    seen = []
    real = wc.run_git
    monkeypatch.setattr(wc, "run_git", lambda args, **kw: (seen.append(args[0]), real(args, **kw))[1])
    out = _run(repo)
    assert [pg["page"] for pg in out["pages"]] == [_WIKI + "alpha.md"]
    assert [pl["path"] for pl in out["pages"][0]["plans"]] == [plan]
    assert out["pages"][0]["plans"][0]["commit_sha"] == sha
    assert out["empty_reason"] is None
    assert sorted(seen) == ["cat-file", "ls-tree", "rev-parse"]


def test_empty_reason_ledger_empty_in_window(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    text = "---\nstatus: landed\n---\n\nx\n"
    old = _NOW - timedelta(days=30)
    _row(repo, "docs/plans/2026-11/old.md", text, "a" * 40, writes=["src/a.py"], at=old)
    out = _run(repo)
    assert out["pages"] == [] and out["empty_reason"] == "ledger_empty_in_window"


def test_empty_reason_no_citing_page(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "other", writes=("src/zzz.py",))
    out = _run(repo)
    assert out["pages"] == [] and out["empty_reason"] == "no_citing_page"


def test_empty_reason_hub_cut(repo) -> None:
    for i in range(wc.HUB_CITING_PAGES + 1):
        _wiki(repo, f"hub{i}", "src/hub.py")
    _plan(repo, "ph", writes=("src/hub.py",))
    out = _run(repo)
    assert out["pages"] == [] and out["empty_reason"] == "hub_cut"


def test_empty_reason_is_null_when_pages_non_empty(repo) -> None:
    _wiki(repo, "alpha", "src/a.py")
    _plan(repo, "p1")
    out = _run(repo)
    assert out["pages"] and out["empty_reason"] is None
