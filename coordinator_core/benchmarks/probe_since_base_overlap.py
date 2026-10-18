"""Spike probe: price the since-base line-overlap check in-process, and replay the incident.

Run: python -m coordinator_core.benchmarks.probe_since_base_overlap [--replay-only] [--json]

The candidate check (`since_base_drops`) reads blobs through `rollback_check._blob_at_commit`
and `commit_walk.commit_meta` (zero spawns), attributes each HEAD line to the commit that
introduced it by propagating attribution through `difflib` opcodes version by version, then
diffs HEAD -> landing bytes and reports removed lines a foreign commit introduced. Cost is
reported as `time.process_time()` (never wall clock) plus a subprocess spawn counter that
covers only the measured call. Fixtures are built under a temp dir with `git fast-import`
(setup only, outside the measured window).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.run import run_git
from coordinator_core.temp_layout import managed_tempdir
from coordinator_core.git.git_objects import read_object, read_ref_loose_or_packed
from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit

_PLAN_RE = re.compile(r"^Checkpoint-Plan: (\S.*?)\s*$", re.M)
_BASE_RE = re.compile(r"^Checkpoint-Base: ([0-9a-f]{7,40})\s*$", re.M)


class Finding(NamedTuple):
    path: str
    foreign_sha: str
    removed: int


def is_own_commit(message: str, plan: Optional[str], base_sha: str) -> bool:
    """A run's own checkpoint commit: `Checkpoint-Plan:` names its plan AND `Checkpoint-Base:`
    is its run base. Session-Id is shared across concurrent runs, so it cannot discriminate."""
    if not plan:
        return False
    pm, bm = _PLAN_RE.search(message), _BASE_RE.search(message)
    if not pm or not bm:
        return False
    b = bm.group(1)
    return pm.group(1) == plan and (base_sha.startswith(b) or b.startswith(base_sha))


def _first_parent_to_base(common_dir: Path, head: str, base: str, cap: int = 5000) -> List[str]:
    """Shas newest->oldest from head, stopping before base. Empty-ish on a non-ancestor base (cap)."""
    out: List[str] = []
    sha: Optional[str] = head
    while sha and sha != base and len(out) < cap:
        meta = commit_meta(common_dir, sha)
        if meta is None:
            break
        out.append(sha)
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return out


def _blob_lines(common_dir: Path, blob: object) -> Optional[List[bytes]]:
    if blob is None or blob is ABSENT:
        return None
    obj = read_object(common_dir, blob)  # type: ignore[arg-type]
    if obj is None:
        return None
    return obj[1].splitlines(keepends=True)


def _opcodes(a: List[bytes], b: List[bytes]):
    """difflib opcodes after trimming the common prefix/suffix: a commit's edit is local, so the
    quadratic matcher sees only the changed middle."""
    n = min(len(a), len(b))
    lo_a, lo_b = 0, n  # largest k with a[:k] == b[:k], by C-speed slice compares
    while lo_a < lo_b:
        mid = (lo_a + lo_b + 1) // 2
        if a[:mid] == b[:mid]:
            lo_a = mid
        else:
            lo_b = mid - 1
    lo = lo_a
    room = n - lo
    h_a, h_b = 0, room
    while h_a < h_b:
        mid = (h_a + h_b + 1) // 2
        if a[len(a) - mid :] == b[len(b) - mid :]:
            h_a = mid
        else:
            h_b = mid - 1
    hi = h_a
    ops = []
    if lo:
        ops.append(("equal", 0, lo, 0, lo))
    sm = difflib.SequenceMatcher(None, a[lo : len(a) - hi], b[lo : len(b) - hi], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        ops.append((tag, i1 + lo, i2 + lo, j1 + lo, j2 + lo))
    if hi:
        ops.append(("equal", len(a) - hi, len(a), len(b) - hi, len(b)))
    return ops


def since_base_drops(
    common_dir: Path,
    head: str,
    base: str,
    landing: Dict[str, bytes],
    *,
    plan: Optional[str] = None,
    chain: Optional[List[str]] = None,
) -> List[Finding]:
    chain = chain if chain is not None else _first_parent_to_base(common_dir, head, base)
    chain = list(reversed(chain))  # oldest -> newest
    own = {
        sha
        for sha in chain
        if is_own_commit((commit_meta(common_dir, sha) or {}).get("message", ""), plan, base)
    }
    findings: List[Finding] = []
    for path, new_bytes in landing.items():
        head_blob = _blob_at_commit(common_dir, head, path)
        base_blob = _blob_at_commit(common_dir, base, path)
        if head_blob == base_blob or head_blob is None or head_blob is ABSENT:
            continue
        head_lines = _blob_lines(common_dir, head_blob)
        if head_lines is None or head_lines == new_bytes.splitlines(keepends=True):
            continue
        # HEAD lines the landing removes; additions-only landings end here without a history walk.
        tracked = [
            i
            for tag, i1, i2, _j1, _j2 in _opcodes(head_lines, new_bytes.splitlines(keepends=True))
            if tag in ("delete", "replace")
            for i in range(i1, i2)
        ]
        if not tracked:
            continue
        # Versions of the path newest -> oldest as (commit, blob); resolve each removed HEAD line
        # to the commit that introduced it by mapping its index back one version at a time.
        versions: List[tuple] = []
        prev: object = base_blob
        for sha in chain:  # oldest -> newest
            blob = _blob_at_commit(common_dir, sha, path)
            if blob is None or blob == prev:
                continue
            versions.append((sha, blob))
            prev = blob
        versions.reverse()
        removed_by: Dict[str, int] = {}
        cur = head_lines
        for k, (sha, _blob) in enumerate(versions):
            older_blob = versions[k + 1][1] if k + 1 < len(versions) else base_blob
            older = _blob_lines(common_dir, older_blob) or []
            still: List[int] = []
            ops = _opcodes(older, cur)
            for i in tracked:
                for tag, i1, _i2, j1, j2 in ops:
                    if j1 <= i < j2:
                        if tag == "equal":
                            still.append(i1 + (i - j1))
                        elif sha not in own:
                            removed_by[sha] = removed_by.get(sha, 0) + 1
                        break
            tracked, cur = still, older
            if not tracked or removed_by:
                break
        for sha, n in removed_by.items():
            findings.append(Finding(path, sha, n))
    return findings


class _Spawns:
    def __init__(self) -> None:
        self.n = 0
        self._orig = subprocess.Popen.__init__

    def __enter__(self) -> "_Spawns":
        outer = self
        orig = self._orig

        def counted(popen_self, *a, **k):  # type: ignore[no-untyped-def]
            outer.n += 1
            return orig(popen_self, *a, **k)

        subprocess.Popen.__init__ = counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


def _clear_caches() -> None:
    from coordinator_core.git import commit_walk, git_objects, rollback_check

    commit_walk._COMMIT_CACHE.clear()
    rollback_check._TREE_CACHE.clear()
    git_objects._OBJECT_CACHE.clear()


def measure(fn: Callable[[], object]) -> Dict[str, object]:
    _clear_caches()
    with _Spawns() as sp:
        t0 = time.process_time()
        result = fn()
        dt = (time.process_time() - t0) * 1000.0
    return {"process_ms": round(dt, 2), "spawns": sp.n, "result": result}


def _src_lines(seed: int, n: int) -> List[bytes]:
    return [f"    value_{seed}_{i} = compute({i}, {seed})  # line {i}\n".encode() for i in range(n)]


def _git_checked(args: Sequence[str], cwd: Path, input: Optional[bytes] = None, binary: bool = False):
    res = run_git(list(args), cwd=str(cwd), input=input, binary=binary)
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args)} failed ({res.returncode}): {res.stderr.strip()}")
    return res


def _fast_import(repo: Path, stream: bytes, delta: bool = False) -> None:
    _git_checked(["fast-import", "--quiet"] + ([] if delta else ["--depth=0"]), repo, input=stream)


def build_fixture(root: Path, n_paths: int, depth: int, touches: int, file_lines: int = 4500, delta: bool = False) -> Dict[str, object]:
    """Repo with `n_paths` files of `file_lines` lines: a base commit, then `depth` foreign
    commits that each edit `touches` paths round-robin (a path sees ~depth*touches/n_paths versions)."""
    repo = Path(tempfile.mkdtemp(prefix="fx_", dir=root))
    _git_checked(["init", "-q", "-b", "main"], repo)
    paths = [f"pkg/m{i}.py" for i in range(n_paths)]
    state = {p: _src_lines(i, file_lines) for i, p in enumerate(paths)}
    out = bytearray()
    mark = 0

    def commit(msg: str, files: Dict[str, List[bytes]], parent: Optional[int]) -> int:
        nonlocal mark
        mark += 1
        m = mark
        out.extend(f"commit refs/heads/main\nmark :{m}\ncommitter t <t@t> {1700000000 + m} +0000\n".encode())
        mb = msg.encode()
        out.extend(f"data {len(mb)}\n".encode() + mb + b"\n")
        if parent:
            out.extend(f"from :{parent}\n".encode())
        for p, ls in files.items():
            data = b"".join(ls)
            out.extend(f"M 100644 inline {p}\ndata {len(data)}\n".encode() + data + b"\n")
        return m

    parent = commit("base", dict(state), None)
    k = 0
    for c in range(depth):
        edited = {}
        for _ in range(touches):
            p = paths[k % n_paths]
            k += 1
            ls = list(state[p])
            ls.insert(100 + c, f"    foreign_add_{c} = 1\n".encode())
            state[p] = ls
            edited[p] = ls
        parent = commit(f"foreign {c}", edited, parent)
    _fast_import(repo, bytes(out), delta)
    head = read_ref_loose_or_packed(repo / ".git", "refs/heads/main")
    base = run_git(["rev-list", "--first-parent", "main"], cwd=str(repo)).stdout.split()[-1]
    # landing = HEAD copy with the foreign additions dropped (stale-copy shape) for every path
    landing = {}
    for i, p in enumerate(paths):
        landing[p] = b"".join(_src_lines(i, file_lines)) + b"# own edit\n"
    return {"repo": repo, "head": head, "base": base, "landing": landing, "paths": paths, "head_files": state}


def _row(tmp: Path, n_paths: int, depth: int, touches: int, lines: int, mode: str, delta: bool = False, note: str = "") -> Dict[str, object]:
    fx = build_fixture(tmp, n_paths, depth, touches, lines, delta)
    cd = Path(fx["repo"]) / ".git"  # type: ignore[arg-type]
    if mode == "clean":  # keeps every foreign line; edits one base line and appends an own line
        landing = {}
        for p, ls in fx["head_files"].items():  # type: ignore[union-attr]
            ls = list(ls)
            ls[10] = b"    changed_by_this_run = 1\n"
            landing[p] = b"".join(ls) + b"# own edit\n"
    else:
        landing = fx["landing"]  # type: ignore[assignment]
    m = measure(lambda: since_base_drops(cd, fx["head"], fx["base"], landing))  # type: ignore[arg-type]
    res = m.pop("result")
    m.update(paths=n_paths, depth=depth, edits_per_commit=touches, file_lines=lines, mode=mode, findings=len(res))  # type: ignore[arg-type]
    if note:
        m["note"] = note
    shutil.rmtree(fx["repo"], ignore_errors=True)  # type: ignore[arg-type]
    return m


def run_matrix(tmp: Path) -> List[Dict[str, object]]:
    rows = []
    for lines in (500, 4500):
        for mode in ("stale", "clean"):
            for n_paths in (1, 10, 35):
                for depth in (1, 20, 200):
                    rows.append(_row(tmp, n_paths, depth, min(2, n_paths), lines, mode))
    rows.append(_row(tmp, 35, 20, 35, 4500, "stale", note="worst-case: every commit edits every path"))
    rows.append(_row(tmp, 35, 20, 2, 4500, "stale", delta=True, note="delta-packed objects"))
    return rows


def replay_incident(repo_root: Path) -> Dict[str, object]:
    cd = repo_root / ".git"
    path = "coordinator_core/ops/dispatch_emit/terminal_commit.py"
    foreign, landing_commit = "a13b25919b", "2418107a64"

    def rev(s: str) -> str:
        return _git_checked(["rev-parse", s], repo_root).stdout.strip()

    f_sha, l_sha, base = rev(foreign), rev(landing_commit), rev(foreign + "^")
    head = rev(landing_commit + "^")  # state the halt run saw as HEAD when it landed
    blob = _git_checked(["show", f"{l_sha}:{path}"], repo_root, binary=True).stdout_bytes
    own_plan = "docs/plans/2026-10-16-review-edit-outside-declared-paths.md"
    m = measure(lambda: since_base_drops(cd, head, base, {path: blob}, plan=own_plan))
    res = m.pop("result")
    m.update(
        base=base[:10], head=head[:10], foreign=f_sha[:10], landing_blob=l_sha[:10] + ":" + path,
        findings=[f._asdict() for f in res],  # type: ignore[union-attr]
        names_foreign=any(f.foreign_sha == f_sha for f in res),  # type: ignore[union-attr]
    )
    try:
        import ast

        ast.parse(blob.decode())
        m["ast_parse"] = "parses"
    except SyntaxError as e:
        m["ast_parse"] = f"SyntaxError line {e.lineno}"
    return m


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    repo_root = Path(__file__).resolve().parents[2]
    out: Dict[str, object] = {"replay": replay_incident(repo_root)}
    if not args.replay_only:
        tmp = Path(tempfile.mkdtemp(prefix="since_base_probe_", dir=managed_tempdir()))
        try:
            out["matrix"] = run_matrix(tmp)
        finally:
            rmtree_or_raise(tmp, label="since_base_probe")
    print(json.dumps(out, indent=None if args.json else 2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
