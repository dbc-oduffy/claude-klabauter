"""
coordinator_core.ops.scope_soak_enable — one-shot idempotent soak-clock sentinel writer.

Purpose: starts the warn-only soak clock for scoped-safety-commits by writing
`.git/coordinator-sessions/.warn-mode-enabled-at` with the current UTC ISO-8601
timestamp, if the sentinel does not already exist. Idempotent: a pre-existing
sentinel is never overwritten — its timestamp is echoed back instead.

Run this ONCE after Phase 2's scope-guard commit hook is live. The
flip-readiness evaluator (scope-flip-readiness) reads this sentinel to compute
the "2-week minimum soak" criterion.

Port source: coordinator/bin/scope-soak-enable (DoE-claude)
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292, chunk B3
See also: docs/pretooluse-deny-contract.md, docs/wiki/scoped-safety-commits.md § Phase 5

Negative-spec:
    - Never overwrites an existing sentinel — idempotent no-op on rerun.
    - Always writes UTC (no BSD/GNU `date -u` fallback branch needed in Python —
      datetime.now(timezone.utc) is portable).
    - Output wording is verbatim-preserved even though no caller machine-parses
      it — it is quoted in docs/wiki/scoped-safety-commits.md and
      docs/pretooluse-deny-contract.md as user-facing help text.
"""

from __future__ import annotations

GENERATES = []  # writes only .git/coordinator-sessions/.warn-mode-enabled-at inside the worktree's own .git directory

import os
import sys
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from coordinator_core.ops._git_root_util import git_root as _resolve_git_root

_SESSIONS_DIR_REL = os.path.join(".git", "coordinator-sessions")
_SENTINEL_NAME = ".warn-mode-enabled-at"


def enable(git_root: Optional[str] = None) -> Tuple[str, int]:
    """Write (or report) the soak-clock sentinel.

    git_root: injected repo root for test isolation; resolved via `git
    rev-parse --show-toplevel` when omitted.

    Returns (stdout_text, rc): rc is 0 on both the idempotent-already-started
    path and the freshly-started path; 1 only when not inside a git repo (the
    "not in a git repo" diagnostic is printed to stderr as a side effect,
    mirroring the bash script, and stdout_text is empty in that case).
    """
    if git_root is None:
        git_root = _resolve_git_root()
    if not git_root:
        print(
            "Error: not inside a git repository. Run from within the project repo.",
            file=sys.stderr,
        )
        return ("", 1)

    sessions_dir = os.path.join(git_root, _SESSIONS_DIR_REL)
    sentinel = os.path.join(sessions_dir, _SENTINEL_NAME)

    if os.path.isfile(sentinel):
        with open(sentinel, encoding="utf-8") as fh:
            existing = fh.read().strip()
        text = (
            "Soak already started — sentinel already exists.\n"
            f"  Enabled at: {existing}\n"
            f"  Sentinel:   {sentinel}\n"
            "\n"
            "To check flip readiness, run: scope-flip-readiness\n"
        )
        return (text, 0)

    os.makedirs(sessions_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(sentinel, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ts + "\n")

    text = (
        "Soak clock started.\n"
        f"  Enabled at: {ts}\n"
        f"  Sentinel:   {sentinel}\n"
        "\n"
        "The flip predicate requires:\n"
        "  - >= 10 sessions with scope warnings recorded\n"
        "  - False-positive rate < 10%\n"
        "  - Zero unresolved orphan-class warnings in trailing 7 days\n"
        "  - >= 14 days elapsed since this sentinel was written\n"
        "\n"
        "To check flip readiness, run: scope-flip-readiness\n"
    )
    return (text, 0)


def main(argv: List[str]) -> int:
    """CLI entry — scope-soak-enable takes no arguments."""
    text, rc = enable()
    if text:
        sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
