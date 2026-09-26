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

Port of: learn-lessons-config-update.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292

Negative-spec:
    - Does NOT write/mutate any tracked file — advisory stderr hint only.
    - Does NOT require machine-local to be present/executable — if the
      resolver is missing or ``dump`` fails, treats the repo as
      unregistered and falls through to the advisory hint (fail-open,
      mirroring the bash oracle's `[ -x "$ML" ]` guard + `|| continue` reads).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional
from coordinator_core.win_portability import is_executable, no_console_creationflags


def _norm(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _resolve_machine_local(claude_home: str) -> Optional[str]:
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


def _repos_snapshot(ml: str) -> Dict[str, str]:
    try:
        proc = subprocess.run(
            [ml, "dump", "--prefix", "repos", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _repos_snapshot: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _slugify(name: str) -> str:
    lowered = name.lower()
    slug = re.sub(r"[^a-z0-9]", "_", lowered)
    return slug.strip("_")


def main(argv: List[str]) -> int:
    claude_home = os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~")), ".claude")

    cwd = _norm(os.getcwd())
    home_norm = _norm(claude_home)

    if cwd == home_norm:
        return 0

    ml = _resolve_machine_local(claude_home)
    if ml:
        for p in _repos_snapshot(ml).values():
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
