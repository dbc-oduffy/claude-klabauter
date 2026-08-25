"""coordinator_core.write_guards.__main__ — module entrypoint for the Write/Edit guard engine.

Mirrors coordinator_core.subagent_sandbox.__main__. Reads the raw PreToolUse JSON
payload on stdin, evaluates all Write/Edit guards, writes the single
hookSpecificOutput envelope to stdout on a deny/advisory (nothing on allow),
exit 0 always (hook contract — ALLOW/DENY conveyed via stdout, never exit code).

Primary consumer is DoE's naked-Python PreToolUse dispatcher, which imports
`evaluate_payload_json` IN-PROCESS (no subprocess). This CLI entry exists for
parity with subagent_sandbox and for standalone testing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from coordinator_core.write_guards.engine import evaluate_payload_json


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.write_guards",
        description="PreToolUse Write/Edit guard engine.",
    )
    parser.add_argument("--policy", dest="policy_path", default=None,
                        help="Path to subagent-sandbox-policy.yaml (for the reused sandbox guard).")
    parser.add_argument("--cwd", dest="cwd", default=None,
                        help="Working directory to resolve the target git root from.")
    args = parser.parse_args(argv)

    hook_output = evaluate_payload_json(
        sys.stdin.read(), policy_path=args.policy_path, cwd=args.cwd
    )
    if hook_output is not None:
        sys.stdout.write(json.dumps(hook_output))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
