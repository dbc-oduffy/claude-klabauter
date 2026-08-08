"""
coordinator_core.ops.install_claude_doe_wrapper — install the `claude-doe`
persistent launch wrapper onto `${CLAUDE_HOME:-$HOME}/.local/bin/claude-doe`.

Port source: coordinator/commands/install.md (example-doctrine-repo repo) Step 3.5b, the
literal bash fence at line 892 of the source doc. Ported verbatim (not
re-derived from the more generic `coordinator_core.install.wrapper_onto_path`
op): that op's target-dir resolver (`_default_wrapper_bin_dir`) does NOT
honor `CLAUDE_HOME` — it always resolves against the REAL `Path.home()` on
POSIX, which would silently break the install-sandbox-check.py harness's
`CLAUDE_HOME`-isolated dry runs (a live-home write from a sandboxed test is
exactly the failure class docs/wiki/machine-local-registry.md's dry-run-
safety lessons exist to prevent). This module is a small, deliberately
separate op that preserves the doc block's own `${CLAUDE_HOME:-$HOME}`
precedence exactly, rather than repointing the shared generic op (unknown
blast radius on its other callers) to gain that behavior.

Contract: emits the exact `claude_doe_wrapper: <status>` stdout row the example-doctrine-repo
Phase 7 status table expects on every exit path, folding install.md's former
if/echo wrapper into this module (M3/D9 pattern,
docs/plans/2026-07-23-skills-carry-no-code-extirpation.md).

Negative-spec:
    - Does NOT mutate PATH itself — the PATH-membership check is advisory
      only (a printed remediation note), exactly like the doc block's own
      `printf ':%s:' "$PATH" | grep -qF ...` check.
    - Does NOT hash/diff file content before copying — the doc oracle's own
      `cp -p` recopies unconditionally on every live (non-check-only) run;
      this is preserved (not "fixed" to add a content-diff short-circuit),
      matching the doc's own idempotency framing ("re-installs are safe,
      not necessarily no-op-silent").
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
from typing import List, Optional

from coordinator_core.session.declared_writes import declare_write

_PROG = "install-claude-doe-wrapper"


def _claude_home_base() -> str:
    return (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )


def _wrapper_dst() -> str:
    return os.path.join(_claude_home_base(), ".local", "bin", "claude-doe")


def _on_path(local_bin: str) -> bool:
    path_val = os.environ.get("PATH", "")
    members = path_val.split(os.pathsep)
    return any(os.path.normpath(m) == os.path.normpath(local_bin) for m in members if m)


def main(argv: List[str]) -> int:
    check_only = "--check-only" in argv

    wrapper_src: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--wrapper-src":
            if i + 1 >= len(argv):
                print(f"{_PROG}: --wrapper-src requires a value", file=sys.stderr)
                return 1
            wrapper_src = argv[i + 1]
            i += 2
        else:
            i += 1

    if not wrapper_src:
        print(f"{_PROG}: --wrapper-src is required (path to coordinator/bin/claude-doe)", file=sys.stderr)
        print("claude_doe_wrapper: failed (--wrapper-src not supplied)")
        return 1

    if not os.path.isfile(wrapper_src):
        print(f"{_PROG}: wrapper source not found: {wrapper_src}", file=sys.stderr)
        print(f"claude_doe_wrapper: failed (wrapper source not found: {wrapper_src})")
        return 1

    wrapper_dst = _wrapper_dst()
    local_bin = os.path.dirname(wrapper_dst)

    if check_only:
        if os.path.isfile(wrapper_dst) and filecmp.cmp(wrapper_src, wrapper_dst, shallow=False):
            print(f"claude_doe_wrapper: ready ({wrapper_dst})")
            return 0
        status = "stale" if os.path.isfile(wrapper_dst) else "absent"
        print(f"{_PROG}: check failed: {wrapper_dst} is {status} (source: {wrapper_src})", file=sys.stderr)
        print(f"claude_doe_wrapper: failed ({status}: {wrapper_dst})")
        return 1

    try:
        os.makedirs(local_bin, exist_ok=True)
        shutil.copyfile(wrapper_src, wrapper_dst)
        st = os.stat(wrapper_dst)
        os.chmod(wrapper_dst, st.st_mode | 0o111)
    except OSError as exc:
        print(f"{_PROG}: install failed: {exc}", file=sys.stderr)
        print(f"claude_doe_wrapper: failed ({exc})")
        return 1

    # DR-276: declared AFTER the copy+chmod lands, never before — the contract
    # is a report of what was ACTUALLY written, not of an intended surface.
    declare_write(wrapper_dst)

    print(f"claude_doe_wrapper: installed ({wrapper_dst})")
    if not _on_path(local_bin):
        print(f"  NOTE: {local_bin} is not yet on PATH — add to login rc for new terminals:")
        print(f'    export PATH="{local_bin}:$PATH"  # e.g. append to ~/.zprofile')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
