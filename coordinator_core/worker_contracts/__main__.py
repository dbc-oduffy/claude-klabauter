"""Coordinator-owned dispatch CLI; stock vendor CLIs receive no --worker flag."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from . import persona_prompt, resolve


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier-map", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--tier", default="opus")
    parser.add_argument("--persona", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--assignment", type=Path)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.worker:
            if args.persona or args.tier_map or args.provider or args.describe:
                raise ValueError("--worker uses installed Codex defaults; persona/map/describe do not apply")
            if not args.assignment:
                raise ValueError("--worker requires --assignment")
            from .worker import launch
            return launch(args.repo, args.task_file.read_text(encoding="utf-8"),
                          json.loads(args.assignment.read_text(encoding="utf-8")))
        if not args.persona or not args.tier_map or not args.provider:
            raise ValueError("persona dispatch requires --persona, --tier-map, and --provider")
        resolved = resolve(args.tier_map, args.provider, args.tier)
        prompt = persona_prompt(args.persona, args.task_file.read_text(encoding="utf-8"), args.tier)
        if not args.repo.is_dir():
            raise ValueError("repo must be an existing directory")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    # The provider-owned command must explicitly opt into stdin prompting.
    # Preserve argument boundaries and never invoke a shell.
    if args.describe:
        print(json.dumps({"provider": resolved.provider, "tier": resolved.tier,
                          "model": resolved.model, "effort": resolved.effort,
                          "argv": resolved.argv, "persona": str(args.persona)}))
        return 0
    if not args.assignment:
        parser.error("persona dispatch requires --assignment with live Group EM slot grant")
    from .worker import supervise
    try:
        return supervise(args.repo, prompt, json.loads(args.assignment.read_text(encoding="utf-8")),
                         list(resolved.argv), provider=resolved.provider, mode="persona")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
