"""Every release index written is declared and owed to the caller's commit (DR-216 D2(v))."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import release_index, release_tagging
from coordinator_core.ops.release_index import write_release_index
from coordinator_core.session import declared_writes

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    for i, tag in enumerate(("v0.1.0", "v0.2.0", "v0.3.0")):
        rel = tmp_path / release_index.ENTRY_ROOT / "2026-10" / f"e{i}.md"
        rel.parent.mkdir(parents=True, exist_ok=True)
        rel.write_text(f"e{i}\n", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", f"cut {tag}")
        _git(tmp_path, "tag", "-a", tag, "-m", tag)
    return tmp_path


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain", "--", release_index.INDEX_ROOT)


def test_declared_set_equals_written_and_cut_owes_all(repo: Path) -> None:
    with declared_writes.collecting() as declared:
        res = write_release_index(repo)
    written = [w["path"] for w in res["written"]]
    assert len(written) == 3
    assert sorted(Path(p).relative_to(repo).as_posix() if Path(p).is_absolute() else p for p in declared) == sorted(
        written
    )

    for p in written:
        (repo / p).unlink()
    out = release_tagging._index_after_cut(repo, "v0.3.0")
    assert out["commit_owed"] == out["written"] == written
    assert _git(repo, "log", "-1", "--format=%s") == "cut v0.3.0"

    _git(repo, "add", "--", *out["commit_owed"])
    _git(repo, "commit", "-q", "-m", "index")
    assert _porcelain(repo) == ""


def test_report_mode_declares_and_owes_nothing(repo: Path) -> None:
    with declared_writes.collecting() as declared:
        res = write_release_index(repo, report=True)
    assert len(res["written"]) == 3
    assert list(declared) == []
    assert _porcelain(repo) == ""
