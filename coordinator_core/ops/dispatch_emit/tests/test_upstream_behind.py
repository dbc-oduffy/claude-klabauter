"""upstream_behind.check: none / current / ahead / diverged-refuses-with-counts / behind-fast-forwards, on real tmp_path git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import upstream_behind
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()


def commit(root: Path, name: str) -> str:
    (root / name).write_text(name + "\n", encoding="utf-8", newline="\n")
    git(root, "add", name)
    git(root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", name)
    return git(root, "rev-parse", "HEAD")


def give_upstream(root: Path, base: Path) -> Path:
    """Bare origin beside *root*, with root's current branch pushed and tracking it."""
    origin = base / "origin.git"
    git(base, "init", "-q", "--bare", str(origin))
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "-u", "origin", "HEAD")
    return origin


def origin_advances(root: Path, origin: Path, base: Path, name: str, *, fetch: bool = True) -> str:
    """A second clone commits onto origin's branch; *root* then fetches so its tracking ref knows."""
    other = base / f"other-{name}"
    git(base, "clone", "-q", str(origin), str(other))
    sha = commit(other, name)
    git(other, "push", "-q", "origin", "HEAD")
    if fetch:
        git(root, "fetch", "-q", "origin")
    return sha


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    commit(root, "seed")
    return root, tmp_path


def test_a_branch_with_no_upstream_is_not_checked_and_spawns_nothing(world, monkeypatch) -> None:
    root, _ = world
    spawned: list = []
    monkeypatch.setattr(upstream_behind, "_git", lambda *a, **k: spawned.append(a))

    verdict = upstream_behind.check(root)

    assert verdict.state == upstream_behind.NONE and verdict.refusal is None
    assert spawned == []


def test_up_to_date_spawns_nothing(world, monkeypatch) -> None:
    root, base = world
    give_upstream(root, base)
    spawned: list = []
    monkeypatch.setattr(upstream_behind, "_git", lambda *a, **k: spawned.append(a))

    verdict = upstream_behind.check(root)

    assert verdict.state == upstream_behind.CURRENT and verdict.refusal is None
    assert spawned == []


def test_ahead_only_proceeds_and_does_not_fetch(world, monkeypatch) -> None:
    root, base = world
    give_upstream(root, base)
    commit(root, "local-1")
    calls: list = []
    real = upstream_behind._git
    monkeypatch.setattr(upstream_behind, "_git", lambda r, args, **k: calls.append(args[0]) or real(r, args, **k))

    verdict = upstream_behind.check(root)

    assert verdict.state == upstream_behind.AHEAD and verdict.ahead == 1 and verdict.behind == 0
    assert verdict.refusal is None
    assert calls == ["rev-list"]


def test_behind_with_unique_local_commits_refuses_naming_both_counts_and_never_moves_head(world, monkeypatch) -> None:
    root, base = world
    origin = give_upstream(root, base)
    commit(root, "local-1")
    for n in ("o1", "o2", "o3"):
        origin_advances(root, origin, base, n)
    head = git(root, "rev-parse", "HEAD")
    calls: list = []
    real = upstream_behind._git
    monkeypatch.setattr(upstream_behind, "_git", lambda r, args, **k: calls.append(args[0]) or real(r, args, **k))

    verdict = upstream_behind.check(root)

    assert verdict.state == upstream_behind.DIVERGED and (verdict.ahead, verdict.behind) == (1, 3)
    assert "1 ahead of and 3 behind" in verdict.refusal and verdict.upstream in verdict.refusal
    assert git(root, "rev-parse", "HEAD") == head
    assert calls == ["rev-list"]


def test_behind_with_no_unique_commits_fetches_and_fast_forwards(world) -> None:
    root, base = world
    origin = give_upstream(root, base)
    origin_advances(root, origin, base, "o1", fetch=False)
    tip = origin_advances(root, origin, base, "o2", fetch=False)
    git(root, "fetch", "-q", "origin")
    # The tracking ref already knows; a newer origin commit rides the same fetch.
    newest = origin_advances(root, origin, base, "o3", fetch=False)

    verdict = upstream_behind.check(root)

    assert verdict.state == upstream_behind.FAST_FORWARDED and verdict.refusal is None
    assert git(root, "rev-parse", "HEAD") == newest != tip
    assert (root / "o3").is_file()


def test_a_failed_fetch_refuses_naming_the_count_and_leaves_head(world) -> None:
    root, base = world
    origin = give_upstream(root, base)
    origin_advances(root, origin, base, "o1")
    git(root, "remote", "set-url", "origin", str(base / "gone.git"))
    head = git(root, "rev-parse", "HEAD")

    verdict = upstream_behind.check(root)

    assert verdict.state == upstream_behind.BEHIND and "1 behind and 0 ahead" in verdict.refusal
    assert git(root, "rev-parse", "HEAD") == head
