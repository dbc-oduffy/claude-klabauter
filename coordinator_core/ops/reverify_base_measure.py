"""reverify-base-measure: which failing test ids are red at the run base (or a later HEAD).

Runs baseline_export (pytest inside an export of `--sha`) once over all ids, drops ids the base does
not contain, then runs once at `--head-sha` over the ids still green, and writes the file
review.reverify reads as `base_export_path`: one `FAILED <id>` line per red id and one trailing
`{"baseline_export": {...}}` JSON line. A bulk test run, outside the op budget by design.

Usage: python -m coordinator_core.ops.reverify_base_measure --sha <base> [--head-sha <sha>] --out <path> -- <ids...>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.ops.review_reverify import _RED_LINE
from coordinator_core.win_portability import no_console_creationflags

USAGE = "usage: reverify_base_measure --sha <base> [--head-sha <sha>] --out <path> -- <ids...>"
EXIT_OK = 0
EXIT_USAGE = 2
_PYTEST_USAGE_ERROR = 4
_NOT_FOUND = re.compile(r"not found:\s+(\S+)")
_ENGINE_ROOT = Path(__file__).resolve().parents[2]
#: Runs baseline_export with the live engine importable by sys.path only: a PYTHONPATH entry would
#: leak into the runner and shadow the exported tree's own packages.
_LAUNCH = "import sys; sys.path.insert(0, sys.argv[1]); from coordinator_core.testing.baseline_export import main; sys.exit(main(sys.argv[2:]))"


def _parse(argv: list[str]) -> Optional[tuple[str, Optional[str], str, list[str]]]:
    if "--" not in argv:
        return None
    cut = argv.index("--")
    head, ids = argv[:cut], argv[cut + 1:]
    flags: dict[str, str] = {}
    i = 0
    while i < len(head):
        if head[i] not in ("--sha", "--head-sha", "--out") or i + 1 >= len(head) or head[i] in flags:
            return None
        flags[head[i]] = head[i + 1]
        i += 2
    if not flags.get("--sha") or not flags.get("--out") or not ids:
        return None
    return flags["--sha"], flags.get("--head-sha"), flags["--out"], ids


def _run(sha: str, ids: list[str], cwd: str) -> tuple[str, Optional[int], list[str], list[str]]:
    """One baseline_export run: (export state, runner exit, stdout lines, stderr lines)."""
    argv = [sys.executable, "-c", _LAUNCH, str(_ENGINE_ROOT), "--sha", sha, "--",
            sys.executable, "-m", "pytest", "-rfE", *ids]
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace",
                          **no_console_creationflags())
    lines = proc.stdout.splitlines()
    stderr_lines = proc.stderr.splitlines()
    tail = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    try:
        result = json.loads(tail).get("baseline_export")
    except (ValueError, AttributeError):
        result = None
    if not isinstance(result, dict):
        return "failed", None, lines, stderr_lines
    return str(result.get("export")), result.get("runner_exit"), lines, stderr_lines


def _red(lines: list[str]) -> set[str]:
    return {hit.group(1) for hit in (_RED_LINE.match(ln.strip()) for ln in lines) if hit}


def _not_found(lines: list[str], ids: list[str]) -> list[str]:
    """Requested ids a pytest 'not found' line names (a path may carry the rootdir prefix)."""
    named = [m.group(1) for m in (_NOT_FOUND.search(ln) for ln in lines) if m]
    return [i for i in ids if any(n == i or n.endswith("/" + i) or n.endswith(os.sep + i) for n in named)]


def _is_red(node: str, red: set[str]) -> bool:
    return any(r == node or r.startswith(node + "::") or r.startswith(node.rstrip("/") + "/") for r in red)


def measure(sha: str, head_sha: Optional[str], ids: list[str], cwd: str) -> dict:
    """The red ids and the trailing-line payload for `ids` measured at `sha` (and `head_sha`)."""
    remaining = list(ids)
    export, runner_exit, lines, err = _run(sha, remaining, cwd)
    # pytest names one missing id per run (on stderr), so each retry drops the next.
    for _ in range(len(ids)):
        if export != "ok" or runner_exit != _PYTEST_USAGE_ERROR:
            break
        gone = _not_found(err + lines, remaining)
        if not gone:
            break
        remaining = [i for i in remaining if i not in gone]
        if not remaining:
            break
        export, runner_exit, lines, err = _run(sha, remaining, cwd)
    red: set[str] = set()
    passed: list[str] = []
    if export == "ok" and runner_exit in (0, 1) and remaining:
        red = _red(lines)
        passed = [i for i in remaining if not _is_red(i, red)]
    if head_sha and export == "ok" and runner_exit in (0, 1) and passed:
        h_export, h_exit, h_lines, _h_err = _run(head_sha, passed, cwd)
        if h_export != "ok" or h_exit not in (0, 1):
            export = h_export if h_export != "ok" else "failed"
        else:
            red |= _red(h_lines)
    return {"red": sorted(red), "export": export, "runner_exit": runner_exit, "passed_at_base": passed}


def main(argv: list[str]) -> int:
    parsed = _parse(list(argv))
    if parsed is None:
        print(f"reverify_base_measure: bad arguments\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    sha, head_sha, out, ids = parsed
    cwd = os.path.realpath(os.getcwd())
    result = measure(sha, head_sha, ids, cwd)
    body = [f"FAILED {node}" for node in result["red"]]
    body.append(json.dumps({"baseline_export": {
        "export": result["export"],
        "runner_exit": result["runner_exit"],
        "passed_at_base": result["passed_at_base"],
    }}))
    path = Path(out) if Path(out).is_absolute() else Path(cwd) / out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
