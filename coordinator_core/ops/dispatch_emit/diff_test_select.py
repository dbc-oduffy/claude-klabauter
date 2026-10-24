"""Select the tests a diff needs: edited paths inside a surface, mapped to test files.

`select` is pure; `main` is the I/O shell. It spawns two git processes (`git diff --name-only
BASE..HEAD -- <surface>` and one fixed-string `git grep --cached` on module stems limited to TEST_PATHSPECS for the module imports),
plus one `git diff` per distinct `--passed-at` sha when that option is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional, Sequence

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit import test_caller_widen as tcw



class Selection(NamedTuple):
    tests: tuple
    skipped: tuple


def _in_surface(path: str, surface: Sequence[str]) -> bool:
    p = path.replace("\\", "/")
    for s in surface:
        s = s.replace("\\", "/").rstrip("/")
        if p == s or p.startswith(s + "/"):
            return True
    return False


def grep_modules(edited: Sequence[str], surface: Sequence[str]) -> list:
    """The non-test source modules `select` will look up referencers for."""
    return sorted(
        p
        for p in edited
        if _in_surface(p, surface)
        and p.endswith(".py")
        and not tcw.is_test_path(p)
        and not p.endswith("__init__.py")
    )


def select(
    edited: Sequence[str],
    surface: Sequence[str],
    referencers_text: str,
    passed_at: Mapping[str, str],
    unchanged_since: Callable[[str], bool],
) -> Selection:
    """Tests to run for `edited`; a test whose `passed_at` sha has no surface edits since
    lands in `skipped` instead."""
    kept = [p for p in edited if _in_surface(p, surface)]
    modules = grep_modules(kept, surface)
    by_module = tcw.referencers(modules, tcw.parse_grep(referencers_text)) if modules else {}
    wanted = set()
    for p in kept:
        if tcw.is_test_path(p) and p.endswith(".py"):
            wanted.add(p)
        else:
            wanted.update(by_module.get(p, ()))
    tests, skipped = [], []
    for t in sorted(wanted):
        sha = passed_at.get(t)
        (skipped if sha and unchanged_since(sha) else tests).append(t)
    return Selection(tuple(tests), tuple(skipped))


def _git(root: Path, args: list) -> Optional[str]:
    """stdout, or None when git failed (rc other than 0/1); callers must not read None as "no output"."""
    r = run_git(args, cwd=str(root))
    return r.stdout if r.returncode in (0, 1) else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="--select-tests")
    ap.add_argument("--base", required=True)
    ap.add_argument("--surface", nargs="+", required=True)
    ap.add_argument("--passed-at", default="{}", help="JSON {test_path: sha}")
    ap.add_argument("--repo-root")
    args = ap.parse_args(list(argv) if argv is not None else sys.argv[1:])
    top = args.repo_root or show_toplevel()
    if not top:
        print("not in a git repo; pass --repo-root", file=sys.stderr)
        return 2
    root = Path(top)
    try:
        passed_at = json.loads(args.passed_at)
    except ValueError:
        print("--passed-at is not valid JSON; selection refused", file=sys.stderr)
        return 2
    if not isinstance(passed_at, dict):
        print("--passed-at must be a JSON object; selection refused", file=sys.stderr)
        return 2
    diff_out = _git(root, ["diff", "--name-only", f"{args.base}..HEAD", "--", *args.surface])
    if diff_out is None:
        print("git diff failed; selection refused", file=sys.stderr)
        return 2
    edited = [ln.strip() for ln in diff_out.splitlines() if ln.strip()]
    modules = grep_modules(edited, args.surface)
    stems = sorted({m.rsplit("/", 1)[-1][: -len(".py")] for m in modules})
    needles = [a for s in stems for a in ("-e", s)]
    text = ""
    if stems:
        grep_out = _git(root, ["grep", "--cached", "-F", *needles, "--", *tcw.TEST_PATHSPECS])
        if grep_out is None:
            print("git grep failed; selection refused", file=sys.stderr)
            return 2
        text = grep_out
    memo: dict = {}

    def unchanged_since(sha: str) -> bool:
        if sha not in memo:
            out = _git(root, ["diff", "--name-only", f"{sha}..HEAD", "--", *args.surface])
            memo[sha] = out is not None and not out.strip()
        return memo[sha]

    sel = select(edited, args.surface, text, passed_at, unchanged_since)
    print(json.dumps({"tests": list(sel.tests), "skipped": list(sel.skipped), "edited": edited}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
