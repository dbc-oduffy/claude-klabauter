"""
coordinator_core.ops.learn_lessons_config_update — cwd learn-lessons-root advisory.

Purpose: ensure the cwd repo is discoverable by learn-lessons. learn-lessons
discovery roots are derived PER-MACHINE from the machine-local ``[repos]``
registry (see ``coordinator/bin/learn-lessons-roots.py``: ``$CLAUDE_HOME`` +
``machine-local get repos.*``, skip-absent, minus publish targets). This
module does NOT append absolute paths to any committed config file — that
accretion baked one machine's paths into a git-tracked file and did not
survive a machine change (retired 2026-06-19 on the bash side; the prior
awk-marker append was also silently broken).

Behavior: if the cwd repo is already a learn-lessons root (the meta-repo
``$CLAUDE_HOME`` itself, or a registered ``[repos]`` entry), silent no-op.
Otherwise print a one-line hint to stderr advising registration via
machine-local. NEVER mutates a tracked file. Always returns 0 (idempotent;
safe as a Phase 0 call).

Port of: learn-lessons-config-update.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
    - Does NOT write/mutate any tracked file — advisory stderr hint only.
    - Does NOT require machine-local to be present/executable — if the
      resolver is missing or ``keys``/``get`` fail, treats the repo as
      unregistered and falls through to the advisory hint (fail-open,
      mirroring the bash oracle's `[ -x "$ML" ]` guard + `|| continue` reads).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List, Optional
from coordinator_core.win_portability import is_executable, no_console_creationflags


def _norm(path: str) -> str:
    """Physical-path normalize for comparison (resolves symlinks, mirrors `cd && pwd -P`)."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _resolve_machine_local(claude_home: str) -> Optional[str]:
    """PATH first, then the in-plugin bin as fallback — mirrors the bash oracle."""
    from shutil import which

    ml = which("machine-local")
    if ml:
        return ml
    fallback = os.path.join(
        claude_home, "plugins", "coordinator-claude", "coordinator", "bin", "machine-local"
    )
    if os.path.isfile(fallback) and is_executable(fallback):
        return fallback
    return None


def _machine_local_keys(ml: str) -> List[str]:
    try:
        proc = subprocess.run(
            [ml, "keys"],
            capture_output=True,
            text=True,
            timeout=10,
            # Review: code-reviewer — Windows portability convention applied
            # inconsistently across this wave's siblings; align this call site.
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _machine_local_keys: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("repos.")
    ]


def _machine_local_get(ml: str, key: str) -> str:
    try:
        proc = subprocess.run(
            [ml, "get", key],
            capture_output=True,
            text=True,
            timeout=10,
            # Review: code-reviewer — Windows portability convention applied
            # inconsistently across this wave's siblings; align this call site.
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _machine_local_get: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _slugify(name: str) -> str:
    """Mirrors: tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '_' | sed 's/^_*//; s/_*$//'."""
    lowered = name.lower()
    slug = re.sub(r"[^a-z0-9]", "_", lowered)
    return slug.strip("_")


def main(argv: List[str]) -> int:
    """CLI entry: advisory-only, always returns 0."""
    claude_home = os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~")), ".claude")

    cwd = _norm(os.getcwd())
    home_norm = _norm(claude_home)

    # The meta-repo itself is always a learn-lessons root -- nothing to advise.
    if cwd == home_norm:
        return 0

    ml = _resolve_machine_local(claude_home)
    if ml:
        for key in _machine_local_keys(ml):
            p = _machine_local_get(ml, key)
            if not p:
                continue
            if _norm(p) == cwd:
                return 0

    slug = _slugify(os.path.basename(cwd))
    print(f"learn-lessons: '{cwd}' is not a registered learn-lessons root.", file=sys.stderr)
    print("  To include its lessons in central runs, register it once:", file=sys.stderr)
    print(f'    machine-local set repos.{slug} "{cwd}"', file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
