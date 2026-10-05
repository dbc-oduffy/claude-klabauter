# plan-status-transition — CLI trampoline over claude-klabauter
# coordinator_core.ops.plan_status_transition (the plan lifecycle recorder:
# stamp-implemented with --falsifier-verdict/--falsifier-output/--prose is the
# only writer of exit_criterion_met). Direct-import variant (mirrors
# close-out-and-stamp, pickup-assemble): in-process call after resolving the
# engine root, no cc_invoke/IPC hop.
#
# Spec backlink: coordinator-content-repo coordinator/skills/execute-plan/SKILL.md § Phase 4
#
# Usage:
#   plan-status-transition stamp-implemented --plan <p> --falsifier-verdict pass \
#       --falsifier-output <raw> --prose <line>
#
# Exit codes: the op's own main() code, or 3 on transport failure (engine root
# unresolvable or coordinator_core import failure).
from __future__ import annotations
"""plan-status-transition — see the # comment block above for the RAG-bait
purpose text (the polyglot shebang convention makes this string a discarded
expression statement, not the module __doc__)."""

import sys

_TRANSPORT_FAIL = 3


def _import_module():
    import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import require_dispatch_engine_on_path

    require_dispatch_engine_on_path()
    import coordinator_core.ops.plan_status_transition as _mod

    return _mod


def main(argv: list[str]) -> int:
    try:
        mod = _import_module()
    except RuntimeError as exc:
        print(f"plan-status-transition: CLAUDE_KLABAUTER_ROOT resolution failed: {exc}", file=sys.stderr)
        return _TRANSPORT_FAIL
    except ImportError as exc:
        print(
            f"plan-status-transition: coordinator_core.ops.plan_status_transition "
            f"not importable: {exc}",
            file=sys.stderr,
        )
        return _TRANSPORT_FAIL

    return mod.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
