# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""run-lineage-dag-suites.py — AC10 aggregator for the handoff-lineage-DAG +
chain-review-coverage workstreams. Runs all suites that exercise the DAG
primitive, the live-children guard, and the review-coverage gate, AND-combined
(any suite failing fails the whole run). Single entry point cited by
docs/plans/2026-06-30-chain-review-coverage-dag-consumer.md AC10.

Suites:
  1. node    — bin/lib/walk-handoff-dag.test.js          (referencedBy/walkForward + exclude)
  2. python  — bin/tests/test_handoff_has_live_children.py   (exclude + edge-kinds + exit-2)
  3. bats    — bin/tests/test-review-coverage-gate.bats        (DAG-mode walk/topology)
  4. bats    — bin/tests/test-review-coverage-gate-derivation.bats (segment derivation/guards/failure)

bats is invoked via `npx bats` (no global install required; bats >= 1.x). Suite 2 was
ported off bats to native Python in the 2026-07-19 Windows de-bash campaign (W1b) —
handoff-has-live-children.sh (its subject) was deleted, no bash left on this suite's path.

Port source: coordinator/bin/tests/run-lineage-dag-suites.sh (retired bash body on this
cutover; see git log for the prior implementation). No shell interpreter is spawned by
this port — every subprocess call below targets `node`, `python3` (`sys.executable`), or
`npx`, none of which is `bash`/`sh`, so no shell-out-carve-out entry is needed.

Run: python3 coordinator/bin/tests/run-lineage-dag-suites.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent   # coordinator/bin/tests
BIN_DIR = SCRIPT_DIR.parent                    # coordinator/bin


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
