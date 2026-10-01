"""record-superseding-review.py -- CLI over review_mint.record_superseding_review.

Writes the stranded-run superseding review record and prints its
repo-root-relative path as the ONLY line on stdout, so
`/workstream-complete`'s `{d-record-superseding-review.entry_path}` token
threads it into `review-stamp mint --superseding-record`.

Usage:
  record-superseding-review.py --plan <path> --session-id <sid> --base <sha> --head <sha>
      [--wave-sidecar <path> ...] [--prep-sidecar <path>]
      [--stage-returns-json <json>] [--supersedes <sha>] [--repo-root <path>]

Exit codes:
  0 -- record written; its path is the only stdout line
  1 -- the op refused (reason on stderr), nothing written
  2 -- usage error, or engine import failure
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _ensure_claude_klabauter_on_path() -> str:
    import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
    import cc_invoke

    return cc_invoke.require_engine_on_path(__file__)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="record-superseding-review.py")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--wave-sidecar", action="append", default=[])
    parser.add_argument("--prep-sidecar", default=None)
    parser.add_argument("--stage-returns-json", default=None)
    parser.add_argument("--supersedes", default=None)
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        stage_returns = json.loads(args.stage_returns_json) if args.stage_returns_json else None
    except json.JSONDecodeError as exc:
        print(f"record-superseding-review.py: --stage-returns-json is not JSON: {exc}", file=sys.stderr)
        return 2

    try:
        _ensure_claude_klabauter_on_path()
        from coordinator_core.ops.review_mint.supersede import (
            SupersedeRefused,
            record_superseding_review,
        )
    except (RuntimeError, ImportError) as exc:
        print(f"record-superseding-review.py: engine unreachable: {exc}", file=sys.stderr)
        return 2

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    try:
        result = record_superseding_review(
            repo_root=repo_root,
            plan=args.plan,
            commit_range={"base": args.base, "head": args.head},
            wave_sidecar_paths=[Path(p) for p in args.wave_sidecar],
            prep_sidecar=args.prep_sidecar,
            stage_returns=stage_returns,
            session_id=args.session_id,
            supersedes=args.supersedes,
        )
    except SupersedeRefused as exc:
        print(f"record-superseding-review.py: refused: {exc}", file=sys.stderr)
        return 1
    print(result["record_path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
