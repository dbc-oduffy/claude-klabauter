"""
coordinator_core.ops.parse_resolves_trailer — extract `Resolves:` git-trailer
artifact-ids from a single commit.

Purpose: thin wrapper that extracts `Resolves: <artifact-id>` trailer values
from a commit's message, one artifact-id per stdout line, so callers (e.g.
rollup-derive) can derive resolving-commit sets without hand-parsing commit
bodies.

Extraction strategy — two-rung, primary-then-fallback, mirroring the bash
oracle exactly:
  1. Primary: `git log -1 --format='%(trailers:key=Resolves,valueonly)' <commit>`
     — structured pretty-format trailer extraction (git >= 2.15). Exact-case
     `Resolves:` only.
  2. Fallback (only when primary yields nothing): re-parse the raw commit
     body via `git interpret-trailers --parse`, matching `^Resolves:` with a
     case-INSENSITIVE grep — deliberate typo-forgiveness (`RESOLVES:`,
     `resolves:`) beyond the documented exact-case convention on the primary
     path. This asymmetry is inherited from the bash oracle
     (parse-resolves-trailer.sh, reviewed the Staff Engineer #7) and is NOT a bug to fix
     mid-port.

Exit codes (parity-critical):
  0 — success (including the vacuous zero-trailers case — empty output is a
      valid non-error result, not a failure)
  1 — usage error (missing/unrecognized argument)
  2 — not inside a git repository, OR <commit> does not resolve to a valid
      commit

Port of: parse-resolves-trailer.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-08-lifecycle-vocab-c2-durable-links-rollup.md § C4
Doctrine: coordinator/docs/wiki/resolves-commit-trailer.md (example-doctrine-repo)

Negative-spec (do NOT "fix" mid-port):
    - Does NOT probe git version via `git interpret-trailers --version` (not
      a valid flag for that subcommand; the oracle's own docstring notes this
      as an explicit prior mistake to avoid).
    - Fallback grep IS case-insensitive on purpose (see above) even though
      the primary path and the usage text advertise exact-case `Resolves:`
      only — this inconsistency is inherited, not introduced.
    - Read-only. Never mutates the repo.
"""

from __future__ import annotations

import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from typing import List

_PROG = "parse-resolves-trailer.sh"  # literal program-name prefix — matches oracle stderr


def _run_git(args: List[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _is_inside_git_repo() -> bool:
    r = _run_git(["rev-parse", "--git-dir"])
    return r.returncode == 0


def _is_valid_commit(commit: str) -> bool:
    r = _run_git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"])
    return r.returncode == 0


def _primary_trailers(commit: str) -> List[str]:
    r = _run_git(["log", "-1", "--format=%(trailers:key=Resolves,valueonly)", commit])
    if r.returncode != 0:
        return []
    lines = [line for line in r.stdout.split("\n") if line.strip() != ""]
    return lines


def _fallback_trailers(commit: str) -> List[str]:
    body_r = _run_git(["log", "-1", "--format=%B", commit])
    body = body_r.stdout if body_r.returncode == 0 else ""

    interp_r = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=body,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    interpreted = interp_r.stdout

    results: List[str] = []
    for line in interpreted.split("\n"):
        if not line:
            continue
        stripped = line.lstrip()
        prefix = stripped[:9].lower()
        if not prefix.startswith("resolves:"):
            continue
        _, _, rest = stripped.partition(":")
        value = rest.strip()
        if value:
            results.append(value)
    return results


def run(commit: str) -> tuple[List[str], int]:
    """Core extraction: returns (artifact_id_lines, exit_code)."""
    if not _is_inside_git_repo():
        print(f"{_PROG}: not inside a git repository", file=sys.stderr)
        return [], 2

    if not _is_valid_commit(commit):
        print(f"{_PROG}: not a valid commit: {commit}", file=sys.stderr)
        return [], 2

    primary = _primary_trailers(commit)
    if primary:
        return primary, 0

    return _fallback_trailers(commit), 0


_USAGE = """Usage: parse-resolves-trailer.sh <commit>

Emits one artifact-id per line, extracted from the `Resolves: <artifact-id>`
git trailers of <commit>. Zero output lines is a valid, non-error result
(the commit carries no Resolves: trailer) -- callers should treat empty
output as a vacuous pass, not a failure. See:
  coordinator/docs/wiki/resolves-commit-trailer.md

Requires git >= 2.15 (for the %(trailers:...) pretty-format token).

Options:
  --help, -h   Show this help"""


def main(argv: List[str]) -> int:
    """CLI entry: arg validation, extraction, print, return rc."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 1

    if argv[0] in ("--help", "-h"):
        print(_USAGE)
        return 0

    commit = argv[0]
    lines, rc = run(commit)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
