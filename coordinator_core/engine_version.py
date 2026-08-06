"""
coordinator_core.engine_version — the engine self-version surface.

Purpose: resolve which commit of coordinator_core actually RAN, for
receipt-stamping and drift-probe consumers. This is a self-version surface,
not a repo-version surface.

Negative-spec (verbatim intent — do not "fix" this):
  - Resolves the engine's OWN git dir via `Path(__file__)`, NOT the caller's
    `repo_root`. A receipt `engine_sha` is the SHA of the code that RAN,
    which for a vendored consumer correctly resolves the vendored copy's
    SHA (Decision A) — never the consuming repo's own HEAD.
  - Robust to both live (this repo checked out directly) and vendored
    (coordinator_core copied/symlinked into a consumer repo) topology,
    because `Path(__file__)` always points at the copy that is executing.
    Known limitation (Review: code-reviewer, Finding 2): this holds for
    copy-based vendoring; for symlink-based vendoring, `.resolve()` follows
    the symlink back to the source checkout and reports the SOURCE's current
    HEAD, not the frozen SHA the symlink was created to pin. Treat the
    symlink-robustness claim as unverified until a fleet census confirms no
    consumer vendors via symlink.

Public surface (pinned contract):

    def resolve_engine_sha() -> str | None: ...
    MIN_KNOWN_GOOD_SHA: str

Spec backlink: docs/plans/2026-07-12-claude-klabauter-engine-version-surface-drift-probe.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.git_scope import scoped_git_env

# Committed floor-provenance guardrail. This value is version-controlled and
# reviewable in this file's own git history — it is NEVER runtime-fetched
# from a remote/registry. It is bumped as part of the Definition of Done of
# any fix consumers were demonstrably re-hitting (the wsc_resolve/6fdc7b4
# class), so the floor tracks known-bad lines rather than "latest".
#
# This value is the wsc_resolve phantom-handoff fix that motivated the plan.
MIN_KNOWN_GOOD_SHA: str = "6fdc7b4de770dc1c996b3c2a42bf2c7984dd67c9"


def resolve_engine_sha() -> str | None:
    """Resolve the git HEAD SHA of the running coordinator_core copy.

    Returns the stripped 40-char hex HEAD SHA of the git repo containing
    this file, resolved via `Path(__file__)` (see module negative-spec).

    Returns None (the sentinel for "unresolvable") and NEVER raises when:
      - git is not installed / not on PATH (FileNotFoundError/OSError),
      - the engine's own directory is not inside a git repo,
      - the subprocess exits non-zero for any other reason,
      - the subprocess exceeds the timeout (subprocess.TimeoutExpired),
      - the subprocess output cannot be decoded (UnicodeDecodeError).

    Negative-spec (git scoping): `git -C <engine_dir>` sets only the working
    directory. An inherited `GIT_DIR` — git exports one to every hook it runs,
    commonly as the relative `"."` — still wins over directory-based discovery,
    so an unscoped probe returns the HEAD of whatever repo the hook was running
    in and reports it as the running engine's version; `engine_drift` then
    computes an ancestry verdict from that foreign sha and can call a current
    engine "behind". The repo-scoping environment is therefore stripped via
    `git_scope.scoped_git_env()` so that `-C` genuinely selects the engine's own
    checkout. (The git-dir confinement check `git_scope` also offers is NOT used
    here: it confines to a repo ROOT, and `engine_dir` is deliberately a
    subdirectory of whichever tree the engine ships in.) See
    `coordinator_core/git_scope.py`.
    """
    engine_dir = Path(__file__).resolve().parent
    try:
        # Review: code-reviewer (Finding 1) — bounded timeout so a blocked/locked
        # git process degrades to None instead of hanging the per-op invocation
        # budget indefinitely.
        result = subprocess.run(
            ["git", "-C", str(engine_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            env=scoped_git_env(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip()
