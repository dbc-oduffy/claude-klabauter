# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""
verify-ue-overrides.py — CLI trampoline over claude-klabauter
coordinator_core.ops.verify_ue_overrides.

Finish-strangler port (DR-047/DR-059): the bash implementation (walks the
machine-local-registered UE-context directories and asserts each carries the
expected UE plugin override in .claude/settings.json — spec backlink docs/plans/
2026-05-20-coordinator-doctor-wiki.md § Chunk 10) has been fully ported to
coordinator_core/ops/verify_ue_overrides.py (co-located test:
test_verify_ue_overrides.py). This file is now a thin coordinator-claude-side (contract)
trampoline over that claude-klabauter (engine) module, per DR-047 (coordinator-claude owns
contract/generator, claude-klabauter owns engine).

Shebang note: the SHEBANG line above is `#!/usr/bin/env python3`, generator-
owned by `gen-launcher-shim.py --ensure-unix`, and correct for this shape. On
Windows, this file's co-located `.cmd` twin wins via `PATHEXT` when invoked as
a bareword, so the shebang is never read there; on macOS/Linux `python3` is the
right interpreter. Caution: callers must invoke via the extensionless name or a
resolved-interpreter prefix, never a bareword `.py` through git-bash — git-bash
DOES honor the shebang and would exec-127 with no `python3` present. See the
carve-out in coordinator-claude's coordinator/docs/wiki/bash-on-windows-gotchas.md §
Carve-out (cross-repo — this wiki lives in the coordinator-claude repo, not
here).

Manual diagnostic only — per docs/wiki/per-project-plugin-gating.md § verify-ue-
overrides.sh, this is NEVER auto-invoked by any ceremony (its peer UE-context
dirs are specific to the source author's local machine layout). Run manually
when UE override drift is suspected; also referenced as coordinator-doctor P-9.

Exit convention: this is a FAIL-LOUD verification script (not an always-0
advisory like audit-enabled-plugins.py) — a missing/misconfigured registry or
override is a real drift signal the caller needs to see. On CLAUDE_KLABAUTER_ROOT
resolution or import failure this trampoline therefore exits 1, not 0.

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import os
import sys

_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from cc_invoke import _resolve_claude_klabauter_root  # noqa: E402


def _import_main():
    """Resolve CLAUDE_KLABAUTER_ROOT, put it on sys.path, and import the ported entrypoint.

    Reuses cc_invoke's battle-tested CLAUDE_KLABAUTER_ROOT resolution ladder (env var ->
    settings-home pointer file -> coordinator-claude-klabauter-root.sh) rather than
    re-deriving it -- this is a plain in-process import, not an RPC invoke, so
    cc_invoke's subprocess-spawn transport (cc_invoke()/route()) is
    deliberately NOT used here.
    """
    claude_klabauter_root = _resolve_claude_klabauter_root()
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)
    from coordinator_core.ops.verify_ue_overrides import main as _op_main

    return _op_main


def main() -> None:
    try:
        op_main = _import_main()
    except RuntimeError as exc:
        print(f"verify-ue-overrides.py: CLAUDE_KLABAUTER_ROOT resolution failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except ImportError as exc:
        print(
            f"verify-ue-overrides.py: coordinator_core.ops.verify_ue_overrides not "
            f"importable: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # DR-276: op_main takes a positional `script_dir` argument that
    # run_op_main's plain argv-forwarding contract has no room for, so this
    # CLI owns its own main() and wraps the call in recording_declared_writes()
    # directly rather than routing through run_op_main — any paths op_main
    # declares via declare_write() still become a session scope-touch claim
    # instead of landing unclaimed as an orphan at the scoped_git_commit sink.
    from coordinator_core.cli_entry import recording_declared_writes

    with recording_declared_writes():
        code = op_main(sys.argv[1:], script_dir)
    sys.exit(code)


if __name__ == "__main__":
    main()
