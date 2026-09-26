from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional


def count_bash_fences(skill_text: str) -> int:
    return sum(1 for line in skill_text.splitlines() if line.startswith("```bash"))


def check(skill_path: str, baseline_path: str) -> tuple[str, int]:
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
    override convention via positional args at this layer; the DoE
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
