"""Per-phase driver-CPU comparator over publish round logs.

Parses `[timing] <row>: <label>: Ns wall / Ns cpu (driver only` lines, sums driver CPU per
phase over every row, and compares an after-log against a baseline log. Exit 0 = every phase
within bounds; 1 = a phase regressed; 2 = the two logs cover different row sets (no ratio is
reported, since phase CPU scales with the row set) or a log has no timing lines.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PHASE_LABELS: dict[str, str] = {
    "pre_sync_gates": "run_pre_sync_gates",
    "payload_parity": "dispatch_preswap_payload_parity_gate",
    "post_rsync": "dispatch_percolate_post_rsync",
    "repo_key_literal": "dispatch_end_of_run_repo_key_literal_gate",
    "pre_ci": "dispatch_percolate_pre_ci",
    "mirror_like": "dispatch_mirror_like",
}

RATIO_THRESHOLD = 1.5
REPO_KEY_LITERAL_ABSOLUTE_S = 10.0
ZERO_BASELINE_FLOOR_S = 0.1
ROUND_PSEUDO_ROW = "<round>"

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ROWSET_DIFFERS = 2

_TIMING_RE = re.compile(
    r"\[timing\]\s+(?P<row>[^\s:]+):\s+(?P<label>[^\s:]+):\s+"
    r"(?P<wall>[\d.]+)s wall / (?P<cpu>[\d.]+)s cpu \(driver only"
)


def parse_log(text: str) -> tuple[dict[str, float], set[str]]:
    """Return (phase -> summed driver CPU seconds, set of real row names) for one log.

    The `<round>` pseudo-row contributes to phase sums but is not a publish row.
    """
    label_to_phase = {label: phase for phase, label in PHASE_LABELS.items()}
    totals = {phase: 0.0 for phase in PHASE_LABELS}
    rows: set[str] = set()
    for line in text.splitlines():
        m = _TIMING_RE.search(line)
        if not m:
            continue
        row = m.group("row")
        if row != ROUND_PSEUDO_ROW:
            rows.add(row)
        phase = label_to_phase.get(m.group("label"))
        if phase is not None:
            totals[phase] += float(m.group("cpu"))
    return totals, rows


def compare(
    after: dict[str, float], baseline: dict[str, float]
) -> tuple[list[str], list[str]]:
    """Return (report lines, names of regressed phases)."""
    lines: list[str] = []
    regressed: list[str] = []
    for phase in PHASE_LABELS:
        a, b = after[phase], baseline[phase]
        if phase == "repo_key_literal":
            bad = a > REPO_KEY_LITERAL_ABSOLUTE_S
            bound = f"abs<={REPO_KEY_LITERAL_ABSOLUTE_S:.1f}s"
        else:
            bad = a > RATIO_THRESHOLD * max(b, ZERO_BASELINE_FLOOR_S)
            bound = f"<={RATIO_THRESHOLD}x"
        ratio = f"{a / b:.1f}x" if b > 0 else "n/a"
        status = "REGRESSED" if bad else "ok"
        lines.append(
            f"{phase:<18} baseline {b:8.1f}s  after {a:8.1f}s  ratio {ratio:>7}  "
            f"({bound})  {status}"
        )
        if bad:
            regressed.append(phase)
    return lines, regressed


def run(after_path: Path, baseline_path: Path) -> tuple[int, str]:
    """Compare two logs; return (exit code, report text)."""
    after, after_rows = parse_log(after_path.read_text(encoding="utf-8", errors="replace"))
    base, base_rows = parse_log(baseline_path.read_text(encoding="utf-8", errors="replace"))
    if not after_rows or not base_rows:
        empty = after_path if not after_rows else baseline_path
        return EXIT_ROWSET_DIFFERS, f"no driver-only timing lines in {empty}"
    if after_rows != base_rows:
        only_after = sorted(after_rows - base_rows)
        only_base = sorted(base_rows - after_rows)
        return EXIT_ROWSET_DIFFERS, (
            "row sets differ: no comparison made. "
            f"only in after: {only_after}; only in baseline: {only_base}"
        )
    lines, regressed = compare(after, base)
    if regressed:
        lines.append(f"REGRESSED phases: {', '.join(regressed)}")
        return EXIT_REGRESSION, "\n".join(lines)
    lines.append("all phases within bounds")
    return EXIT_OK, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("after_log", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args(argv)
    code, report = run(args.after_log, args.baseline)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
