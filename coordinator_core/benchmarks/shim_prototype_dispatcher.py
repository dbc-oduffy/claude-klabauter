"""coordinator_core.benchmarks.shim_prototype_dispatcher -- THROWAWAY
measurement-only "dispatcher" half of the C7 shim prototype. NOT the C8
production dispatcher; discard when C8 lands its own.

Purpose: `shim_decision_rule`'s shim arm ("forwarder -> dispatcher ->
target work") does not exist yet -- C8 has not landed `coordinator/bin/
coordinator-assemble.py` or `coordinator/bin/lib/entry_point_shim.py`. To
measure ANYTHING for C7's decision record, stage 2 must build a minimal
prototype that reproduces the SHAPE of a forwarder-plus-dispatcher round
trip (two process starts chained, the second doing the real work) without
attempting the production shim's routing/name-table logic. This script is
that second hop: run standalone, it performs EXACTLY the same target work
`coordinator/bin/plan-assemble.py` performs when invoked bare (resolve
CLAUDE_KLABAUTER_ROOT, import `coordinator_core.plan_assemble`, call `main([])`),
so the shim arm and the baseline arm do identical real work and only
differ in "how many processes were spawned to reach it" -- see
`shim_decision_rule.py` module docstring 'Shim arm' for why that is the
only permitted axis of difference.

What this shares with a real C8 dispatcher: it is a second process,
spawned by a forwarder, that resolves CLAUDE_KLABAUTER_ROOT and performs the same
in-process call `plan-assemble.py` performs today.

What this does NOT share with a real C8 dispatcher: no subcommand
routing table, no shim-name resolution, no support for any entry point
other than the one path this prototype hardcodes (`plan_assemble.main`
with no args). It exists to be timed, not maintained.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# coordinator_core/benchmarks/ -> coordinator_core/ -> repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_LIB_DIR = os.path.join(_REPO_ROOT, "coordinator", "bin", "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

_TRANSPORT_FAIL = 3


def main(argv: list) -> int:
    from cc_invoke import _resolve_claude_klabauter_root  # noqa: E402

    try:
        claude_klabauter_root = _resolve_claude_klabauter_root()
    except RuntimeError as exc:
        print(f"shim_prototype_dispatcher: CLAUDE_KLABAUTER_ROOT resolution failed: {exc}", file=sys.stderr)
        return _TRANSPORT_FAIL
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)
    try:
        import coordinator_core.plan_assemble as _mod
    except ImportError as exc:
        print(f"shim_prototype_dispatcher: coordinator_core.plan_assemble not importable: {exc}", file=sys.stderr)
        return _TRANSPORT_FAIL

    return _mod.main(argv)


if __name__ == "__main__":
    sys.exit(main([]))
