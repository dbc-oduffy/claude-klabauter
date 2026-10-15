"""list_release_tags against real tmp repos, and the completion.stamp_releases handler."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from coordinator_core.ops import release_stamp as rs
from coordinator_core.ops.release_stamp.model import Attribution, ReleaseTag, StampError
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DATES = {"d1": "2026-01-05T10:00:00", "d2": "2026-02-06T10:00:00"}


def _git(repo: Path, *args: str, date: str = _DATES["d1"]) -> str:
    env = {
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env},
        **no_console_creationflags(),
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, date: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name, date=date)
    _git(repo, "commit", "-q", "-m", name, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    return r


def test_lightweight_and_annotated_resolve_to_commit(repo: Path) -> None:
    c1 = _commit(repo, "a", _DATES["d1"])
    _git(repo, "tag", "v0.1.0")
    c2 = _commit(repo, "b", _DATES["d2"])
    _git(repo, "tag", "-a", "v0.2.0", "-m", "rel", date="2030-01-01T00:00:00")

    tags = rs.list_release_tags(repo, "")

    assert [t.name for t in tags] == ["v0.1.0", "v0.2.0"]
    assert tags[0].commit_sha == c1 and tags[1].commit_sha == c2
    assert tags[0].date == "2026-01-05" and tags[1].date == "2026-02-06"
    assert tags[0].tree_sha == _git(repo, "rev-parse", "v0.1.0^{tree}")
    assert tags[1].tree_sha == _git(repo, "rev-parse", "v0.2.0^{tree}")
    assert tags[1].version == (0, 2, 0)


def test_non_version_tags_and_other_prefixes_dropped(repo: Path) -> None:
    _commit(repo, "a", _DATES["d1"])
    for t in ("v1.0.0", "vNext", "v1.2", "v1.2.3-rc1", "backup/v1.0.0", "example-game-repo-v0.4.0", "xv1.0.0"):
        _git(repo, "tag", t)
    assert [t.name for t in rs.list_release_tags(repo, "")] == ["v1.0.0"]
    assert [t.name for t in rs.list_release_tags(repo, "example-game-repo-")] == ["example-game-repo-v0.4.0"]


def test_version_order_is_numeric(repo: Path) -> None:
    _commit(repo, "a", _DATES["d1"])
    for t in ("v0.6.10", "v0.6.9", "v0.10.0", "v0.6.2"):
        _git(repo, "tag", t)
    assert [t.name for t in rs.list_release_tags(repo, "")] == [
        "v0.6.2",
        "v0.6.9",
        "v0.6.10",
        "v0.10.0",
    ]


def test_no_tags(repo: Path) -> None:
    _commit(repo, "a", _DATES["d1"])
    assert rs.list_release_tags(repo, "") == []


def test_git_failure_raises_git_failed(tmp_path: Path) -> None:
    with pytest.raises(StampError) as exc:
        rs.list_release_tags(tmp_path / "missing", "")
    assert exc.value.reason == "git-failed"


def _run(params: dict, root: Path | None):
    return asyncio.run(rs._stamp_releases_handler(params, root))


@pytest.mark.parametrize(
    "params, needle",
    [({"tag_prefix": 3}, "tag_prefix"), ({"report": "yes"}, "report")],
)
def test_handler_param_refusals(repo: Path, params: dict, needle: str) -> None:
    res = _run(params, repo)
    assert res["no_op"] is True
    assert "bad-params" in res["error"] and needle in res["error"]


def test_handler_refuses_without_repo_root() -> None:
    res = _run({}, None)
    assert res["no_op"] is True and "no-repo-root" in res["error"]


def test_handler_surfaces_stamp_error_as_refusal(repo: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise StampError("tree-unreadable", "v0.1.0")

    monkeypatch.setattr(rs, "stamp_releases", boom)
    res = _run({}, repo)
    assert res == {"error": "completion.stamp_releases: tree-unreadable: v0.1.0", "no_op": True}


def _fake_callees(monkeypatch, attribute, stamp_entry) -> None:
    """Install stand-in attribution and writer modules so the op's pass-through is tested alone."""
    for name, attr, fn in (("attribution", "attribute", attribute), ("writer", "stamp_entry", stamp_entry)):
        mod = types.ModuleType(f"coordinator_core.ops.release_stamp.{name}")
        setattr(mod, attr, fn)
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
        monkeypatch.setattr(rs, name, mod, raising=False)


def test_report_passes_through_with_callees_substituted(repo: Path, monkeypatch) -> None:
    _commit(repo, "a", _DATES["d1"])
    _git(repo, "tag", "v0.1.0")
    tag = rs.list_release_tags(repo, "")[0]
    seen: dict = {}

    def fake_attribute(root, tags):
        seen["tags"] = tags
        return Attribution(
            stamps={"archive/completed/2026-01/b.md": tag, "archive/completed/2026-01/a.md": tag},
            unreleased=("archive/completed/2026-01/z.md",),
        )

    def fake_stamp(root, path, t):
        seen.setdefault("stamped", []).append(path)
        return {
            "path": path,
            "released_in": t.name,
            "released_at": t.date,
            "released_sha": t.commit_sha,
        }, None

    _fake_callees(monkeypatch, fake_attribute, fake_stamp)

    dry = rs.stamp_releases(repo, report=True)
    assert "stamped" not in seen
    assert dry["report"] is True and dry["tags_considered"] == 1
    assert [s["path"] for s in dry["stamped"]] == [
        "archive/completed/2026-01/a.md",
        "archive/completed/2026-01/b.md",
    ]
    assert dry["unreleased"] == {"count": 1, "paths": ["archive/completed/2026-01/z.md"]}

    wet = rs.stamp_releases(repo)
    assert wet["report"] is False
    assert seen["stamped"] == [s["path"] for s in dry["stamped"]]
    assert wet["stamped"] == dry["stamped"] and wet["skipped"] == []


def test_skip_reasons_are_reported(repo: Path, monkeypatch) -> None:
    _commit(repo, "a", _DATES["d1"])
    _git(repo, "tag", "v0.1.0")
    tag = rs.list_release_tags(repo, "")[0]
    _fake_callees(
        monkeypatch,
        lambda r, t: Attribution({"archive/completed/m/x.md": tag}, ()),
        lambda r, p, t: (None, f"{p}: status is released"),
    )
    res = rs.stamp_releases(repo)
    assert res["stamped"] == [] and res["skipped"] == ["archive/completed/m/x.md: status is released"]


def test_release_tag_is_frozen() -> None:
    t = ReleaseTag("v1.0.0", (1, 0, 0), "a", "b", "2026-01-01")
    with pytest.raises(Exception):
        t.name = "x"  # type: ignore[misc]
