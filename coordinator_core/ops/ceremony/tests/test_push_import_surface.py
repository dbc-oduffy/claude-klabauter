"""Import-surface pin for C1 (docs/plans/2026-08-29-the-push-subsystem-leaves-
and-then-the-pipeline-can-go.md): the push-with-retry subsystem now lives at
`coordinator_core.ops.ceremony.push`, not `commit_pipeline.py`, and every
push-only importer resolves it from there.

IMPORT-ONLY move, no behaviour change -- this test pins the SURFACE (the
symbol lives at its new home), not push semantics, which the pre-existing
push tests already cover.

`commit_pipeline.py` itself was RETIRED by `12b6a009aa` (2026-08-29), so the
re-export half of this pin went with it: a module that does not exist
re-exports nothing -- see `test_commit_entry_import_cost.py` for the full
incident this retirement surfaced.
"""

from __future__ import annotations

import importlib

PUSH_ONLY_IMPORTERS = [
    "coordinator_core.ops.push_outstanding",
    "coordinator_core.ops.ceremony.post_commit_tail",
    "coordinator_core.ops.ceremony.consumed_handoff_stamp",
    "coordinator_core.execute_plan_assemble.close_out_and_stamp",
    "coordinator_core.workstream_complete.directives_commit_tail",
    "coordinator_core.ops.session.safe_commit_offer",
]


def test_push_subsystem_symbols_live_at_their_new_home():
    push_mod = importlib.import_module("coordinator_core.ops.ceremony.push")
    for name in (
        "push_with_retry",
        "derive_push_status",
        "derive_pushed_tristate",
        "resolve_post_push_sha",
        "PushOutcome",
        "PUSH_MODE_SYNC",
        "PUSH_MODE_DEFERRED",
        "PUSH_MODE_NONE",
        "PUSH_MODE_NEVER",
        "PUSH_STATUS_PUSHED",
        "PUSH_STATUS_FAILED",
        "PUSH_STATUS_DECLINED",
        "PUSH_STATUS_NO_REMOTE",
        "PUSH_STATUS_NOT_ATTEMPTED",
        "PUSH_STATUS_UNCONFIRMED",
        "PUSH_STATUS_CADENCE_PENDING",
        "CEREMONY_PUSH_BUDGET_SECS",
        "PUSH_RETRY_BUDGET_SECS",
        # `_drain_pending_push_after_sync` gravestoned 2026-08-30
        # (docs/plans/2026-08-30-who-pushes-and-when.md C2) -- zero call
        # sites, its delegate (`auto_push.drain_pending_push`) deleted in
        # the same pass. The pin goes with it.
    ):
        assert hasattr(push_mod, name), f"push.py is missing {name!r}"


def test_seven_push_only_importers_still_resolve_after_the_move():
    """Every importer named in C1's `writes` list still imports cleanly.

    Not a behaviour test -- an import-time smoke test that a caller's own
    `from ... import (...)` line resolves, which is exactly what a stale
    `commit_pipeline` re-export would silently defeat.
    """
    for module_name in PUSH_ONLY_IMPORTERS:
        importlib.import_module(module_name)


def test_every_ceremony_push_site_sizes_its_ladder_from_elapsed():
    """No ceremony push site hands `push_with_retry` the FLAT budget.

    `_ceremony_push_budget` was written 2026-08-26 for a reviewer-confirmed P2
    and wired to nothing: it had ZERO callers until 2026-08-31 while all three
    ceremony push sites still passed `CEREMONY_PUSH_BUDGET_SECS` directly. The
    defect it closes was therefore live everywhere it was supposed to be fixed,
    and nothing failed -- the function imported, read correctly, and was simply
    never called.

    That is why this asserts on the CALL SITES and not on the symbol. An
    import-surface check (the test directly above) passes in exactly the broken
    state this pins against, because the flat constant is a legitimate symbol
    that legitimately exists; what was wrong was who used it.
    """
    # Review: coordinator:code-reviewer (a72f5accd9830c935) P2 -- the prior
    # regex (`push_with_retry\((.*?)\)`, DOTALL, non-greedy) is paren-depth-
    # unaware: it stops at the FIRST `)` after the open paren, not the call's
    # true close. A call site wrapping the flat constant in any expression
    # containing an intervening `(...)` (e.g. `budget_secs=(CEREMONY_PUSH_
    # BUDGET_SECS)`, or any helper call before it) would have its match
    # truncated before the flat symbol's text, and the assertion would pass
    # vacuously on exactly the regression this test exists to catch. `ast`
    # walking is depth-aware by construction and closes that gap.
    import ast
    from pathlib import Path

    push_mod = Path(__file__).resolve().parents[1]
    sites = []
    for path in sorted(push_mod.glob("*.py")):
        if path.name == "push.py":
            continue  # defines both; the fallback arm legitimately returns the flat slice
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "push_with_retry":
                continue
            for kw in node.keywords:
                if kw.arg == "budget_secs":
                    segment = ast.get_source_segment(text, kw.value) or ""
                    sites.append((path.name, segment))

    assert sites, "no ceremony push_with_retry call sites found -- test is looking in the wrong place"
    flat = [
        (name, args) for name, args in sites
        if "CEREMONY_PUSH_BUDGET_SECS" in args
    ]
    assert not flat, (
        "ceremony push site(s) still pass the flat budget instead of "
        f"_ceremony_push_budget(elapsed): {[n for n, _ in flat]}"
    )
