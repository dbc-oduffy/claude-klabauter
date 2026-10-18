"""Tests for coordinator_core.ops.release_index."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import release_index
from coordinator_core.ops.release_index import ReleaseIndexError, released_names, write_release_index
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


def _entry(root: Path, month: str, name: str) -> str:
    rel = f"{release_index.ENTRY_ROOT}/{month}/{name}"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: pending-release\n---\n{name}\n", encoding="utf-8")
    return rel


def _tag(root: Path, tag: str, *entries: tuple[str, str]) -> None:
    for month, name in entries:
        _entry(root, month, name)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", f"cut {tag}")
    _git(root, "tag", "-a", tag, "-m", tag)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def _index(root: Path, tag: str) -> dict:
    return yaml.safe_load((root / release_index.INDEX_ROOT / f"{tag}.yaml").read_text(encoding="utf-8"))


def test_index_lists_exactly_names_first_held_by_its_tag(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"), ("2026-10", "b.md"))
    result = write_release_index(repo)
    assert [w["tag"] for w in result["written"]] == ["v0.1.0"]
    doc = _index(repo, "v0.1.0")
    assert doc["tag"] == "v0.1.0"
    assert doc["sha"] == _git(repo, "rev-parse", "v0.1.0^{commit}")
    assert doc["entries"] == [
        "archive/completed/2026-10/a.md",
        "archive/completed/2026-10/b.md",
    ]


def test_later_tag_lists_only_new_names(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    _tag(repo, "v0.2.0", ("2026-10", "b.md"))
    write_release_index(repo)
    assert _index(repo, "v0.1.0")["entries"] == ["archive/completed/2026-10/a.md"]
    assert _index(repo, "v0.2.0")["entries"] == ["archive/completed/2026-10/b.md"]


def test_existing_index_is_never_rewritten(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    write_release_index(repo)
    path = repo / release_index.INDEX_ROOT / "v0.1.0.yaml"
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    result = write_release_index(repo)
    assert result["written"] == []
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_index_with_different_sha_is_refused_as_retargeted_before_any_write(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    write_release_index(repo)
    _tag(repo, "v0.2.0", ("2026-10", "b.md"))
    _git(repo, "tag", "-f", "-a", "v0.1.0", "-m", "moved", "HEAD")
    with pytest.raises(ReleaseIndexError) as err:
        write_release_index(repo)
    assert err.value.reason == "retargeted"
    assert not (repo / release_index.INDEX_ROOT / "v0.2.0.yaml").exists()


def test_report_mode_writes_nothing(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    result = write_release_index(repo, report=True)
    assert result["report"] is True
    assert [w["tag"] for w in result["written"]] == ["v0.1.0"]
    assert not (repo / release_index.INDEX_ROOT).exists()


def test_tag_with_slash_is_skipped_as_bad_tag(repo: Path) -> None:
    _tag(repo, "x/v0.1.0", ("2026-10", "a.md"))
    result = write_release_index(repo, tag_prefix="x/")
    assert result["written"] == []
    assert result["skipped"] == [{"tag": "x/v0.1.0", "reason": "bad-tag"}]


def test_entry_moved_between_month_dirs_stays_released(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-09", "a.md"))
    (repo / "archive/completed/2026-10").mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "archive/completed/2026-09/a.md", "archive/completed/2026-10/a.md")
    _git(repo, "commit", "-q", "-m", "move")
    _git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    write_release_index(repo)
    assert _index(repo, "v0.2.0")["entries"] == []
    assert released_names(repo)["a.md"] == "v0.1.0"


def test_released_names_maps_name_to_tag_and_opens_no_entry_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    _tag(repo, "v0.2.0", ("2026-10", "b.md"))
    write_release_index(repo)
    opened: list[str] = []
    real_open = Path.open
    real_read_text = Path.read_text

    def spy_open(self: Path, *a, **k):
        opened.append(self.as_posix())
        return real_open(self, *a, **k)

    def spy_read_text(self: Path, *a, **k):
        opened.append(self.as_posix())
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    assert released_names(repo) == {"a.md": "v0.1.0", "b.md": "v0.2.0"}
    assert not [p for p in opened if p.startswith(f"{repo.as_posix()}/{release_index.ENTRY_ROOT}/")]


def test_written_path_is_declared(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    with declared_writes.collecting() as declared:
        write_release_index(repo)
    assert any(Path(p).as_posix().endswith("archive/releases/v0.1.0.yaml") for p in declared)


def test_zero_tags_gives_empty_result(repo: Path) -> None:
    result = write_release_index(repo)
    assert result["written"] == []
    assert result["tags_considered"] == 0
    assert released_names(repo) == {}


def _call(params: dict, root: Path | None) -> dict:
    return asyncio.run(release_index._write_index_handler(params, root))


def test_handler_writes_and_refuses_bad_params(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    bad = _call({"report": "yes"}, repo)
    assert bad["no_op"] is True and bad["reason"] == "bad-params"
    bad = _call({"tag_prefix": 3}, repo)
    assert bad["reason"] == "bad-params"
    ok = _call({}, repo)
    assert [w["tag"] for w in ok["written"]] == ["v0.1.0"]


def test_handler_refuses_without_repo_root() -> None:
    res = _call({}, None)
    assert res["no_op"] is True and res["reason"] == "bad-params"


def test_handler_reports_retargeted_as_refusal(repo: Path) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    _call({}, repo)
    _tag(repo, "v0.2.0", ("2026-10", "b.md"))
    _git(repo, "tag", "-f", "-a", "v0.1.0", "-m", "moved", "HEAD")
    res = _call({}, repo)
    assert res["no_op"] is True and res["reason"] == "retargeted"


def test_write_mode_spawns_exactly_one_process(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tag(repo, "v0.1.0", ("2026-10", "a.md"))
    _tag(repo, "v0.2.0", ("2026-10", "b.md"))
    spawned: list[object] = []
    real = subprocess.Popen

    class Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            spawned.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    write_release_index(repo)
    assert len(spawned) == 1


def test_path_needing_quotes_round_trips(repo: Path) -> None:
    """A space and ``#`` force the double-quoted form."""
    _tag(repo, "v0.1.0", ("2026-10", "a b #c.md"))
    write_release_index(repo)
    assert _index(repo, "v0.1.0")["entries"] == ["archive/completed/2026-10/a b #c.md"]
    assert released_names(repo) == {"a b #c.md": "v0.1.0"}
