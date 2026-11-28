"""Profile the publish import-closure gate over a restricted tree: process time per phase, spawns, files touched.

Prints one JSON line. Process time is time.process_time, never wall clock. With --manifest the tree is
diffed against an expected-manifest and the gate is called incrementally when it accepts `changed`/`baseline`.
Runs as `python -m coordinator_core.benchmarks.import_closure_gate_profile <tree>`.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
for _entry in (_REPO, _REPO / "coordinator" / "lib"):
    if os.fspath(_entry) not in sys.path:
        sys.path.insert(0, os.fspath(_entry))

from percolate import import_closure as ic  # noqa: E402


def _blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class _Counters:
    def __init__(self, root: Path) -> None:
        self.root = os.fspath(root)
        self.spawns = 0
        self.stat: set[str] = set()
        self.opened: set[str] = set()

    def under_tree(self, path) -> str | None:
        text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
        return text if text.endswith(".py") and text.startswith(self.root) else None


@contextmanager
def _counting(counters: _Counters) -> Generator[None]:
    real_popen_init = subprocess.Popen.__init__
    real_stat = os.stat
    real_read_bytes = Path.read_bytes
    real_open = Path.open

    def popen_init(self: Any, *a: Any, **kw: Any) -> None:
        counters.spawns += 1
        real_popen_init(self, *a, **kw)

    def stat(path: Any, *a: Any, **kw: Any) -> os.stat_result:
        hit = counters.under_tree(path)
        if hit:
            counters.stat.add(hit)
        return real_stat(path, *a, **kw)

    def read_bytes(self: Path) -> bytes:
        hit = counters.under_tree(self)
        if hit:
            counters.opened.add(hit)
        return real_read_bytes(self)

    def path_open(self: Path, *a: Any, **kw: Any) -> Any:
        hit = counters.under_tree(self)
        if hit:
            counters.opened.add(hit)
        return real_open(self, *a, **kw)

    setattr(subprocess.Popen, "__init__", popen_init)
    setattr(os, "stat", stat)
    setattr(Path, "read_bytes", read_bytes)
    setattr(Path, "open", path_open)
    try:
        yield
    finally:
        setattr(subprocess.Popen, "__init__", real_popen_init)
        setattr(os, "stat", real_stat)
        setattr(Path, "read_bytes", real_read_bytes)
        setattr(Path, "open", real_open)


def phase_split(tree_root: Path) -> dict:
    """Process ms of stat, read, hash, parse and resolve over every `.py` under `tree_root`, each phase run alone."""
    ms: dict[str, float] = {}

    def timed(name: str, fn: Callable[[], Any]) -> Any:
        start = time.process_time()
        out = fn()
        ms[name] = round((time.process_time() - start) * 1000.0, 1)
        return out

    files = timed("stat", lambda: [(p, p.stat()) for p in sorted(tree_root.rglob("*.py"))])
    raws = timed("read", lambda: [(p.relative_to(tree_root).as_posix(), p.read_bytes()) for p, _ in files])
    timed("hash", lambda: [(hashlib.sha256(raw).hexdigest(), _blob_sha1(raw)) for _, raw in raws])
    facts = timed("parse", lambda: [(rel, ic._file_facts(raw, rel, None)) for rel, raw in raws])
    init_attrs = ic._package_init_attribute_names(tree_root)

    def resolve() -> int:
        seen: dict[str, bool] = {}
        for _, f in facts:
            if f.get("syntax_error"):
                continue
            for entry in (*f["bare"], *f["refs"]):
                if entry not in seen:
                    seen[entry] = ic._resolves_in_tree(tree_root, entry) or entry in init_attrs
        return len(seen)

    timed("resolve", resolve)
    return {"phase_ms": ms, "files": len(files)}


def diff_against_manifest(tree_root: Path, manifest: Path, prefix: str) -> tuple[set[str], set[str]]:
    """`(changed, removed)` tree-relative paths: `.py` blobs that differ from or are absent in the manifest, and
    manifest `.py` entries under `prefix` that the tree lacks."""
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    paths = {p[len(prefix):]: sha for p, sha in doc["paths"].items() if p.startswith(prefix) and p.endswith(".py")}
    present = {p.relative_to(tree_root).as_posix() for p in tree_root.rglob("*.py")}
    changed = {rel for rel in present if paths.get(rel) != _blob_sha1((tree_root / rel).read_bytes())}
    return changed, set(paths) - present


def run_gate(tree_root: Path, source_root: Path | None, tests_only: bool, kwargs: dict) -> dict:
    """The real gate call, with spawns and files touched counted."""
    counters = _Counters(tree_root)
    start = time.process_time()
    with _counting(counters):
        examined, violations = ic.find_import_closure_violations(
            tree_root, source_root, tests_only=tests_only, **kwargs
        )
    return {
        "process_ms": round((time.process_time() - start) * 1000.0, 1),
        "spawns": counters.spawns,
        "files_stat": len(counters.stat),
        "files_opened": len(counters.opened),
        "files_graded": examined,
        "examined": examined,
        "violations": [list(v) for v in violations],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree", type=Path, help="restricted tree root to grade")
    ap.add_argument("--source-root", type=Path, default=None)
    ap.add_argument("--manifest", type=Path, default=None, help="expected-manifest.json to diff the tree against")
    ap.add_argument("--prefix", default="", help="manifest path prefix that maps to the tree root, e.g. coordinator_core/")
    ap.add_argument("--tests-only", action="store_true")
    ap.add_argument("--no-phases", action="store_true", help="skip the per-phase split")
    args = ap.parse_args(argv)
    tree = args.tree.resolve()
    if not tree.is_dir():
        print(f"import_closure_gate_profile: not a directory: {tree}", file=sys.stderr)
        return 1

    row: dict = {"tree": os.fspath(tree), "mode": "full"}
    kwargs: dict = {}
    if args.manifest is not None:
        changed, removed = diff_against_manifest(tree, args.manifest, args.prefix)
        row.update(changed=len(changed), removed=len(removed))
        params = inspect.signature(ic.find_import_closure_violations).parameters
        if "changed" in params and "baseline" in params:
            kwargs = {"changed": changed, "baseline": None}
            row["mode"] = "incremental"
        else:
            row["incremental_unsupported"] = True
    if not args.no_phases:
        row.update(phase_split(tree))
    row.update(run_gate(tree, args.source_root.resolve() if args.source_root else None, args.tests_only, kwargs))
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
