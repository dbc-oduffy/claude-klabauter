"""Probe: attribute the cost of `rollback_check.find_exact_blob_rollbacks` on a live store.

Run: python -m coordinator_core.benchmarks.probe_rollback_check_cost --repo . --from-commit 3c95ac4a26
     python -m coordinator_core.benchmarks.probe_rollback_check_cost --repo . --head HEAD --churned 60 --window 1000

Runs the check in-process from cold caches and prints process time (`os.times()` self plus
reaped children, so a spawned mechanism is charged honestly; never wall clock), the
subprocess spawn count, `read_object` calls, `_TREE_CACHE`
hits/misses, pack-object inflates (top-level vs OFS_DELTA chain links) and per-pack mmap
opens. Counters wrap module attributes inside the probe; production code is untouched.
A run past `--cap-s` of process time is cut and reported as exceeded, with partial counters.

Candidate values: with `--from-commit` they are that commit's own blobs for the paths it
changed, and `--head` defaults to its first parent (the commit_paths shape). With `--paths` /
`--sample-head` / `--churned` they are a sentinel that matches no ancestor, which forces the
full walk. `--churned N` is the N most-changed paths over `--head`'s last `--lookback`
first-parent commits, resolved in-process with no spawn. `--impl FILE.py:FUNC` measures
FUNC (same signature as `find_exact_blob_rollbacks`) in place of the shipped function.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.git import commit_walk, git_objects, rollback_check
from coordinator_core.git.commit_walk import commit_meta

_SENTINEL = "0" * 39 + "1"


class _CapExceeded(Exception):
    pass


def _process_s() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _resolve_head(common_dir: Path, head: str) -> Optional[str]:
    """40-hex passes through; HEAD / a ref name resolves from loose files then packed-refs."""
    if re.fullmatch(r"[0-9a-f]{40}", head):
        return head
    ref = head
    for _ in range(5):
        if ref == "HEAD":
            raw = (common_dir / "HEAD").read_text().strip()
        else:
            loose = common_dir / ref
            if loose.is_file():
                raw = loose.read_text().strip()
            else:
                raw = ""
                packed = common_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text().splitlines():
                        if line.endswith(" " + ref):
                            raw = line.split(" ", 1)[0]
                            break
        if re.fullmatch(r"[0-9a-f]{40}", raw):
            return raw
        if raw.startswith("ref: "):
            ref = raw[5:]
            continue
        return None
    return None


def _churned(common_dir: Path, head: str, n: int, lookback: int) -> List[str]:
    """The n paths changed most often across `lookback` first-parent commits ending at head."""
    counts: Counter = Counter()
    sha: Optional[str] = head
    child_tree: Optional[str] = None
    for i in range(lookback + 1):
        meta = commit_meta(common_dir, sha) if sha else None
        if meta is None:
            break
        tree = meta.get("tree")
        if i > 0:
            changed: Dict[str, object] = {}
            _diff_trees(common_dir, tree, child_tree, "", changed)
            counts.update(changed.keys())
        child_tree = tree
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return [p for p, _c in counts.most_common(n)]


def _load_impl(spec: str):
    file_part, _, func = spec.rpartition(":")
    mod_spec = importlib.util.spec_from_file_location("_rollback_impl", file_part)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return getattr(mod, func)


def _clear_caches() -> None:
    commit_walk._COMMIT_CACHE.clear()
    rollback_check._TREE_CACHE.clear()
    git_objects._OBJECT_CACHE.clear()


def _tree_of(common_dir: Path, commit_sha: str) -> Optional[str]:
    meta = commit_meta(common_dir, commit_sha)
    return meta.get("tree") if meta else None


def _diff_trees(common_dir: Path, a: Optional[str], b: Optional[str], prefix: str, out: Dict[str, object]) -> None:
    """Paths whose blob differs between trees a (old) and b (new); value is b's blob or ABSENT."""
    ea = rollback_check._tree_entries(common_dir, a) if a else {}
    eb = rollback_check._tree_entries(common_dir, b) if b else {}
    for name in sorted(set(ea or {}) | set(eb or {})):
        xa, xb = (ea or {}).get(name), (eb or {}).get(name)
        if xa == xb:
            continue
        path = prefix + name
        a_tree = xa[1] if xa and xa[0] == rollback_check._TREE_MODE else None
        b_tree = xb[1] if xb and xb[0] == rollback_check._TREE_MODE else None
        if a_tree or b_tree:
            _diff_trees(common_dir, a_tree, b_tree, path + "/", out)
        if xb is not None and xb[0] != rollback_check._TREE_MODE:
            out[path] = xb[1]
        elif xb is None and xa is not None and xa[0] != rollback_check._TREE_MODE:
            out[path] = rollback_check.ABSENT


def _list_blob_paths(common_dir: Path, tree: str, prefix: str, limit: int, out: List[str]) -> None:
    for name, (mode, sha) in sorted((rollback_check._tree_entries(common_dir, tree) or {}).items()):
        if len(out) >= limit:
            return
        if mode == rollback_check._TREE_MODE:
            _list_blob_paths(common_dir, sha, prefix + name + "/", limit, out)
        else:
            out.append(prefix + name)


class _Counters:
    def __init__(self, cap_s: float) -> None:
        self.cap_s = cap_s
        self.read_object_calls = 0
        self.tree_cache_hits = 0
        self.tree_cache_misses = 0
        self.pack_object_inflates = 0
        self.delta_chain_links_inflated = 0
        self.mmap_opens = 0
        self.mmap_opens_by_pack: Counter = Counter()
        self._orig: Dict[str, object] = {}

    def _check_cap(self) -> None:
        if _process_s() - self._t0 > self.cap_s:
            raise _CapExceeded()

    def __enter__(self) -> "_Counters":
        me = self
        o_read = rollback_check.read_object
        o_tree = rollback_check._tree_entries
        o_at = git_objects._read_pack_object_at
        o_bytes = git_objects._read_pack_bytes
        self._orig = {"read": o_read, "tree": o_tree, "at": o_at, "bytes": o_bytes}

        def read(common_dir, sha):  # type: ignore[no-untyped-def]
            me.read_object_calls += 1
            me._check_cap()
            return o_read(common_dir, sha)

        def tree(common_dir, tree_sha):  # type: ignore[no-untyped-def]
            if (str(common_dir), tree_sha) in rollback_check._TREE_CACHE:
                me.tree_cache_hits += 1
            else:
                me.tree_cache_misses += 1
            return o_tree(common_dir, tree_sha)

        def at(common_dir, pack_path, pack_bytes, offset, _depth=0):  # type: ignore[no-untyped-def]
            if _depth == 0:
                me.pack_object_inflates += 1
            else:
                me.delta_chain_links_inflated += 1
            return o_at(common_dir, pack_path, pack_bytes, offset, _depth)

        def pbytes(pack_path):  # type: ignore[no-untyped-def]
            me.mmap_opens += 1
            me.mmap_opens_by_pack[Path(pack_path).name] += 1
            return o_bytes(pack_path)

        rollback_check.read_object = read  # type: ignore[assignment]
        rollback_check._tree_entries = tree  # type: ignore[assignment]
        git_objects._read_pack_object_at = at  # type: ignore[assignment]
        git_objects._read_pack_bytes = pbytes  # type: ignore[assignment]
        self._popen = subprocess.Popen.__init__
        self.spawns = 0
        orig_popen = self._popen

        def counted(popen_self, *a, **k):  # type: ignore[no-untyped-def]
            me.spawns += 1
            return orig_popen(popen_self, *a, **k)

        subprocess.Popen.__init__ = counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        rollback_check.read_object = self._orig["read"]  # type: ignore[assignment]
        rollback_check._tree_entries = self._orig["tree"]  # type: ignore[assignment]
        git_objects._read_pack_object_at = self._orig["at"]  # type: ignore[assignment]
        git_objects._read_pack_bytes = self._orig["bytes"]  # type: ignore[assignment]
        subprocess.Popen.__init__ = self._popen  # type: ignore[method-assign]


def measure(
    common_dir: Path, head: str, candidates: Dict[str, object], window: int, cap_s: float, impl=None
) -> Dict[str, object]:
    _clear_caches()
    fn = impl or rollback_check.find_exact_blob_rollbacks
    with _Counters(cap_s) as c:
        c._t0 = _process_s()
        status = "completed"
        findings = None
        try:
            findings = fn(common_dir, head, candidates, window)
        except _CapExceeded:
            status = f"exceeded {cap_s:g}s process time (cut)"
        ms = (_process_s() - c._t0) * 1000.0
    return {
        "status": status,
        "process_ms": round(ms, 2),
        "spawns": c.spawns,
        "read_object_calls": c.read_object_calls,
        "tree_cache_hits": c.tree_cache_hits,
        "tree_cache_misses": c.tree_cache_misses,
        "pack_object_inflates": c.pack_object_inflates,
        "delta_chain_links_inflated": c.delta_chain_links_inflated,
        "mmap_opens": c.mmap_opens,
        "mmap_opens_by_pack": dict(c.mmap_opens_by_pack),
        "paths": len(candidates),
        "window": window,
        "findings": None if findings is None else len(findings),
        "findings_digest": None if findings is None else hashlib.sha1(
            repr(sorted(tuple(f) for f in findings)).encode()
        ).hexdigest(),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--head")
    ap.add_argument("--paths", action="append", default=[], help="file of paths, one per line; repeatable")
    ap.add_argument("--window", type=int, default=1000)
    ap.add_argument("--from-commit")
    ap.add_argument("--sample-head", type=int, help="first N blob paths of --head's tree")
    ap.add_argument("--churned", type=int, help="N most-changed paths over --head's last --lookback commits")
    ap.add_argument("--lookback", type=int, default=400)
    ap.add_argument("--impl", help="FILE.py:FUNC measured in place of find_exact_blob_rollbacks")
    ap.add_argument("--cap-s", type=float, default=60.0)
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    repo = Path(args.repo).resolve()
    common_dir = repo / ".git"
    candidates: Dict[str, object] = {}
    head = args.head
    if args.from_commit:
        meta = commit_meta(common_dir, args.from_commit)
        if meta is None:
            print(f"unresolved commit {args.from_commit}", file=sys.stderr)
            return 2
        parents = meta.get("parents") or []
        head = head or (parents[0] if parents else None)
        ptree = _tree_of(common_dir, parents[0]) if parents else None
        _diff_trees(common_dir, ptree, meta.get("tree"), "", candidates)
    for pf in args.paths:
        for line in Path(pf).read_text().splitlines():
            if line.strip():
                candidates[line.strip()] = _SENTINEL
    if args.sample_head:
        if not head:
            print("--sample-head needs --head", file=sys.stderr)
            return 2
        sample: List[str] = []
        _list_blob_paths(common_dir, _tree_of(common_dir, head) or "", "", args.sample_head, sample)
        for p in sample:
            candidates[p] = _SENTINEL
    if head:
        head = _resolve_head(common_dir, head)
    if args.churned:
        if not head:
            print("--churned needs --head", file=sys.stderr)
            return 2
        for p in _churned(common_dir, head, args.churned, args.lookback):
            candidates[p] = _SENTINEL
    if not head or not candidates:
        print("need --head (or --from-commit) and at least one path", file=sys.stderr)
        return 2
    print(json.dumps(measure(common_dir, head, candidates, args.window, args.cap_s, _load_impl(args.impl) if args.impl else None), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
