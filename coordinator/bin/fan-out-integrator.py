# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
# Reviewer sidecar path (column 2): the path each reviewer RETURNED after self-persisting its
#       dependency) — a DEDICATED code, distinct from BOTH business codes above, so a caller
# exists. CLAUDE_PLUGIN_ROOT is set from that resolved root before the claude-klabauter op is imported
# (unless the caller already set CLAUDE_PLUGIN_ROOT, which wins unconditionally), so its own
# PLUGIN_ROOT resolution rung 1 always wins here.

from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _import_run_op_main():
    """Resolve the engine root, put it on sys.path, and import `run_op_main`.

    Also sets CLAUDE_PLUGIN_ROOT to this trampoline's own resolved plugin root —
    `data_root("snippets").parent` (co-located or DoE-resident per the split-repo
    layout; see coordinator_data_root.py), or the caller's pre-existing
    CLAUDE_PLUGIN_ROOT env value if one was already set (never clobbered) — so
    the claude-klabauter op's own PLUGIN_ROOT resolver
    (coordinator_core.ops.fan_out_integrator._resolve_plugin_root) hits its
    rung-1 short-circuit and never needs its own fallback ladder in the normal
    call shape.
    """
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path
    from coordinator_data_root import data_root

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(data_root("snippets").parent)
    os.environ.setdefault("CLAUDE_PLUGIN_ROOT", plugin_root)
    claude_klabauter_root = require_dispatch_engine_on_path()
    from coordinator_core.cli_entry import run_op_main
    return run_op_main


def main(argv: "list[str] | None" = None) -> int:
    try:
        run_op_main = _import_run_op_main()
    except RuntimeError as exc:
        print(f"fan-out-integrator.py: engine-root resolution failed: {exc}", file=sys.stderr)
        return 3
    except ImportError as exc:
        print(
            f"fan-out-integrator.py: coordinator_core.cli_entry not importable: {exc}",
            file=sys.stderr,
        )
        return 3

    try:
        code = run_op_main("coordinator_core.ops.fan_out_integrator", (sys.argv[1:] if argv is None else argv))
    except ImportError as exc:
        print(
            f"fan-out-integrator.py: coordinator_core.ops.fan_out_integrator not importable: {exc}",
            file=sys.stderr,
        )
        return 3
    return code


if __name__ == "__main__":
    sys.exit(main())
