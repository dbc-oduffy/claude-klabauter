"""
coordinator_core.ops.verify_no_powershell_flash — thin-shim delegator.

Purpose: mechanizes `verify-no-powershell-flash.sh`, a preserved backward-compat
alias for callers still using the old script name. The canonical guard logic
lives in `verify-no-console-flash.py` (bin/) — this module does
NOT reimplement any guard logic; it locates and re-invokes the canonical
guard script alongside the caller, forwarding argv and exit code verbatim.

Port of: verify-no-powershell-flash.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-29-windows-console-flash-elimination.md § Chunk 3
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
    - Does NOT parse or reimplement the console-flash detection rules — the
      original `.sh` was itself a thin `exec "$SCRIPT_DIR/verify-no-console-flash.sh"
      "$@"` shim ("Do NOT add logic here"), and this port preserves that
      division of labor exactly; the canonical guard is a separate port item.
    - The sibling script path is resolved by the CALLER (the example-doctrine-repo-side
      polyglot trampoline), not derived here — this module has no
      example-doctrine-repo-repo-topology knowledge of its own. `bin_dir` is a required,
      caller-supplied directory (the directory the trampoline itself lives
      in), mirroring the original's own `$SCRIPT_DIR` self-relative lookup.
    - On POSIX, invokes the sibling by literal path (not `bash <path>`) so the
      sibling's own shebang decides the interpreter — matches the bash
      original's `exec "$SCRIPT_DIR/verify-no-console-flash.sh" "$@"`
      exactly (an `exec`, not a `bash`-forced re-interpretation). On Windows
      that parity is unattainable: CreateProcess reads no shebang and rejects
      the `.sh` outright (WinError 193), so the launch goes through
      `coordinator_core.launchable.resolve_launchable`, which prefixes an
      interpreter ONLY on nt and leaves the POSIX vector bare-path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List

from coordinator_core.launchable import resolve_launchable

_PROG = "verify-no-powershell-flash.sh"
_CANONICAL_GUARD_NAME = "verify-no-console-flash.sh"


def main(argv: List[str]) -> int:
    """CLI entry: locate the canonical guard next to bin_dir, exec it, forward argv+rc.

    argv[0] is REQUIRED: the absolute path to the directory the trampoline
    itself lives in (its own `$SCRIPT_DIR`-equivalent). argv[1:] is forwarded
    verbatim to the canonical guard (e.g. an optional ROOT override).

    Exit codes: passes through the canonical guard's own exit code (0 clean,
    1 violations found) unchanged. Returns 2 only for an internal error this
    shim itself hits (missing bin_dir arg, canonical guard not found/not
    invocable) — the canonical guard itself never exits 2, so 2 is
    unambiguous evidence of a shim-level failure, not a guard verdict.

    Deliberate isolation boundary — do not convert to an in-process import.
    Mechanism: measurement semantics — the guard asserts the CHILD's
    console-window behaviour, a property that only exists at the process
    level and cannot be observed by importing the guard in-process. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    if not argv:
        print(f"{_PROG}: missing argument: <bin-dir>", file=sys.stderr)
        return 2

    bin_dir = argv[0]
    passthrough = argv[1:]

    sibling = os.path.join(bin_dir, _CANONICAL_GUARD_NAME)
    if not os.path.isfile(sibling):
        print(f"{_PROG}: canonical guard not found at {sibling}", file=sys.stderr)
        return 2

    try:
        # resolve_launchable, not a bare path: the canonical guard is a .sh with a
        # shebang, which Windows CreateProcess cannot exec (WinError 193).
        result = subprocess.run([*resolve_launchable(sibling), *passthrough])
    except OSError as exc:
        print(f"{_PROG}: failed to invoke {sibling}: {exc}", file=sys.stderr)
        return 2

    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
