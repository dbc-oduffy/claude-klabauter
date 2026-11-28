"""Stamp-to-published split for a percolate round, with the ten-minute gate.

Runs the real round on a disposable clone (``percolate_round_ab``), or parses a
captured production round with ``--from-log``, and prints one row per driver
step and per publish phase, a total, and ``ten_minute_gate: pass|fail``.

The disposable mode runs ``--no-publish``: it has no push leg and cannot alone
evidence stamp-to-published. Gate a round that pushes with ``--from-log``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks import percolate_round_ab as _ab

TEN_MINUTE_BUDGET_S = 600.0

_STEP_RE = re.compile(
    r"^\s*\[timing\]\s+([^:]+):\s+step:([^:\s]+):\s+wall=([\d.]+)s\s+cpu=([\d.]+)s"
)

# A round that pushed prints one of these on stdout; a failed push prints `push failed:` on stderr.
_PUBLISHED_RE = re.compile(
    r"^(?:Published: pushed to |percolate-round: pushed \d+ unpushed commit)", re.MULTILINE
)
_PUSH_FAILED_RE = re.compile(r"^percolate-round: push failed:", re.MULTILINE)

# Steps excluded from the top-level sum: the probe runs before the round body and is covered by `step:total`.
_NESTED_STEPS = frozenset({"engine_import_probe"})


def parse_timing_text(text: str) -> dict:
    """Parse publish phase lines and C1a `step:` lines out of round output.

    Pass stdout and stderr joined: the materialise step is emitted on stderr.
    """
    steps: List[dict] = []
    phases: List[dict] = []
    for line in text.splitlines():
        m = _STEP_RE.match(line)
        if m:
            round_label, name, wall_s, cpu_s = m.groups()
            steps.append(
                {"round": round_label, "step": name,
                 "wall_s": float(wall_s), "cpu_s": float(cpu_s)}
            )
            continue
        m = _ab._TIMING_PHASE_RE.match(line)
        if m:
            row_label, phase_label, wall_s, cpu_s = m.groups()
            phases.append(
                {"row": row_label, "phase": phase_label,
                 "wall_s": float(wall_s), "cpu_s": float(cpu_s)}
            )
    return {"steps": steps, "phases": phases}


def total_wall_s(parsed: dict) -> Optional[float]:
    """Total round wall: the larger of `step:total` and the sum of top-level steps.

    The max covers a `step:total` that excludes the parent-emitted materialise
    leg; nested steps are never summed. None when no step line was seen.
    """
    steps = parsed["steps"]
    declared = [s["wall_s"] for s in steps if s["step"] == "total"]
    summed = [
        s["wall_s"] for s in steps
        if s["step"] != "total" and s["step"] not in _NESTED_STEPS
    ]
    candidates = declared + ([sum(summed)] if summed else [])
    return max(candidates) if candidates else None


def evaluate(
    parsed: dict,
    *,
    timed_out: bool = False,
    rc: Optional[int] = None,
    published: Optional[bool] = None,
) -> dict:
    """Apply the gate: pass iff a total was measured, <= 600s, round not timed out or failed.

    `published=False` (a log with no push-success marker) fails the gate; None means not asserted.
    """
    total = total_wall_s(parsed)
    if timed_out:
        total = max(total or 0.0, float(_ab._REAL_ROUND_TIMEOUT_S))
    passed = (
        total is not None
        and total <= TEN_MINUTE_BUDGET_S
        and not timed_out
        and rc in (None, 0)
        and published is not False
    )
    return {
        **parsed,
        "total_wall_s": total,
        "timed_out": timed_out,
        "rc": rc,
        "published": published,
        "ten_minute_gate": "pass" if passed else "fail",
    }


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_disposable(target: str, scratch_root: Optional[Path] = None) -> dict:
    """Run the real round (--no-publish) on a disposable clone and evaluate it."""
    try:
        cap = _ab.capture_real_round_phase_breakdown(target, scratch_root=scratch_root)
    except subprocess.TimeoutExpired as exc:
        text = _as_text(exc.stdout) + "\n" + _as_text(exc.stderr)
        return evaluate(parse_timing_text(text), timed_out=True)
    text = cap["stdout"] + "\n" + cap["stderr"]
    result = evaluate(parse_timing_text(text), rc=cap["rc"])
    result["dest"] = cap["dest"]
    return result


def run_from_log(path: Path) -> dict:
    """Parse and gate a captured production round (stdout+stderr combined).

    The log must carry the push-success line and no `push failed:` line, else the gate fails.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    published = bool(_PUBLISHED_RE.search(text)) and not _PUSH_FAILED_RE.search(text)
    return evaluate(parse_timing_text(text), published=published)


def format_report(result: dict) -> str:
    lines = ["driver steps (wall / cpu):"]
    for s in result["steps"]:
        lines.append(f"  step:{s['step']}: {s['wall_s']:.1f}s wall / {s['cpu_s']:.1f}s cpu")
    lines.append("publish phases (wall / cpu):")
    for p in result["phases"]:
        lines.append(
            f"  {p['row']}: {p['phase']}: {p['wall_s']:.1f}s wall / {p['cpu_s']:.1f}s cpu"
        )
    total = result["total_wall_s"]
    lines.append(
        "total_wall: " + (f"{total:.1f}s" if total is not None else "unmeasured")
        + (" (round timed out)" if result["timed_out"] else "")
    )
    if result.get("published") is False:
        lines.append("published: no push-success line in the log")
    lines.append(f"ten_minute_gate: {result['ten_minute_gate']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", default=_ab._DEFAULT_TARGET)
    parser.add_argument(
        "--from-log", default=None,
        help="parse a captured production round (stdout+stderr) instead of running one",
    )
    parser.add_argument("--scratch-root", default=None)
    parser.add_argument("--json", default=None, help="also write the result as JSON")
    args = parser.parse_args(argv)

    if args.from_log:
        result = run_from_log(Path(args.from_log))
    else:
        scratch = Path(args.scratch_root) if args.scratch_root else None
        result = run_disposable(args.target, scratch)
    print(format_report(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ten_minute_gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
