"""Standing gate: release.write_index plus the unreleased lookup under the 500ms bar.

Fixture: a git repo with 20 annotated version tags and 20 new completion entries per tag (400).
Every measured call runs on a fresh worktree directory whose `.git` file points at the fixture's
object store, so no call sees an earlier call's indices. The measured cell is the full backfill
(every tag lacks an index), which bounds the one-tag cut.

Basis: the parent's own CPU (`in_process_time_ms`: index writes, in-process tree reads,
`released_names`) plus the one `git for-each-ref` child (`batched_process_time_ms`). Process time
and spawn count only, never wall clock. BUDGET_MS is the brightline bar itself (DR-344).
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms, in_process_time_ms
from coordinator_core.ops import release_index

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BUDGET_MS = 500.0
TAGS = 20
ENTRIES_PER_TAG = 20
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True, creationflags=_NO_WINDOW
    )


@pytest.fixture(scope="module")
def object_store(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("release-index-fixture")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")
    for t in range(TAGS):
        month = repo / "archive" / "completed" / f"2026-{t % 10 + 1:02d}"
        month.mkdir(parents=True, exist_ok=True)
        for e in range(ENTRIES_PER_TAG):
            (month / f"entry-t{t:02d}-e{e:02d}.md").write_text(f"---\ntitle: t{t}e{e}\n---\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"release {t}")
        _git(repo, "tag", "-a", f"v0.{t}.0", "-m", f"v0.{t}.0")
    return repo


class _Copies:
    """Hands out an empty worktree directory per call, its `.git` file pointing at the shared store."""

    def __init__(self, store: Path, base: Path) -> None:
        self._store = store
        self._base = base
        self._n = itertools.count()
        self.last: Path = base

    def fresh(self) -> Path:
        root = self._base / f"copy-{next(self._n)}"
        root.mkdir()
        (root / ".git").write_text(f"gitdir: {(self._store / '.git').as_posix()}\n", encoding="utf-8")
        self.last = root
        return root


@pytest.fixture()
def copies(object_store, tmp_path) -> _Copies:
    return _Copies(object_store, tmp_path)


def test_write_mode_spawns_one_git_child_and_writes_every_index(copies, monkeypatch):
    spawned: list[list[str]] = []
    real = release_index.run_git

    def counting(args, *a, **kw):
        spawned.append(list(args))
        return real(args, *a, **kw)

    popens: list[object] = []
    real_popen = subprocess.Popen

    def counting_popen(*a, **kw):
        popens.append(a[0] if a else kw.get("args"))
        return real_popen(*a, **kw)

    monkeypatch.setattr(release_index, "run_git", counting)
    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    root = copies.fresh()
    result = release_index.write_release_index(root)
    assert len(spawned) == 1, f"expected one git child, saw {spawned}"
    assert len(popens) == 1, f"expected one spawn of any kind, saw {popens}"
    assert len(result["written"]) == TAGS
    assert sum(len(w["entries"]) for w in result["written"]) == TAGS * ENTRIES_PER_TAG
    assert len(list((root / release_index.INDEX_ROOT).glob("*.yaml"))) == TAGS
    assert len(release_index.released_names(root)) == TAGS * ENTRIES_PER_TAG


def test_write_plus_lookup_process_time_under_bar(copies):
    def op() -> None:
        root = copies.fresh()
        release_index.write_release_index(root)
        release_index.released_names(root)

    parent = in_process_time_ms(op)
    assert len(release_index.released_names(copies.last)) == TAGS * ENTRIES_PER_TAG, "timed a call that wrote nothing"

    cmd = ["git", "for-each-ref", f"--format={release_index._REF_FORMAT}", "refs/tags/v*"]
    child = batched_process_time_ms(cmd, k=5, cwd=str(copies.fresh()))
    assert child["rc"] == 0, f"for-each-ref rc={child['rc']}"

    total = parent["process_time_ms"] + child["process_time_ms"]
    print(
        f"RELEASE-INDEX-BUDGET: parent {parent['process_time_ms']:.1f}ms + child "
        f"{child['process_time_ms']:.1f}ms = {total:.1f}ms ({sys.platform})"
    )
    assert total < BUDGET_MS, f"release index write plus lookup {total:.1f}ms >= {BUDGET_MS}ms"
