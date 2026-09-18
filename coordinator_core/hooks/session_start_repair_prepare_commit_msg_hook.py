"""coordinator_core.hooks.session_start_repair_prepare_commit_msg_hook —
SessionStart(startup-only) op: self-heals a stale `SCRIPT=` path in the native
git `prepare-commit-msg` hook this session's repo already has installed.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/session-start-repair-prepare-commit-msg-hook.py`.
The installed `.git/hooks/prepare-commit-msg` shim hardcodes an absolute
`SCRIPT=` path to `coordinator/bin/coordinator-prepare-commit-msg`, resolved
at install time; a shim installed before the 2026-07-22 executable-surface
migration points at a doctrine-repo location the file no longer occupies, and
silently never stamps `Session-Id:`/`Deliverable-Id:` trailers thereafter.
This op repairs the SCRIPT path in place when it can find a candidate that
does resolve.

ADAPTATION (class 1): DoE's `_git_hooks_dir`/repo-root resolution shelled out
to `git rev-parse --git-path hooks` / `git rev-parse --show-toplevel`. This
port uses the engine's own zero-spawn primitives instead —
`coordinator_core.git.repo_root.git_dir`/`show_toplevel` — falling through to
the identical `git rev-parse` subprocess only when the zero-spawn walk cannot
resolve (mirrors those primitives' own documented fallback contract). The
`resolve_claude_klabauter_root()` rung DoE's `_engine_root` provided is replaced with
`coordinator_core.engine_root.coordinator_engine_root()` — the resolver this
op is already running inside of; both name the same tree this process's own
`coordinator_core` package was imported from.

Op contract: `params` is unused — every path is resolved from `os.getcwd()`,
matching the source script's own cwd-based (never `__file__`-based)
resolution, since the repo being repaired is whichever repo the invoking
session's process is running in, not this engine's own tree. Fail-open at
every step: never touches a hook file it does not recognize as its own shim
(`_SHIM_MARKER` gate), and only rewrites when none of the shim's current
candidates resolve AND a replacement candidate does.

Negative-spec:
    Does NOT touch a `.git/hooks/prepare-commit-msg` file lacking the
    `coordinator coordinator-prepare-commit-msg hook` marker line.
    Does NOT rewrite a shim whose existing SCRIPT already resolves to a file
    on disk — read-only touched (one stat per candidate) on a healthy shim.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PureWindowsPath

from coordinator_core.git.repo_root import git_dir as _git_dir, show_toplevel
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.win_portability import no_console_creationflags

_SHIM_MARKER = "coordinator coordinator-prepare-commit-msg hook"
_SCRIPT_RELATIVE = "coordinator/bin/coordinator-prepare-commit-msg"


def _git_hooks_dir(cwd: str) -> str:
    """`<git-dir>/hooks`, zero-spawn first via `git_dir`, falling through to a
    `git rev-parse --git-path hooks` subprocess only if that resolves
    nothing. Returns "" on any failure."""
    try:
        gd = _git_dir(cwd)
    except Exception:
        gd = None
    if gd:
        return str(Path(gd) / "hooks")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            **no_console_creationflags(),
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    out = result.stdout.strip()
    if not out:
        return ""
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = Path(cwd) / out_path
    return str(out_path)


def _candidate_script_paths(repo_root: str) -> "list[str]":
    candidates = [str(Path(repo_root) / _SCRIPT_RELATIVE)]

    doe_root_pointer = Path.home() / ".claude" / ".doe-root"
    try:
        doe_root = doe_root_pointer.read_text(encoding="utf-8").strip()
    except Exception:
        doe_root = ""
    if doe_root:
        candidates.append(str(Path(doe_root) / _SCRIPT_RELATIVE))

    try:
        from coordinator_core.engine_root import coordinator_engine_root

        claude_klabauter_root = coordinator_engine_root()
    except Exception:
        claude_klabauter_root = None
    if claude_klabauter_root:
        candidates.append(str(Path(claude_klabauter_root) / _SCRIPT_RELATIVE))

    candidates.append(
        str(Path.home() / ".claude" / "plugins" / "coordinator-claude" / _SCRIPT_RELATIVE)
    )
    return candidates


def _first_existing(paths: "list[str]") -> str:
    for p in paths:
        try:
            if Path(p).is_file():
                return p
        except Exception:
            continue
    return ""


@register_op("hooks.session_start_repair_prepare_commit_msg_hook")
async def _handler(params: dict, repo_root=None) -> dict:
    cwd = os.getcwd()

    hooks_dir = _git_hooks_dir(cwd)
    if not hooks_dir:
        return no_advisory()
    hook_path = Path(hooks_dir) / "prepare-commit-msg"

    try:
        if not hook_path.is_file():
            return no_advisory()
        text = hook_path.read_text(encoding="utf-8")
    except Exception:
        return no_advisory()

    if _SHIM_MARKER not in text:
        return no_advisory()  # not our shim -- never touch a hook we don't recognize

    try:
        this_repo_root = show_toplevel(cwd)
    except Exception:
        this_repo_root = None
    if not this_repo_root:
        return no_advisory()

    candidates = _candidate_script_paths(this_repo_root)

    existing_scripts = re.findall(r'SCRIPT="([^"]+)"', text)
    if any(_first_existing([p]) for p in existing_scripts):
        return no_advisory()

    replacement = _first_existing(candidates)
    if not replacement:
        return no_advisory()  # nothing resolves anywhere -- fail open

    replacement = PureWindowsPath(replacement).as_posix()
    new_first_line_pattern = re.compile(r'SCRIPT="[^"]+"\n')
    new_text, count = new_first_line_pattern.subn(f'SCRIPT="{replacement}"\n', text, count=1)
    if count != 1:
        return no_advisory()

    try:
        hook_path.write_text(new_text, encoding="utf-8")
        current_mode = hook_path.stat().st_mode
        hook_path.chmod(current_mode | 0o111)
    except Exception:
        return no_advisory()

    return no_advisory()
