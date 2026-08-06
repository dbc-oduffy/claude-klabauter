"""
coordinator_core.ops.check_wsc_inline_budget — inline bash-block budget gate
for workstream-complete/SKILL.md.

Purpose: counts ```bash fenced code blocks in the wsc SKILL.md and compares
against a stored baseline integer. Inline bash blocks are a proxy for
"mechanism logic that should live in a bin/wsc-*.sh script instead of
inline in the skill." The check surfaces a WARN when the count grows.

Spec backlink: wsc-asic task (2026-06-30) / skill-step-parallelization.md § wsc wiring rule
Port of: check-wsc-inline-budget.sh (example-doctrine-repo b5a4192c, 2026-07-20)

Exit codes (parity-critical — caller branches on these):
  0 — count within baseline (or no baseline file — safe to ship before finalization)
  1 — count exceeds baseline (WARN — non-blocking by caller convention)
  2 — fatal error (SKILL.md not found)

Negative-spec:
    - Does NOT use a Markdown/YAML parser — counts literal ```bash opening
      fence lines the same way the bash oracle's `awk '/^```bash/{c++}'`
      does: a line-by-line prefix match on `^```bash`, not a proper fenced-
      code-block parser. A stray ` ```bash` inside a non-code context would
      still be counted, mirroring the bash oracle's own imprecision — this
      is a faithful port, not a fix.
    - Callers invoke this as a WARN, non-blocking check
      (`coordinator/commands/workweek-complete.md`); the trampoline itself
      still exits 1 on budget-exceeded — masking exit 1 into a WARN is the
      caller's responsibility (`| tail -1` in the current wiring), not this
      module's.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional


def count_bash_fences(skill_text: str) -> int:
    """Count lines beginning with ```bash — mirrors the bash oracle's
    `awk '/^```bash/{c++} END{print c+0}'` line-prefix match exactly."""
    return sum(1 for line in skill_text.splitlines() if line.startswith("```bash"))


def check(skill_path: str, baseline_path: str) -> tuple[str, int]:
    """Core predicate — mirrors the bash oracle's read-count-compare body.

    Returns (message, exit_code).
    """
    skill_file = Path(skill_path)
    if not skill_file.is_file():
        return (f"ERROR: SKILL.md not found at {skill_path}", 2)

    current = count_bash_fences(skill_file.read_text(encoding="utf-8"))

    baseline_file = Path(baseline_path)
    if not baseline_file.is_file():
        return (
            f"INFO: no baseline set yet — current inline bash-block count: {current}",
            0,
        )

    baseline_raw = baseline_file.read_text(encoding="utf-8").strip()
    baseline = int(baseline_raw)

    if current > baseline:
        return (
            f"WARN: workstream-complete inline bash-block count {current} exceeds "
            f"baseline {baseline} — new mechanism should be a bin/wsc-*.sh script, "
            "not inline (see skill-step-parallelization.md § wsc wiring rule)",
            1,
        )

    return (f"OK: inline bash-block count {current} within baseline {baseline}", 0)


def main(argv: List[str]) -> int:
    """CLI entry: resolves default skill/baseline paths relative to this
    module unless overridden, prints the check message, returns rc.

    argv[0] (optional) — skill_path override.
    argv[1] (optional) — baseline_path override.
    Mirrors the bash oracle's WSC_SKILL_PATH / WSC_BASELINE_FILE env-var
    override convention via positional args at this layer; the example-doctrine-repo
    trampoline is responsible for translating its own env vars into argv.
    """
    skill_path: Optional[str] = argv[0] if len(argv) > 0 else None
    baseline_path: Optional[str] = argv[1] if len(argv) > 1 else None

    if skill_path is None or baseline_path is None:
        print(
            "check_wsc_inline_budget: missing required skill_path/baseline_path arguments",
            file=sys.stderr,
        )
        return 2

    message, rc = check(skill_path, baseline_path)
    stream = sys.stderr if rc == 2 else sys.stdout
    print(message, file=stream)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
