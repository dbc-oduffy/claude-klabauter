"""coordinator_core.write_guards — PreToolUse Write/Edit guard engines.

Python engine-ification of DoE's per-edit bash guard fan-out
(coordinator/hooks/scripts/block-*.sh, nudge-*.sh on the Write|Edit matcher),
per the DR-047 contract-vs-engine split and the naked-Python hook migration.

DoE owns the thin plumbing (a single python3 PreToolUse dispatcher that imports
this package in-process); claude-klabauter owns these performant engines. One Python
interpreter start collapses the former ~10 bash.exe cold-starts (200-500ms each
on Windows) into in-process logic — zero bash, zero subprocess per edit.

Each guard is a module exposing the interface pinned in INTERFACE.md:
  CLASS   = "hard-deny" | "advisory"
  MATCHERS = list[str]  subset of {Write, Edit, MultiEdit, NotebookEdit}
  PRIORITY = int        (lower runs first within its CLASS)
  def check(payload: dict) -> dict | None
The engine (engine.py) discovers and runs them via `pkgutil.iter_modules` —
adding a new sibling module IS registration; there is no explicit list to
edit here. (E.g. `block_priority_ledger_edit.py` — priority-ledger directory
hard-deny, `docs/plans/2026-07-26-priority-ledger.md` chunk C9a.) The
subagent_sandbox engine is reused directly (not re-ported).
"""
