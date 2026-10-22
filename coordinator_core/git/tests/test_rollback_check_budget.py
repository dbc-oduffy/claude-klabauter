"""coordinator_core.git.tests.test_rollback_check_budget -- the DR-344 process-time
bar and the equivalence oracle for `rollback_check.find_exact_blob_rollbacks`.

The fixture is a hermetic object store built in-process through
`git_objects.write_object` / `object_batch`, one pack per commit as the commit
routes write them, so a full-window walk pays the same pack fan-out the live
store has.

The budget test is `designed_red` until the rebuilt check lands; the
equivalence test pins findings against a brute-force `_blob_at_commit` oracle.
"""

from __future__ import annotations

import hashlib
import os
import random

import pytest

from coordinator_core.git import commit_walk, git_objects, rollback_check
from coordinator_core.git.rollback_check import ABSENT, find_exact_blob_rollbacks

pytestmark = pytest.mark.slow

_COMMITS = 1100
_PATHS = 60
_WINDOW = 1000
_BUDGET_MS = 500.0
_MIN_PACKS = 300
_REGULAR = 0o100644


def _process_ms() -> float:
    t = os.times()
    return (t.user + t.system + t.children_user + t.children_system) * 1000.0


def _clear_caches() -> None:
    commit_walk._COMMIT_CACHE.clear()
    git_objects._OBJECT_CACHE.clear()
    git_objects._PACK_INDEX_CACHE.clear()
    git_objects._PACK_INDEXES_BY_LISTING.clear()
    git_objects._PACK_LISTING_CACHE.clear()
    git_objects._LAST_HIT_PACK.clear()
    rollback_check._TREE_CACHE.clear()


def _commit_text(tree: str, parents: "list[str]", n: int) -> bytes:
    lines = [f"tree {tree}"]
    lines += [f"parent {p}" for p in parents]
    who = f"t <t@local> {1700000000 + n} +0000"
    lines += [f"author {who}", f"committer {who}", "", f"c{n}", ""]
    return "\n".join(lines).encode("utf-8")


def _blob(gitdir, content: bytes) -> str:
    return git_objects.write_object(gitdir, b"blob", content)


class _Fixture:
    def __init__(self, gitdir, head, tip, paths, side_path, side_blob):
        self.gitdir = gitdir
        self.head = head
        self.tip = tip
        self.paths = paths
        self.side_path = side_path
        self.side_blob = side_blob


def _flush_as_pack():
    gitdir, pending = git_objects._BATCH.state
    git_objects._BATCH.state = None
    git_objects._write_pack(gitdir, pending)


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    gitdir = tmp_path_factory.mktemp("rbbudget") / ".git"
    (gitdir / "objects" / "pack").mkdir(parents=True)
    rng = random.Random(344)
    paths = [f"d{i % 6}/s{i % 5}/f{i}.txt" for i in range(_PATHS)]
    state: "dict[str, str]" = {}
    parent = None
    for n in range(_COMMITS):
        with git_objects.object_batch():
            for p in rng.sample(paths, 3):
                if p in state and rng.random() < 0.3:
                    del state[p]
                else:
                    state[p] = _blob(gitdir, f"{p} v{n}\n".encode())
            entries = {p: (_REGULAR, sha) for p, sha in state.items()}
            tree = git_objects.build_tree(gitdir, entries) if entries else None
            if tree is None:
                tree = git_objects.write_object(gitdir, b"tree", b"")
            parent = git_objects.write_object(
                gitdir, b"commit", _commit_text(tree, [parent] if parent else [], n)
            )
            _flush_as_pack()
    tip = parent
    side_path = "side/only.txt"
    with git_objects.object_batch():
        side_blob = _blob(gitdir, b"only on the second parent\n")
        side_entries = {p: (_REGULAR, sha) for p, sha in state.items()}
        side_entries[side_path] = (_REGULAR, side_blob)
        side_tree = git_objects.build_tree(gitdir, side_entries)
        side = git_objects.write_object(
            gitdir, b"commit", _commit_text(side_tree, [tip], _COMMITS)
        )
        _flush_as_pack()
    with git_objects.object_batch():
        entries = {p: (_REGULAR, sha) for p, sha in state.items()}
        tree = git_objects.build_tree(gitdir, entries)
        head = git_objects.write_object(
            gitdir, b"commit", _commit_text(tree, [tip, side], _COMMITS + 1)
        )
        _flush_as_pack()
    packs = list((gitdir / "objects" / "pack").glob("*.idx"))
    assert len(packs) >= _MIN_PACKS
    return _Fixture(gitdir, head, tip, paths, side_path, side_blob)


def _oracle(fx, candidates, window=_WINDOW):
    ancestors = []
    sha = fx.head
    while sha is not None and len(ancestors) < window:
        meta = commit_walk.commit_meta(fx.gitdir, sha)
        if meta is None:
            break
        ancestors.append(sha)
        sha = (meta.get("parents") or [None])[0]
    findings = []
    for path, new_value in candidates.items():
        version = 0
        previous = object()
        for sha in ancestors:
            old = rollback_check._blob_at_commit(fx.gitdir, sha, path)
            if old is None or old == previous:
                continue
            version += 1
            previous = old
            if old == new_value:
                if version >= 2:
                    findings.append(
                        rollback_check.RollbackFinding(path, version - 1, sha)
                    )
                break
    return findings


def _versions(fx, path):
    out = []
    sha = fx.head
    for _ in range(_WINDOW):
        meta = commit_walk.commit_meta(fx.gitdir, sha)
        v = rollback_check._blob_at_commit(fx.gitdir, sha, path)
        if v is not None and (not out or out[-1] != v):
            out.append(v)
        parents = meta.get("parents") or []
        if not parents:
            break
        sha = parents[0]
    return out


@pytest.mark.designed_red
def test_full_window_60_paths_under_500ms_process_time(fx):
    candidates = {
        p: hashlib.sha1(p.encode()).hexdigest() for p in fx.paths
    }
    assert len(candidates) == _PATHS
    _clear_caches()
    before = _process_ms()
    findings = find_exact_blob_rollbacks(fx.gitdir, fx.head, candidates, _WINDOW)
    spent = _process_ms() - before
    assert findings == []
    assert spent < _BUDGET_MS, f"{spent:.0f}ms process time on {_PATHS} paths, bar {_BUDGET_MS:.0f}ms"


def test_findings_equal_brute_force_oracle(fx):
    _clear_caches()
    per_path = {p: _versions(fx, p) for p in fx.paths}
    candidates: "dict[str, object]" = {}
    kinds: "dict[str, str]" = {}

    def pick(kind, want, value_of):
        for p, vs in per_path.items():
            if p in candidates or not want(vs):
                continue
            candidates[p] = value_of(vs)
            kinds[p] = kind
            return
        raise AssertionError(f"fixture has no path for {kind}")

    for _ in range(3):
        pick("depth1", lambda vs: len(vs) >= 2 and vs[1] is not ABSENT and vs[1] != vs[0],
             lambda vs: vs[1])
    pick("deep", lambda vs: len(vs) >= 4 and vs[2] not in (vs[0], vs[1]) and vs[2] is not ABSENT,
         lambda vs: vs[2])
    pick("delete", lambda vs: vs[0] is not ABSENT and ABSENT in vs[1:],
         lambda vs: ABSENT)
    candidates[fx.side_path] = fx.side_blob
    kinds[fx.side_path] = "merge-second-parent"
    for p in fx.paths:
        if p not in candidates:
            candidates[p] = per_path[p][0]

    got = find_exact_blob_rollbacks(fx.gitdir, fx.head, candidates, _WINDOW)
    assert got == _oracle(fx, candidates)

    by_path = {f.path: f for f in got}
    assert sum(1 for p, k in kinds.items() if k == "depth1" and by_path[p].depth == 1) == 3
    assert any(f.depth >= 2 for p, f in by_path.items() if kinds.get(p) == "deep")
    assert any(
        candidates[p] is ABSENT and p in by_path for p, k in kinds.items() if k == "delete"
    )
    assert fx.side_path not in by_path
    assert rollback_check.refusal(got)
    breadth_only = [f for f in got if f.depth == 1]
    assert rollback_check.refusal(breadth_only) is (len({f.path for f in breadth_only}) >= 3)
