"""test_verdict.record — the sanctioned door that records a test-runner's terminal verdict.

The only input is the runner's structured result; the verdict is derived from its counts, never typed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.completion_receipts.test_verdict import (
    TestVerdictRefused,
    derive_verdict,
    record_test_verdict,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _record(root: Path, result: dict, plan: Optional[str]) -> dict:
    path = record_test_verdict(root, result, plan_path=plan)
    return {
        "status": "recorded",
        "sidecar": _rel(root, path),
        "test_verdict": derive_verdict(result),
    }


@register_op("test_verdict.record")
def _record_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: result (the runner's test_result dict), optional plan. A TestVerdictRefused
    propagates as the op error."""
    root = main_worktree_root(repo_root) if repo_root else Path(params.get("repo_root") or ".")
    return _record(root, params["result"], params.get("plan"))


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="test-verdict")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="record the runner's terminal verdict into its sidecar")
    rec.add_argument("--result-json", required=True, help="the runner's test_result JSON, or a path to it")
    rec.add_argument("--repo-root", default=None)
    rec.add_argument("--plan", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.repo_root) if args.repo_root else Path.cwd()
    raw = args.result_json
    try:
        if not raw.lstrip().startswith("{"):
            raw = Path(raw).read_text(encoding="utf-8")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise TestVerdictRefused("result is not a JSON object")
        out = _record(root, result, args.plan)
    except (OSError, ValueError) as exc:
        print(f"test-verdict: {exc}", file=sys.stderr)
        return 1
    print(out["sidecar"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
