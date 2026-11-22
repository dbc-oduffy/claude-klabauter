"""Tests for the read-only EM-brief premise check."""

import subprocess

from coordinator_core.ops import _brief_premise as bp
from pathlib import Path
import pytest


def _tree(tmp_path):
    f = tmp_path / "coordinator_core" / "ops" / "real_door.py"
    f.parent.mkdir(parents=True)
    f.write_text("def real_fn():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _by(rows, claim):
    return next(r for r in rows if r["claim"] == claim)


def test_r40_shaped_wrong_door_is_contradicted(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    brief = "Fix it in coordinator_core/ops/wrong_door.py, the door is `real_fn` in coordinator_core/ops/real_door.py."
    rows = bp.check_brief_claims(brief, root)
    assert _by(rows, "coordinator_core/ops/wrong_door.py")["verdict"] == "CONTRADICTED"
    assert _by(rows, "coordinator_core/ops/real_door.py")["verdict"] == "RESOLVES"
    assert _by(rows, "real_fn in coordinator_core/ops/real_door.py")["verdict"] == "RESOLVES"
    out = bp.render_contradicted(rows)
    assert out.startswith("CONTRADICTED: coordinator_core/ops/wrong_door.py")
    assert "real_door" not in out


def test_missing_symbol_and_backslash_path(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    rows = bp.check_brief_claims(r"see `ghost` in coordinator_core\ops\real_door.py", root)
    assert _by(rows, "ghost in coordinator_core\\ops\\real_door.py")["verdict"] == "CONTRADICTED"
    assert _by(rows, "coordinator_core/ops/real_door.py")["verdict"] == "RESOLVES"


def test_hex_ref_uncheckable_and_plain_words_ignored(tmp_path: Path) -> None:
    rows = bp.check_brief_claims("lever-register.md @3749abc and the word deadbeefs plus a/b", tmp_path)
    assert _by(rows, "3749abc")["verdict"] == "UNCHECKABLE"
    assert _by(rows, "3749abc")["kind"] == "ref"
    assert all(r["verdict"] in bp.VERDICTS for r in rows)


def test_no_contradictions_renders_empty(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    rows = bp.check_brief_claims("coordinator_core/ops/real_door.py", root)
    assert bp.render_contradicted(rows) == ""
    assert bp.check_brief_claims("", root) == []


def test_urls_are_not_path_claims(tmp_path: Path) -> None:
    assert bp.check_brief_claims("https://example.com/a/b.md", tmp_path) == []


def test_still_returns_rows_when_subprocess_spawn_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise OSError("spawn denied")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    rows = bp.check_brief_claims("x/y.py @3749abc", _tree(tmp_path))
    assert {r["verdict"] for r in rows} == {"CONTRADICTED", "UNCHECKABLE"}


def _base_world(tmp_path: Path):
    """A repo whose commit holds a.py (with `needle`); the worktree has since lost both the file and the symbol."""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def needle():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("def needle():\n    pass\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    sha = git("rev-parse", "HEAD")
    (tmp_path / "pkg" / "a.py").unlink()
    (tmp_path / "pkg" / "b.py").write_text("def other():\n    pass\n", encoding="utf-8")
    return sha


@pytest.mark.spawns_process
def test_base_ref_resolves_claims_the_worktree_lost(tmp_path: Path) -> None:
    sha = _base_world(tmp_path)
    brief = "edit pkg/a.py and `needle` in pkg/b.py, but pkg/ghost.py and `nope` in pkg/b.py are wrong"
    tree = bp.check_brief_claims(brief, tmp_path)
    assert _by(tree, "pkg/a.py")["verdict"] == "CONTRADICTED"
    assert _by(tree, "needle in pkg/b.py")["verdict"] == "CONTRADICTED"
    rows = bp.check_brief_claims(brief, tmp_path, sha)
    assert _by(rows, "pkg/a.py")["verdict"] == "RESOLVES"
    assert _by(rows, "needle in pkg/b.py")["verdict"] == "RESOLVES"
    assert _by(rows, "pkg/ghost.py")["verdict"] == "CONTRADICTED"
    assert _by(rows, "nope in pkg/b.py")["verdict"] == "CONTRADICTED"
    assert bp.render_contradicted(rows).count("CONTRADICTED:") == 2


@pytest.mark.spawns_process
def test_base_ref_costs_one_spawn_and_none_without_contradiction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import run as git_run

    sha = _base_world(tmp_path)
    calls: list = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda args, **kw: calls.append(args) or real(args, **kw))
    bp.check_brief_claims("pkg/a.py and `needle` in pkg/b.py and pkg/ghost.py", tmp_path, sha)
    assert len(calls) == 1
    calls.clear()
    bp.check_brief_claims("pkg/b.py", tmp_path, sha)
    assert calls == []


@pytest.mark.spawns_process
def test_unresolvable_base_ref_keeps_the_tree_verdict(tmp_path: Path) -> None:
    _base_world(tmp_path)
    rows = bp.check_brief_claims("pkg/a.py", tmp_path, "no-such-ref")
    assert _by(rows, "pkg/a.py")["verdict"] == "CONTRADICTED"
