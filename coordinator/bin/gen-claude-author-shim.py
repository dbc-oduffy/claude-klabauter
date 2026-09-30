# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
# gen-claude-author-shim.py — CLI trampoline over claude-klabauter
# coordinator_core.ops.gen_claude_author_shim.
#
# Finish-strangler port (BIG_PORT): the bash implementation (renders the claude()
# shim from the coordinator template, wires exactly one sentinel-guarded source
# block into the operator's interactive rc, legacy-stopgap detection) has been
# fully ported to coordinator_core/ops/gen_claude_author_shim.py per DR-047 (DoE owns
# contract/generator, claude-klabauter owns engine). This file is now a thin trampoline
# over that claude-klabauter (engine) module — it lives in claude-klabauter post the
# 2026-07-22 executable-surface migration, resolving its DoE-owned template
# via coordinator_data_root.data_file(), not a co-located script path. See the
# claude-klabauter module's own docstring for the full design rationale (idempotency,
# dry-run safety, Windows temp-file portability, faithful-oracle negative-spec).
#
# Shebang note: the SHEBANG line above is `#!/usr/bin/env python3`, generator-
# owned by `gen-launcher-shim.py --ensure-unix`, and correct for this shape. On
# Windows, this file's co-located `.cmd` twin wins via `PATHEXT` when invoked
# as a bareword, so the shebang is never read there; on macOS/Linux `python3`
# is the right interpreter. Caution: callers must invoke via the extensionless
# name or a resolved-interpreter prefix, never a bareword `.py` through git-
# bash — git-bash DOES honor the shebang and would exec-127 with no `python3`
# present. See the carve-out in coordinator-content-repo's coordinator/docs/wiki/bash-on-
# windows-gotchas.md § Carve-out (cross-repo — this wiki lives in the
# coordinator-content-repo repo, not here).
#
# Usage:
#   gen-claude-author-shim.py                    -- render shim + wire rc source line
#   gen-claude-author-shim.py --check-only       -- validate without mutating live files
#   gen-claude-author-shim.py --rc <path>        -- override target rc file
#   gen-claude-author-shim.py --template <path>  -- override template source path
#   gen-claude-author-shim.py --shell powershell -- target a PowerShell profile
#                                                (default template follows the family)
#
# Exit codes: 0 on success (including an idempotent no-op re-run, or a clean
# --check-only pass); 1 on a business failure (unknown argument, missing flag
# value, template not found, rc sentinel block hand-modified, rc file
# uncreatable); 2 on a engine-root-resolution or import (transport) failure --
# a dedicated code, never a reused business rc, so install-maximalist.py's
# `run_required` wrapper (and any other caller) can distinguish "the install
# step itself failed" from "the claude-klabauter engine link is broken" -- this is an
# install-step gate, so failures must block the install rather than being
# swallowed, unlike the never-block auto-push shape. See
# coordinator_core.ops.gen_claude_author_shim's own docstring § Transport-failure
# exit code note for the module-side half of this contract.
#
# Spec backlink: coordinator-content-repo:pln-coordinator-maximalist-install-e73afa § C2
# Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
# Prior bash implementation: see git log (gen-claude-author-shim.py, 231 lines,
# retired on this cutover).

from __future__ import annotations

import sys

def _default_template_path(shell_family: str = "bash") -> str:
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from coordinator_data_root import data_file

    stem = "claude-author-shim.ps1.tmpl" if shell_family == "powershell" else "claude-author-shim.sh.tmpl"
    return str(data_file("templates", "shell", stem))


def _shell_family_from_argv(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "--shell" and i + 1 < len(argv):
            return argv[i + 1]
    from coordinator_core.ops.gen_claude_author_shim import _default_shell_family

    return _default_shell_family()


def _import_runner():
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

    claude_klabauter_root = require_dispatch_engine_on_path()
    from coordinator_core.cli_entry import run_op_main

    return run_op_main


def main(argv: "list[str] | None" = None) -> int:
    try:
        run_op_main = _import_runner()
    except RuntimeError as exc:
        print(
            f"gen-claude-author-shim.py: engine-root resolution failed: {exc}",
            file=sys.stderr,
        )
        return 2
    except ImportError as exc:
        print(
            "gen-claude-author-shim.py: "
            f"coordinator_core.cli_entry not importable: {exc}",
            file=sys.stderr,
        )
        return 2

    argv = (sys.argv[1:] if argv is None else argv)
    if "--template" not in argv and "-h" not in argv and "--help" not in argv:
        try:
            argv = argv + ["--template", _default_template_path(_shell_family_from_argv(argv))]
        except RuntimeError as exc:
            print(
                f"gen-claude-author-shim.py: could not resolve a default "
                f"--template: {exc}",
                file=sys.stderr,
            )
            return 1

    try:
        code = run_op_main("coordinator_core.ops.gen_claude_author_shim", argv)
    except ImportError as exc:
        print(
            "gen-claude-author-shim.py: "
            f"coordinator_core.ops.gen_claude_author_shim not importable: {exc}",
            file=sys.stderr,
        )
        return 2

    return code


if __name__ == "__main__":
    sys.exit(main())
