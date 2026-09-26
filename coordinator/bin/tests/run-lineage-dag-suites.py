# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BIN_DIR = SCRIPT_DIR.parent


def run(label: str, cmd: list[str]) -> bool:
    print(f"=== {label} ===")
    result = subprocess.run(
        cmd,
        cwd=str(SCRIPT_DIR.parent.parent.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    ok = result.returncode == 0
    print(f"--- {label}: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    results = [
        run("node: walk-handoff-dag", ["node", "--test", str(BIN_DIR / "lib" / "walk-handoff-dag.test.js")]),
        run(
            "python: handoff-has-live-children",
            [sys.executable, "-m", "pytest", str(SCRIPT_DIR / "test_handoff_has_live_children.py"), "-q"],
        ),
        run("bats: review-coverage-gate", ["npx", "bats", str(SCRIPT_DIR / "test-review-coverage-gate.bats")]),
        run(
            "bats: review-coverage-gate-derivation",
            ["npx", "bats", str(SCRIPT_DIR / "test-review-coverage-gate-derivation.bats")],
        ),
    ]

    rc = 0 if all(results) else 1
    if rc == 0:
        print("########## run-lineage-dag-suites: ALL SUITES GREEN ##########")
    else:
        print("########## run-lineage-dag-suites: ONE OR MORE SUITES FAILED ##########", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
