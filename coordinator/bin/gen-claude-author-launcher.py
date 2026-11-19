#!/usr/bin/env python3
"""
gen-claude-author-launcher.py — CLI trampoline over claude-klabauter
coordinator_core.ops.gen_claude_author_launcher.

Renders the Windows-native claude-author launcher(s) (claude-author.cmd, claude-author.ps1)
from the coordinator templates into ${CLAUDE_HOME:-$HOME}/.local/bin/. Windows-only:
a clean no-op (exit 0) on macOS/Linux. See the claude-klabauter module's own docstring for the
full design rationale (Git-for-Windows PATH gap, dynamic-root-discovery templates,
idempotent atomic writes, --check-only dry-run safety).

Shebang note: the SHEBANG line above is `#!/usr/bin/env python3`, generator-
owned by `gen-launcher-shim.py --ensure-unix`, and correct for this shape. On
Windows, this file's co-located `.cmd` twin wins via `PATHEXT` when invoked as
a bareword, so the shebang is never read there; on macOS/Linux `python3` is the
right interpreter. Caution: callers must invoke via the extensionless name or a
resolved-interpreter prefix, never a bareword `.py` through git-bash — git-bash
DOES honor the shebang and would exec-127 with no `python3` present. See the
carve-out in coordinator-content-repo's coordinator/docs/wiki/bash-on-windows-gotchas.md §
Carve-out (cross-repo — this wiki lives in the coordinator-content-repo repo, not
here).

Usage:
  gen-claude-author-launcher.py              -- install/refresh the launcher(s)
  gen-claude-author-launcher.py --check-only -- validate without mutating live files
  gen-claude-author-launcher.py --template-dir <path>  -- override template source dir

Exit codes: 0 on success (including the non-Windows no-op skip); 1 on any failure
(unknown argument, missing/invalid --template-dir, template not found, or a
engine-root/import resolution failure) -- this is an install-step gate
(install-maximalist.py wraps it in `run_required`), so failures must block the
install rather than being swallowed, unlike the never-block auto-push shape.

Port of: coordinator/bin/gen-claude-author-launcher.py (bash body retired on cutover;
see git log)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import os
import sys

def _default_template_dir() -> str:
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from coordinator_data_root import data_file

    # Per-file resolution: the co-located templates/bin exists in the engine
    # repo but does not carry these two DoE-owned templates.
    return os.path.dirname(str(data_file("templates", "bin", "claude-author-launcher.cmd.tmpl")))


def _import_runner():
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

    claude_klabauter_root = require_dispatch_engine_on_path()
    from coordinator_core.cli_entry import run_op_main

    return run_op_main


_UTF8_BOM = b"\xef\xbb\xbf"


def _stage_bom_templates(src_dir: str, dst_dir: str) -> None:
    """Copy the launcher templates into `dst_dir`, the `.ps1` one prefixed with a
    UTF-8 BOM (Windows PowerShell 5.1 reads a BOM-less script as cp1252)."""
    import shutil

    for name in ("claude-author-launcher.cmd.tmpl", "claude-author-launcher.ps1.tmpl"):
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dst_dir, name)
        if not name.startswith("claude-author-launcher.ps1"):
            shutil.copyfile(src, dst)
            continue
        with open(src, "rb") as fh:
            data = fh.read()
        if not data.startswith(_UTF8_BOM):
            data = _UTF8_BOM + data
        with open(dst, "wb") as fh:
            fh.write(data)


def main(argv: "list[str] | None" = None) -> int:
    import tempfile

    argv = (sys.argv[1:] if argv is None else list(argv))
    if os.name != "nt" or "-h" in argv or "--help" in argv:
        return _main(argv)
    with tempfile.TemporaryDirectory(prefix="claude-author-launcher-") as staged:
        staged_argv = argv
        try:
            if "--template-dir" in argv:
                i = argv.index("--template-dir")
                src = argv[i + 1] if i + 1 < len(argv) else ""
                if src and os.path.isdir(src):
                    _stage_bom_templates(src, staged)
                    staged_argv = argv[: i + 1] + [staged] + argv[i + 2 :]
            else:
                _stage_bom_templates(_default_template_dir(), staged)
                staged_argv = argv + ["--template-dir", staged]
        except (RuntimeError, ImportError, OSError):
            staged_argv = argv
        if staged_argv is not argv and len(os.listdir(staged)) < 2:
            staged_argv = argv
        return _main(staged_argv)


def _main(argv: "list[str]") -> int:
    try:
        run_op_main = _import_runner()
    except RuntimeError as exc:
        print(
            f"gen-claude-author-launcher.py: engine-root resolution failed: {exc}",
            file=sys.stderr,
        )
        return 1
    except ImportError as exc:
        print(
            "gen-claude-author-launcher.py: coordinator_core.cli_entry not importable: "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    argv = (sys.argv[1:] if argv is None else argv)
    if "--template-dir" not in argv and "-h" not in argv and "--help" not in argv:
        try:
            argv = argv + ["--template-dir", _default_template_dir()]
        except RuntimeError as exc:
            print(
                f"gen-claude-author-launcher.py: could not resolve a default "
                f"--template-dir: {exc}",
                file=sys.stderr,
            )
            return 1

    try:
        code = run_op_main("coordinator_core.ops.gen_claude_author_launcher", argv)
    except ImportError as exc:
        print(
            "gen-claude-author-launcher.py: "
            f"coordinator_core.ops.gen_claude_author_launcher not importable: {exc}",
            file=sys.stderr,
        )
        return 1

    return code


if __name__ == "__main__":
    sys.exit(main())
