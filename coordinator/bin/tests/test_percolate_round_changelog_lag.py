"""Pins `_changelog_lag_line`: a publish round reports carried source commits
the hand-curated mirror CHANGELOG predates, and never blocks or writes it.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_changelog_lag.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    script = _BIN_DIR / "percolate-round.py"
    if not script.is_file():
        pytest.skip("percolate-round.py is not present beside this test", allow_module_level=True)
    spec = importlib.util.spec_from_file_location("percolate_round_changelog_lag", script)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", f"edit {rel}")


@pytest.fixture()
def rows(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    _git(src, "init", "-q")
    _commit(src, "dist/klabauter-toplevel/CHANGELOG.md", "v1\n")
    top = f"top|flat-mirror|{src / 'dist/klabauter-toplevel'}|{dest}"
    engine = f"eng|mirror|{src / 'coordinator_core'}|{dest / 'coordinator_core'}"
    return src, top, engine


def test_changelog_touched_at_head_gives_no_line(rows):
    src, top, engine = rows
    assert _mod._changelog_lag_line([top, engine], [top, engine], src, "HEAD") == ""


def test_two_later_engine_commits_give_n_2(rows):
    src, top, engine = rows
    _commit(src, "coordinator_core/a.py", "1\n")
    _commit(src, "coordinator_core/b.py", "2\n")
    _commit(src, "unrelated/c.py", "3\n")
    line = _mod._changelog_lag_line([engine], [top, engine], src, "HEAD")
    sha = _git(src, "log", "-1", "--format=%h", "--", "dist/klabauter-toplevel/CHANGELOG.md")
    assert line == f"CHANGELOG predates 2 carried source commit(s) since {sha}"


def test_target_without_the_toplevel_makes_no_git_call(rows, monkeypatch):
    src, top, engine = rows
    _commit(src, "coordinator_core/a.py", "1\n")
    calls = []
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: calls.append(a) or pytest.fail("git called"))
    assert _mod._changelog_lag_line([engine], [engine], src, "HEAD") == ""
    other = f"other|mirror|{src / 'coordinator_core'}|{src.parent / 'elsewhere'}"
    assert _mod._changelog_lag_line([other], [top, other], src, "HEAD") == ""
    assert calls == []
