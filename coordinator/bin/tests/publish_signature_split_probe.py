"""Falsifier probe for the publish.py delta-signature split (not collected: no `test_` prefix).

Mode 1 (default): materialises publish.py at `--before` and `--after` (a gate-only commit pair) via
`git show <sha>:coordinator/bin/publish.py` into a row-owned dir under repo `scratch/`, computes the
publish.py signature component of each with the LIVE publish.py's own function, plus a synthetic
body edit to one transform root, and prints one JSON line:
{gate_edit_equal, transform_edit_equal, component_fn, ...}. The component is
`_hash_transform_closure` once that exists in the live publish.py, else `_hash_bytes_of` (whole file).
Healthy after the split: gate_edit_equal true, transform_edit_equal false.

Mode 2 (`--timing-log FILE`): parses `[timing]` lines of a cold-round log and prints per-phase
process (cpu) seconds as JSON, largest first.

Run: python coordinator/bin/tests/publish_signature_split_probe.py [--before SHA --after SHA]
     python coordinator/bin/tests/publish_signature_split_probe.py --timing-log round.log
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PUBLISH_REL = "coordinator/bin/publish.py"
_DEFAULT_AFTER = "HEAD"
_TRANSFORM_ROOT_CANDIDATES = ("_publish_copy_file", "dispatch_mirror_like", "_materialize_tree_paths")
_TIMING_RE = re.compile(r"\[timing\]\s+(.+?):\s+(\d+(?:\.\d+)?)s wall\s*/\s*(\d+(?:\.\d+)?)s cpu")


def _load_publish():
    sys.path.insert(0, str(_REPO))
    spec = importlib.util.spec_from_file_location("publish_signature_probe_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git_show(sha: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(_REPO), "show", f"{sha}:{_PUBLISH_REL}"],
        check=True,
        capture_output=True,
        creationflags=_NO_WINDOW,
    ).stdout


def _edit_transform_root(source: bytes) -> "tuple[bytes, str]":
    """Insert one statement into the first body line of the first transform root present."""
    text = source.decode("utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in _TRANSFORM_ROOT_CANDIDATES:
        fn = defs.get(name)
        if fn is None:
            continue
        first = fn.body[0]
        is_doc = isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str)
        if is_doc and len(fn.body) > 1:
            anchor = fn.body[1]
        else:
            anchor = first
        at = anchor.lineno - 1
        lines.insert(at, " " * anchor.col_offset + "_c0_probe_marker = 1\n")
        return "".join(lines).encode("utf-8"), name
    raise SystemExit(f"no transform root candidate {_TRANSFORM_ROOT_CANDIDATES} found in publish.py")


def run_signature_split(before: str, after: str) -> dict:
    publish = _load_publish()
    fn = getattr(publish, "_hash_transform_closure", None)
    component = fn if fn is not None else publish._hash_bytes_of
    work = _REPO / "scratch" / "c0-publish-signature-split-probe"
    work.mkdir(parents=True, exist_ok=True)
    try:
        blobs = {"before": _git_show(before), "after": _git_show(after)}
        edited, root = _edit_transform_root(blobs["after"])
        blobs["transform_edit"] = edited
        sigs = {}
        for label, blob in blobs.items():
            p = work / f"publish_{label}.py"
            p.write_bytes(blob)
            sigs[label] = component(p)
        return {
            "before": before,
            "after": after,
            "component_fn": "_hash_transform_closure" if fn is not None else "_hash_bytes_of",
            "transform_root_edited": root,
            "gate_edit_equal": sigs["before"] == sigs["after"],
            "transform_edit_equal": sigs["after"] == sigs["transform_edit"],
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def parse_timing_log(text: str) -> dict:
    """Sum cpu/wall seconds per phase label (row name stripped when `row: phase` shaped)."""
    phases: "dict[str, dict[str, float]]" = {}
    for line in text.splitlines():
        m = _TIMING_RE.search(line)
        if not m:
            continue
        label = m.group(1).strip()
        _row, sep, phase = label.partition(": ")
        key = phase if sep else label
        slot = phases.setdefault(key, {"cpu_s": 0.0, "wall_s": 0.0, "lines": 0})
        slot["wall_s"] += float(m.group(2))
        slot["cpu_s"] += float(m.group(3))
        slot["lines"] += 1
    ranked = sorted(phases.items(), key=lambda kv: kv[1]["cpu_s"], reverse=True)
    return {"phases": [{"phase": k, **{a: round(b, 3) for a, b in v.items()}} for k, v in ranked]}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--before", default=None, help="gate-edit parent sha (default: <after>^)")
    ap.add_argument("--after", default=_DEFAULT_AFTER)
    ap.add_argument("--timing-log", default=None, help="mode 2: cold-round log to parse")
    args = ap.parse_args(argv)
    if args.timing_log:
        out = parse_timing_log(Path(args.timing_log).read_text(encoding="utf-8", errors="replace"))
    else:
        out = run_signature_split(args.before or f"{args.after}^", args.after)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
