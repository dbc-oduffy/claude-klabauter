"""HEAD-keyed last-modified-at cache coverage (Lever 1).

The break-class risk here is a stale hit: ``last_modified_at`` feeds rag/cockpit, so a cache
that serves an old answer after history moved is worse than no cache. Every test below is
therefore an equivalence assertion against the uncached oracle
(``enrich._walk_last_modified_at``) on a real throwaway git repo, plus explicit coverage of
the three invalidation classes:

  - HEAD unchanged           -> exact hit, no walk at all.
  - HEAD fast-forwarded      -> extension from the new commits only, still oracle-equal.
  - HEAD rewritten / reset   -> guard fails, full re-derivation, still oracle-equal.
  - date-inverted range      -> guard fails (the exactness condition the extension needs).

Spec backlink: emit() cost-lever work, 2026-07-29 (Lever 1).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.emit import enrich, lma_cache


def _git(root: Path, *args: str, env: "dict | None" = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway git repo, with the cache redirected into the test's own tmp settings home."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    return root


def _commit(root: Path, rel: str, body: str, *, date: "str | None" = None) -> str:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    env = None
    if date is not None:
        import os

        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    _git(root, "commit", "-q", "-m", f"touch {rel}", env=env)
    return _git(root, "rev-parse", "HEAD")


def _oracle(root: Path, wanted: set[str]) -> dict:
    """The uncached derivation — the thing every cached answer must equal."""
    return enrich._walk_last_modified_at(root, set(wanted))


# ------------------------------------------------------------------ cold / exact-hit behaviour
def test_cold_cache_matches_the_oracle_and_writes_an_entry(repo: Path) -> None:
    _commit(repo, "a.md", "one")
    _commit(repo, "b.md", "two")
    wanted = {"a.md", "b.md"}

    got = lma_cache.resolve_last_modified_at(repo, wanted)

    assert got == _oracle(repo, wanted)
    assert lma_cache.cache_path(repo).is_file()


def test_exact_hit_serves_without_walking_git(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HEAD unchanged + same path-set => the git-log walk must not run at all."""
    _commit(repo, "a.md", "one")
    wanted = {"a.md"}
    first = lma_cache.resolve_last_modified_at(repo, wanted)

    def _explode(*_args, **_kwargs):
        raise AssertionError("an exact cache hit must not walk git history")

    monkeypatch.setattr(enrich, "_walk_last_modified_at", _explode)
    assert lma_cache.resolve_last_modified_at(repo, wanted) == first


def test_a_queried_path_with_no_history_stays_absent_without_rewalking(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queried-but-unresolvable must be remembered as such — that path is the walk's worst case.

    The entry records the queried SET as well as the resolved map precisely so an untracked
    path does not force a fresh full-history read on every subsequent run.
    """
    _commit(repo, "a.md", "one")
    wanted = {"a.md", "never-committed.md"}
    first = lma_cache.resolve_last_modified_at(repo, wanted)
    assert "never-committed.md" not in first

    monkeypatch.setattr(
        enrich,
        "_walk_last_modified_at",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should have hit the cache")),
    )
    assert lma_cache.resolve_last_modified_at(repo, wanted) == first


def test_a_superset_path_request_misses_and_rederives(repo: Path) -> None:
    """A path the entry never queried cannot be answered from it — miss, don't guess."""
    _commit(repo, "a.md", "one")
    _commit(repo, "b.md", "two")
    lma_cache.resolve_last_modified_at(repo, {"a.md"})

    wanted = {"a.md", "b.md"}
    assert lma_cache.resolve_last_modified_at(repo, wanted) == _oracle(repo, wanted)


# ------------------------------------------------------------------------- HEAD invalidation
def test_head_change_invalidates_and_matches_the_oracle(repo: Path) -> None:
    """The core break-class guard: a moved HEAD must never serve the pre-move answer."""
    _commit(repo, "a.md", "one")
    wanted = {"a.md"}
    stale = lma_cache.resolve_last_modified_at(repo, wanted)

    _commit(repo, "a.md", "one-changed", date="2026-07-29T12:00:00+00:00")

    fresh = lma_cache.resolve_last_modified_at(repo, wanted)
    assert fresh == _oracle(repo, wanted)
    assert fresh != stale, "a.md was re-touched; its last_modified_at must have moved"


def test_fast_forward_extension_matches_the_oracle_for_touched_and_untouched_paths(
    repo: Path,
) -> None:
    """The extension path: re-derive from the new commits, keep the cached answer otherwise."""
    _commit(repo, "a.md", "one", date="2026-07-01T00:00:00+00:00")
    _commit(repo, "b.md", "two", date="2026-07-02T00:00:00+00:00")
    wanted = {"a.md", "b.md"}
    before = lma_cache.resolve_last_modified_at(repo, wanted)

    # Fast-forward with a strictly-newer commit that touches only b.md.
    _commit(repo, "b.md", "two-changed", date="2026-07-03T00:00:00+00:00")

    after = lma_cache.resolve_last_modified_at(repo, wanted)
    assert after == _oracle(repo, wanted)
    assert after["a.md"] == before["a.md"], "untouched path keeps its cached answer"
    assert after["b.md"] != before["b.md"], "touched path is re-derived from the new commit"


def test_fast_forward_extension_walks_only_the_new_range(repo: Path, monkeypatch) -> None:
    """The whole point of the extension: the walk must be scoped to ``cached..HEAD``."""
    _commit(repo, "a.md", "one", date="2026-07-01T00:00:00+00:00")
    lma_cache.resolve_last_modified_at(repo, {"a.md"})
    _commit(repo, "a.md", "two", date="2026-07-02T00:00:00+00:00")

    seen: list = []
    original = enrich._walk_last_modified_at

    def _spy(root, wanted, *, revrange=None):
        seen.append(revrange)
        return original(root, wanted, revrange=revrange)

    monkeypatch.setattr(enrich, "_walk_last_modified_at", _spy)
    lma_cache.resolve_last_modified_at(repo, {"a.md"})

    assert len(seen) == 1
    assert seen[0] is not None and ".." in seen[0], (
        f"expected a scoped range walk, got revrange={seen[0]!r}"
    )


def test_history_rewrite_falls_back_to_a_full_walk(repo: Path) -> None:
    """A reset/rebase makes the cached HEAD a non-ancestor — the extension must NOT be used."""
    first = _commit(repo, "a.md", "one", date="2026-07-01T00:00:00+00:00")
    _commit(repo, "a.md", "two", date="2026-07-02T00:00:00+00:00")
    wanted = {"a.md"}
    lma_cache.resolve_last_modified_at(repo, wanted)

    _git(repo, "reset", "-q", "--hard", first)

    got = lma_cache.resolve_last_modified_at(repo, wanted)
    assert got == _oracle(repo, wanted)


def test_date_inverted_range_is_not_extended(repo: Path) -> None:
    """The exactness condition: a new commit DATED older than the cached HEAD blocks extension.

    Under a date-inverted range, ``git log``'s committer-date priority queue can emit the
    range commit AFTER the cached HEAD, at which point "prefer the range value" diverges from
    the ``git log -1 -- <path>`` oracle. The guard must refuse to extend and re-walk instead —
    asserted here by equivalence with the oracle, which is what the guard exists to preserve.
    """
    _commit(repo, "a.md", "one", date="2026-07-10T00:00:00+00:00")
    _commit(repo, "b.md", "two", date="2026-07-10T00:00:00+00:00")
    wanted = {"a.md", "b.md"}
    lma_cache.resolve_last_modified_at(repo, wanted)

    # Backdated commit — fast-forward in topology, inverted in committer date.
    _commit(repo, "a.md", "one-older", date="2026-07-01T00:00:00+00:00")

    assert lma_cache.resolve_last_modified_at(repo, wanted) == _oracle(repo, wanted)


# ------------------------------------------------- non-monotonic dates (refuted-finding pins)
#
# Every test above this point builds history with strictly increasing committer dates, so none
# of them can falsify the extension's merge direction either way. These two can. They exist
# because a review finding argued the merge should keep whichever of the cached and range
# values is NEWER, and that proposal was refuted empirically: ``last_modified_at`` is not
# monotonic in committer date (the oracle returns the TOPOLOGICALLY latest commit touching a
# path), so "keep the newer value" would return a date ``git log -1 -- <path>`` does not.
# See lma_cache.py's module docstring.


def _merge_side_branch(root: Path, rel: str, body: str, *, date: str) -> None:
    """Merge a one-commit side branch dated *date* back into the current branch, ``--no-ff``.

    Merges are the mechanism the extension's exactness condition (2) exists for: default
    ``git log`` pops from a committer-date priority queue, so a branch merged in with commits
    dated older than the merge target is dequeued out of topological order.
    """
    import os

    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    side = f"side-{base}-{rel.replace('/', '-')}"
    _git(root, "checkout", "-q", "-b", side)
    _commit(root, rel, body, date=date)
    _git(root, "checkout", "-q", base)

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    _git(root, "merge", "-q", "--no-ff", "-m", f"merge {side}", side, env=env)


def test_range_value_wins_even_when_dated_before_an_ancestors_touch(repo: Path) -> None:
    """The refuted finding, as a pin: the cache must EQUAL the oracle when the answer moves back.

    History shape — a path's last touch is committer-dated EARLIER than an ancestor commit
    that also touched it:

        C_old   dated 07-05, touches P     <- ancestor of the cached head
        cached  dated 07-01, touches Q     <- what the cache is stored against
        R       dated 07-03, touches P     <- lands in cached..HEAD, passes the guard

    The oracle's answer for P legitimately moves BACKWARDS (07-05 -> 07-03) because R is the
    topologically latest commit touching it. ``merged.update(range_resolved)`` returns exactly
    that; a "keep whichever is newer" merge would return 07-05 and disagree with the oracle.
    """
    _commit(repo, "P.md", "c_old", date="2026-07-05T00:00:00+00:00")
    _commit(repo, "Q.md", "cached", date="2026-07-01T00:00:00+00:00")
    wanted = {"P.md", "Q.md"}

    before = lma_cache.resolve_last_modified_at(repo, wanted)
    assert before == _oracle(repo, wanted)

    _commit(repo, "P.md", "r", date="2026-07-03T00:00:00+00:00")

    oracle = _oracle(repo, wanted)
    got = lma_cache.resolve_last_modified_at(repo, wanted)

    assert got == oracle
    assert oracle["P.md"] < before["P.md"], (
        "premise check — this fixture only pins anything if the oracle's answer for P.md "
        "actually moves backwards in committer date"
    )
    assert got["P.md"] == oracle["P.md"], (
        "the range value must win unconditionally; keeping the newer of the two would return "
        f"{before['P.md']}, which the oracle does not"
    )


def test_non_monotonic_histories_agree_with_the_oracle(repo: Path) -> None:
    """Differential check: cache-then-extend must equal a full walk at HEAD, per history.

    Three explicit histories rather than randomised ones — deterministic, and each shells out
    to git per commit, so the fixture set is deliberately small. Each history seeds the cache
    mid-way and then adds commits whose committer dates fall both before and after the cached
    HEAD's, with a merged side branch in two of the three. Any disagreement here is
    break-class: ``last_modified_at`` is a rag/cockpit join key.
    """
    wanted = {"a.md", "b.md", "c.md"}

    histories: list[tuple[str, list[tuple[str, str]], list[tuple[str, str]], bool]] = [
        # (name, pre-cache commits, post-cache commits, merge a side branch post-cache)
        (
            "backdated-post-commit",
            [("a.md", "2026-07-10T00:00:00+00:00"), ("b.md", "2026-07-04T00:00:00+00:00")],
            [("a.md", "2026-07-06T00:00:00+00:00"), ("c.md", "2026-07-02T00:00:00+00:00")],
            False,
        ),
        (
            "merged-branch-dated-older",
            [("a.md", "2026-07-08T00:00:00+00:00"), ("c.md", "2026-07-09T00:00:00+00:00")],
            [("b.md", "2026-07-11T00:00:00+00:00")],
            True,
        ),
        (
            "pre-cache-dates-inverted",
            [
                ("c.md", "2026-07-20T00:00:00+00:00"),
                ("a.md", "2026-07-03T00:00:00+00:00"),
                ("b.md", "2026-07-12T00:00:00+00:00"),
            ],
            [("c.md", "2026-07-14T00:00:00+00:00"), ("a.md", "2026-07-13T00:00:00+00:00")],
            True,
        ),
    ]

    for name, pre, post, with_merge in histories:
        _git(repo, "checkout", "-q", "--orphan", f"h-{name}")
        _git(repo, "rm", "-rqf", "--ignore-unmatch", ".")
        for rel, date in pre:
            _commit(repo, rel, f"{name}-pre-{rel}", date=date)

        seeded = lma_cache.resolve_last_modified_at(repo, wanted)
        assert seeded == _oracle(repo, wanted), f"{name}: seeded answer already diverged"

        for rel, date in post:
            _commit(repo, rel, f"{name}-post-{rel}", date=date)
        if with_merge:
            _merge_side_branch(repo, "b.md", f"{name}-side", date="2026-07-05T00:00:00+00:00")

        assert lma_cache.resolve_last_modified_at(repo, wanted) == _oracle(repo, wanted), (
            f"{name}: the cache disagreed with its own stated oracle"
        )


# ------------------------------------------------------------------------------ degrade paths
def test_malformed_cache_file_degrades_to_a_fresh_walk(repo: Path) -> None:
    _commit(repo, "a.md", "one")
    wanted = {"a.md"}
    lma_cache.resolve_last_modified_at(repo, wanted)

    path = lma_cache.cache_path(repo)
    path.write_text("}} not json", encoding="utf-8")
    assert lma_cache.resolve_last_modified_at(repo, wanted) == _oracle(repo, wanted)


def test_wrong_version_cache_file_degrades_to_a_fresh_walk(repo: Path) -> None:
    import json

    _commit(repo, "a.md", "one")
    wanted = {"a.md"}
    lma_cache.resolve_last_modified_at(repo, wanted)

    path = lma_cache.cache_path(repo)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = lma_cache._CACHE_VERSION + 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert lma_cache.resolve_last_modified_at(repo, wanted) == _oracle(repo, wanted)


def test_non_repo_root_degrades_without_caching(tmp_path: Path, monkeypatch) -> None:
    """No resolvable HEAD (not a repo) — derive fresh, write nothing, never raise."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert lma_cache.resolve_last_modified_at(plain, {"a.md"}) == {}
    assert not lma_cache.cache_path(plain).exists()


def test_empty_wanted_set_is_a_no_op(repo: Path) -> None:
    assert lma_cache.resolve_last_modified_at(repo, set()) == {}


def test_cache_path_lives_under_the_durable_data_plane(repo: Path, tmp_path: Path) -> None:
    """CLAUDE.md § Durable-data plane — out-of-repo persistence uses the sanctioned prefix.

    Asserted rather than assumed because the alternative failure is silent: an ad-hoc ``~/``
    path would work locally and be invisible to coordinator uninstall's provenance sweep. Also
    asserts the cache is NOT written into the emitting repo, which is routinely a sibling clone.
    """
    path = lma_cache.cache_path(repo)
    assert path.is_relative_to(tmp_path / "settings-home" / "claude-klabauter")
    assert not path.is_relative_to(repo)


def test_sibling_clones_with_the_same_basename_do_not_share_a_cache_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    a = tmp_path / "x" / "same-name"
    b = tmp_path / "y" / "same-name"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert lma_cache.cache_path(a) != lma_cache.cache_path(b)


# --------------------------------------------------------- integration through the public seam
def test_batch_last_modified_at_grouped_still_matches_the_oracle_across_two_runs(repo: Path) -> None:
    """The caller-facing seam ``envelope._stamp_lma`` uses, exercised cold then warm."""
    _commit(repo, "a.md", "one", date="2026-07-01T00:00:00+00:00")
    _commit(repo, "b.md", "two", date="2026-07-02T00:00:00+00:00")
    groups = [["a.md", "b.md"], ["b.md", "missing.md"]]

    expected = [
        [_oracle(repo, {"a.md"})["a.md"], _oracle(repo, {"b.md"})["b.md"]],
        [_oracle(repo, {"b.md"})["b.md"], None],
    ]
    assert enrich.batch_last_modified_at_grouped(repo, groups) == expected
    # Warm run — served from the cache, must be identical (positional order included).
    assert enrich.batch_last_modified_at_grouped(repo, groups) == expected
