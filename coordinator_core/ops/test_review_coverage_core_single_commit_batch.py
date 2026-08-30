"""The single-commit batch must be a pure cost change: same segments, fewer spawns.

`build_segments` resolved one `git log --format=%H --name-only <range>` per
distinct range. Measured 2026-08-28 on a 1252-record week that was 1043
spawns and 32.6s process time against a 500ms bar
(state/bug-backlog/2026-08-28-the-weekly-reviewer-scope-costs-1043-spawns-to-narrow-nothing).

98.6% of those ranges are `<sha>~1..<sha>`, which denotes exactly `{sha}` on a
single-parent commit and needs no reachability walk at all. The batch resolves
all of them in one `git log --no-walk --stdin` pass.

What these tests exist to stop is not the speed regressing -- it is the batch
QUIETLY DIVERGING from the path it replaces. The negative spec it must never
violate is the one already written above `build_segments`: git evaluates a
range as one set expression, so ranges must never be combined. These assert
the fast path agrees with the slow path commit-for-commit, and that the three
shapes where `X~1..X` is NOT `{X}` fall back rather than answering wrong.
"""

from __future__ import annotations

import json
import subprocess

import pytest

# Module-level rather than per-test: every spawn site in this file is a
# HELPER (`_git`, `_commit`, `_head`, the `repo` fixture), and a decorator
# only ever reaches a collectible `test_*`. Four of the five tests build a
# real repo, so tiering the whole file costs one pure-regex test its place
# on the fast tier and is the honest trade the spawn ratchet asks for.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

import coordinator_core.ops.review_coverage_core as rcc
from coordinator_core.win_portability import no_console_creationflags


def _git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        **rcc._CREATIONFLAGS, **no_console_creationflags(),
    )


def _head(cwd) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        **rcc._CREATIONFLAGS, **no_console_creationflags(),
    ).stdout.strip()


def _commit(cwd, name: str) -> str:
    (cwd / name).write_text(name, encoding="utf-8")
    _git("add", "--", name, cwd=cwd)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name, cwd=cwd)
    return _head(cwd)


@pytest.fixture()
def repo(tmp_path):
    _git("init", "-q", str(tmp_path), cwd=tmp_path.parent)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
         "-m", "root", cwd=tmp_path)
    return tmp_path


def _records(ranges):
    return [(f"rec{i}.json", {"kind": "diff", "sha_range": r, "verdict": "ok"})
            for i, r in enumerate(ranges)]


def _both_ways(records, cwd, monkeypatch):
    """(batched_result, unbatched_result, batched_spawns, unbatched_spawns).

    The pristine `subprocess.run` is captured ONCE, before either phase
    patches it. Re-reading it per phase wraps the second counter around the
    first, so the first phase's tally silently absorbs the second's -- which
    is how this helper's first draft reported the batch spawning MORE than
    the path it replaces.
    """
    pristine = subprocess.run
    counts = {"batched": 0, "unbatched": 0}

    def _counter(label):
        def _counted(*a, **k):
            counts[label] += 1
            return pristine(*a, **k)

        return _counted

    monkeypatch.setattr(rcc.subprocess, "run", _counter("batched"))
    batched = rcc.build_segments(records, "skip", cwd=str(cwd))

    monkeypatch.setattr(rcc.subprocess, "run", _counter("unbatched"))
    monkeypatch.setattr(rcc, "_batch_single_commit_segments", lambda *a, **k: {})
    unbatched = rcc.build_segments(records, "skip", cwd=str(cwd))

    monkeypatch.setattr(rcc.subprocess, "run", pristine)
    return batched, unbatched, counts["batched"], counts["unbatched"]


def test_batched_and_unbatched_agree_and_the_batch_spawns_less(repo, monkeypatch):
    shas = [_commit(repo, f"f{i}.txt") for i in range(6)]
    records = _records([f"{s}~1..{s}" for s in shas])

    batched, unbatched, n_batched, n_unbatched = _both_ways(records, repo, monkeypatch)

    assert json.dumps(batched, sort_keys=True) == json.dumps(unbatched, sort_keys=True), (
        "the batch changed the answer, which is the only thing it must never do"
    )
    assert n_batched < n_unbatched, (
        f"batch spawned {n_batched}, per-range spawned {n_unbatched} — no saving"
    )


def test_a_merge_commit_falls_back_rather_than_over_crediting(repo, monkeypatch):
    """`X~1..X` on a merge is X PLUS the whole second-parent branch, not {X}.

    This is the case where the fast path would silently UNDER-credit (it would
    answer `{X}`) if the parent count were assumed rather than read.
    """
    base = _head(repo)
    _git("checkout", "-q", "-b", "side", cwd=repo)
    side = _commit(repo, "side.txt")
    _git("checkout", "-q", base, cwd=repo)
    _git("checkout", "-q", "-B", "main2", cwd=repo)
    _commit(repo, "main.txt")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-ff", "-q",
         "-m", "merge", "side", cwd=repo)
    merge = _head(repo)

    records = _records([f"{merge}~1..{merge}"])
    batched, unbatched, _nb, _nu = _both_ways(records, repo, monkeypatch)

    assert json.dumps(batched, sort_keys=True) == json.dumps(unbatched, sort_keys=True)
    assert side in set(batched[0]["shas"]), (
        "the merge's second-parent commit must still be credited — the fast path "
        "answered {X} for a range that means more than X"
    )


def test_a_root_commit_range_falls_back(repo, monkeypatch):
    """`X~1` does not resolve for a parentless commit; the fallback must
    reproduce the existing skip behaviour rather than the batch inventing an
    answer."""
    root = _head(repo)
    records = _records([f"{root}~1..{root}"])

    batched, unbatched, _nb, _nu = _both_ways(records, repo, monkeypatch)

    assert json.dumps(batched, sort_keys=True) == json.dumps(unbatched, sort_keys=True)


def test_a_multi_commit_range_is_untouched_by_the_batch(repo, monkeypatch):
    """A genuine range must never enter the batch: combining ranges is the
    under-count this module's negative spec forbids."""
    first = _commit(repo, "a.txt")
    _commit(repo, "b.txt")
    last = _commit(repo, "c.txt")
    records = _records([f"{first}..{last}"])

    assert rcc._batch_single_commit_segments([f"{first}..{last}"], cwd=str(repo)) == {}

    batched, unbatched, _nb, _nu = _both_ways(records, repo, monkeypatch)
    assert json.dumps(batched, sort_keys=True) == json.dumps(unbatched, sort_keys=True)
    assert len(list(batched[0]["shas"])) == 2, "a..c credits b and c, not a"


def test_only_the_single_commit_shape_is_recognised():
    """Pure-regex guard, no repo: the shapes admitted to the fast path are
    exactly `<sha>~1..<sha>` and `<sha>^..<sha>` with the SAME sha on both
    sides. A pair of different shas is a real range and must not match."""
    m = rcc._SINGLE_COMMIT_RANGE_RE
    assert m.match("abc1234~1..abc1234")
    assert m.match("abc1234^..abc1234")
    assert not m.match("abc1234~1..def5678"), "different endpoints are a real range"
    assert not m.match("abc1234..abc1234")
    assert not m.match("abc1234~2..abc1234")
    assert not m.match("origin/main..HEAD")
