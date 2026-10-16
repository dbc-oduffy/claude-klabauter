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
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit.verdict_supersession import record_tests_supersession


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _plan_id(root: Path, plan: str) -> Optional[str]:
    """The plan's frontmatter `plan_id`; None when the file, frontmatter or field is absent."""
    plan_file = Path(plan)
    if not plan_file.is_absolute():
        plan_file = root / plan_file
    try:
        split = split_frontmatter(plan_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return read_fm_field_unquoted(split.fm_text, "plan_id") or None if split else None


def _record(root: Path, result: dict, plan: Optional[str]) -> dict:
    path = record_test_verdict(root, result, plan_path=plan)
    verdict = derive_verdict(result)
    sidecar = _rel(root, path)
    tests_record = None
    plan_id = _plan_id(root, plan) if plan else None
    if plan_id:
        tests_record = record_tests_supersession(
            repo_root=root,
            plan_id=plan_id,
            tests={
                "status": verdict,
                "run": result.get("tests_run"),
                "failed": result.get("tests_failed"),
                "sidecar": sidecar,
                "baseline_method": result.get("baseline_method"),
                "baseline": result.get("baseline"),
            },
        )
    return {
        "status": "recorded",
        "sidecar": sidecar,
        "test_verdict": verdict,
        "tests_record": tests_record,
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
