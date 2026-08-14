"""
coordinator_core.ops.install_shell_init_guard_seam — DoE-owned rc-eval seam
for claude-klabauter's stdout-emitter shell-init resource-cap guard (DR-047 split).

Port source: coordinator/commands/install.md (DoE-claude repo) Step 3.5b.1,
the two literal bash fences at lines 932 and 950 of the source doc.

Purpose (unchanged from the doc): resolve `claude-klabauter`'s root
(`REPO_CLAUDE_KLABAUTER` env override, then `machine-local get
repos.claude_klabauter`), and — only if `<claude_klabauter_root>/bin/shell-init-guard.py`
exists and is executable — write an idempotent, sentinel-guarded block into
the operator's interactive rc (selected from the SHELL env var: zsh picks
.zshrc, bash picks .bashrc, same selection idiom as install.md's other
rc-writing steps) that
`eval`s the guard's stdout at shell start. A machine without claude-klabauter
checked out is a graceful no-op, not a failure — there is simply no guard to
source.

The resolved claude-klabauter path is BAKED into the written block at install time,
not re-resolved via `machine-local` at eval time (a cold terminal lacks it
on PATH) — same principle as the `claude-doe`/`claude()` shim blocks.

Contract: emits the exact `shell_init_guard: <status>` stdout row the DoE
Phase 7 status table expects on every exit path, folding install.md's own
if/echo wrapper into this module (M3/D9 pattern,
docs/plans/2026-07-23-skills-carry-no-code-extirpation.md).

Negative-spec:
    - Does NOT strip or update a previously-written block — append-only,
      sentinel-guarded idempotency (a second run with the sentinel already
      present is a silent no-op), matching the doc block's own contract.
    - Does NOT itself invoke/import `shell-init-guard.py` — only checks its
      presence + exec bit and bakes its path into the written rc snippet;
      the guard's own stdout-emitter logic is claude-klabauter-resident and entirely
      out of scope for this seam (DR-047: claude-klabauter owns the engine, DoE owns
      the rc-eval seam that sources it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional
from coordinator_core.install.write_surface import (
    ABSENT_ON_LEGACY_INSTALLS,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import is_executable

GENERATES = []  # appends only to the operator's ~/.zshrc or ~/.bashrc, outside any git repo

_PROG = "install-shell-init-guard-seam"

SENTINEL = "# coordinator-install: interactive-shell resource-cap guard (runaway-file backstop, DR-047 split)"
SENTINEL_END = "# end coordinator-install: interactive-shell resource-cap guard"
"""Closes the `SENTINEL`-opened block, added fresh installs only (chunk C6,
docs/plans/2026-08-06-writer-declared-write-surface-manifest.md). Never
retrofitted onto an already-installed rc file's BEGIN-only block -- a
machine with the legacy form keeps it permanently; `end_marker` on that
declaration reads `write_surface.ABSENT_ON_LEGACY_INSTALLS`, not this
literal. `_rc_has_sentinel` still detects by `SENTINEL` line-membership
alone, so this addition changes nothing about install-time idempotency
detection."""

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="install-shell-init-guard-seam",
    source_module="coordinator_core.ops.install_shell_init_guard_seam",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="rc-block",
                    path="<rc file resolved by _resolve_rc_path — ~/.zshrc or "
                    "~/.bashrc, selected by SHELL, or the --rc/COORDINATOR_SHIM_RC "
                    "override>",
                    begin_marker=SENTINEL,
                    end_marker=SENTINEL_END,
                    reason=(
                        "the fresh-install form of the block written by main() "
                        "when _rc_has_sentinel() is False -- both markers present, "
                        "SENTINEL_END added by chunk C6 of this same plan."
                    ),
                ),
                WriteSurfaceEntry(
                    kind="rc-block",
                    path="<rc file resolved by _resolve_rc_path — ~/.zshrc or "
                    "~/.bashrc, selected by SHELL, or the --rc/COORDINATOR_SHIM_RC "
                    "override>",
                    begin_marker=SENTINEL,
                    end_marker=ABSENT_ON_LEGACY_INSTALLS,
                    reason=(
                        "the pre-C6 legacy form of the same block: BEGIN marker "
                        "only, no END marker. Never written by this writer's "
                        "current code (_rc_has_sentinel() detects it via SENTINEL "
                        "line-membership alone and main() short-circuits to the "
                        "no-op path) -- declared so an uninstall/audit consumer "
                        "knows this on-disk shape is possible and legitimate, not "
                        "a defect to retrofit."
                    ),
                ),
            ),
        ),
    ),
)
"""This writer's declared write surface — the two markers are read FROM
`SENTINEL`/`SENTINEL_END` (never restated), so a future edit to either
constant alone cannot make this declaration drift silently out of sync.
See spec backlink:
docs/plans/2026-08-06-writer-declared-write-surface-manifest.md, chunk C3f."""


def _resolve_machine_local() -> Optional[str]:
    return shutil.which("machine-local")


def resolve_claude_klabauter_clone() -> str:
    """Tier 1: REPO_CLAUDE_KLABAUTER env. Tier 2: machine-local get repos.claude_klabauter."""
    env_override = os.environ.get("REPO_CLAUDE_KLABAUTER", "")
    if env_override:
        return env_override
    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        return ""
    try:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            [ml_bin, "get", "repos.claude_klabauter"],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_rc_path(override: Optional[str]) -> str:
    if override:
        return override
    env_override = os.environ.get("COORDINATOR_SHIM_RC")
    if env_override:
        return env_override
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")
    shell = os.path.basename(os.environ.get("SHELL", "/bin/bash"))
    if shell == "zsh":
        return os.path.join(home, ".zshrc")
    return os.path.join(home, ".bashrc")


def _rc_has_sentinel(rc_path: str) -> bool:
    if not os.path.isfile(rc_path):
        return False
    try:
        with open(rc_path, "r", encoding="utf-8", errors="replace") as fh:
            return SENTINEL in fh.read().split("\n")
    except OSError:
        return False


def main(argv: List[str]) -> int:
    check_only = "--check-only" in argv

    rc_override: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--rc":
            if i + 1 >= len(argv):
                print(f"{_PROG}: --rc requires a value", file=sys.stderr)
                return 1
            rc_override = argv[i + 1]
            i += 2
        else:
            i += 1

    claude_klabauter_clone = resolve_claude_klabauter_clone()
    guard_src = os.path.join(claude_klabauter_clone, "bin", "shell-init-guard.py") if claude_klabauter_clone else ""

    if not claude_klabauter_clone or not os.path.isfile(guard_src) or not is_executable(guard_src):
        print("shell_init_guard: skipped (claude-klabauter not found — no guard to source)")
        return 0

    rc_path = _resolve_rc_path(rc_override)

    if _rc_has_sentinel(rc_path):
        print(f"shell_init_guard: ready (no-op) ({rc_path})")
        return 0

    if check_only:
        print(f"shell_init_guard: check failed: sentinel absent in {rc_path} (would install)")
        return 1

    block = (
        f"\n{SENTINEL}\n"
        "# Graceful no-op if claude-klabauter absent or python3 missing: the -x check + eval's 2>/dev/null +\n"
        "# the emitter's own fail-open behavior combine to make this safe to source unconditionally.\n"
        f'_cc_fsize_guard="{guard_src}"\n'
        'if [ -x "$_cc_fsize_guard" ]; then eval "$(python3 "$_cc_fsize_guard" 2>/dev/null)"; fi\n'
        "unset _cc_fsize_guard\n"
        f"{SENTINEL_END}\n"
    )
    try:
        with open(rc_path, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as exc:
        print(f"{_PROG}: failed to write rc block: {exc}", file=sys.stderr)
        print(f"shell_init_guard: failed ({exc})")
        return 1

    # DR-276: declared AFTER the write lands, matching the append-integrator-
    # dispositions reference — the contract is a report of what was ACTUALLY
    # written, not of an intended surface.
    declare_write(rc_path)

    print(f"shell_init_guard: installed ({rc_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
