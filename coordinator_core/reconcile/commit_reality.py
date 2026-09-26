"""
coordinator_core.reconcile.commit_reality — helper residue for archive_stamp/completion_ops.

**KILLED 2026-08-26 (PM ruling, `state/kill-ledger.md`).** This module formerly held the DEC-1
three-signal commit-reality shipped-ness matcher (`evaluate_commit_reality`), the module's only
public entry point. `handoff.reconcile_open` (C4) was the sole authorized caller of an auto-ship
verdict; it never actually fired one (see the kill-ledger entry for the measured evidence). The
verdict logic, its cross-handoff attribution guard, its explicit-ship-claim path, its plan-
corroboration gate, and every helper reachable only from `evaluate_commit_reality` are deleted.

What survives is helper residue one OTHER module imports directly, independent of the killed
verdict:

  - `archive_stamp.py:175` imports `_DEFAULT_MECHANICAL_DENYLIST` and `_is_mechanical_subject`
    (mechanical-commit-subject filtering for its own ship-SHA walk-back — unrelated to the
    deleted three-signal matcher).

The read-only git subprocess choke point `ops/completion_ops.py` formerly imported from here is
deleted (R3, `docs/plans/2026-09-22-spawn-budget-and-census.md`): `ops/completion_ops.py` now
spawns git through `coordinator_core.git.run.run_git` directly, and this module runs no git at
all, matching its thesis.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2 (DEC-1) — historical; the DEC-1
verdict this module implemented is deleted, not the plan's other chunks.

Negative-spec:
  - Does NOT expose `evaluate_commit_reality` or any commit-reality shipped-ness verdict — that
    surface is deleted (`state/kill-ledger.md`, this chunk's entry).
  - Does NOT write any file, git object, or repo state.
  - Does NOT spawn git or define a git subprocess runner — `_git` is deleted (R3); a caller
    needing git goes through `coordinator_core.git.run.run_git`.
"""

from __future__ import annotations

from typing import Sequence


_DEFAULT_MECHANICAL_DENYLIST: tuple = (
    "pickup:",
    "reclaim(docs)",
    "session-init",
    "memo:",
    "handoff.transition",
    "fleet: archive",
    "archive handoff:",
    "auto-commit:",
    "change_kind:",
    "migrate_handoff_vocabulary",
    "migrate handoff corpus",
    # `_SUBSTRING_FAMILY_TOKENS` member — a real feature subject that merely contains this
    "chore(handoffs):",
)

#: Denylist tokens matched as a SUBSTRING (family marker) rather than a prefix — catches a whole
#: case-insensitive PREFIX only: a real feature commit whose subject merely CONTAINS a token like
_SUBSTRING_FAMILY_TOKENS: frozenset = frozenset({
    "handoff.transition",
    "migrate_handoff_vocabulary",
    "migrate handoff corpus",
})


def _is_mechanical_subject(subject: str, denylist: Sequence[str]) -> bool:
    """Return True when `subject` starts with a denylisted mechanical-commit
    prefix, or contains one of `_SUBSTRING_FAMILY_TOKENS` anywhere.

    Purpose: used by `archive_stamp.py`'s ship-SHA walk-back to skip a pickup:/memo:/
    session-init/handoff.transition-family/frontmatter-mutation/fleet-archive/vocabulary-
    migration commit as `shipped_in` evidence. Case-insensitive PREFIX match for every denylist
    entry, EXCEPT the entries in `_SUBSTRING_FAMILY_TOKENS`, which also match as a substring to
    catch a whole subject family regardless of its conventional-commit type prefix — substring-
    everywhere would silently exclude a legitimate commit whose subject merely contains a token
    like "memo:" mid-subject.
    """
    lowered = subject.strip().lower()
    for token in denylist:
        token_l = token.strip().lower()
        if not token_l:
            continue
        if token_l in _SUBSTRING_FAMILY_TOKENS:
            if token_l in lowered:
                return True
        elif lowered.startswith(token_l):
            return True
    return False
