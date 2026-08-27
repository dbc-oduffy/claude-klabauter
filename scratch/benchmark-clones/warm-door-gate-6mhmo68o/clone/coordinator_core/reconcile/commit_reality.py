"""
coordinator_core.reconcile.commit_reality — helper residue for archive_stamp/completion_ops.

**KILLED 2026-08-26 (PM ruling, `state/kill-ledger.md`).** This module formerly held the DEC-1
three-signal commit-reality shipped-ness matcher (`evaluate_commit_reality`), the module's only
public entry point. `handoff.reconcile_open` (C4) was the sole authorized caller of an auto-ship
verdict; it never actually fired one (see the kill-ledger entry for the measured evidence). The
verdict logic, its cross-handoff attribution guard, its explicit-ship-claim path, its plan-
corroboration gate, and every helper reachable only from `evaluate_commit_reality` are deleted.

What survives is helper residue two OTHER modules import directly, independent of the killed
verdict:

  - `archive_stamp.py:175` imports `_DEFAULT_MECHANICAL_DENYLIST` and `_is_mechanical_subject`
    (mechanical-commit-subject filtering for its own ship-SHA walk-back — unrelated to the
    deleted three-signal matcher).
  - `ops/completion_ops.py:77` imports `_git` (as `_reality_git`) — a read-only git subprocess
    choke point, reused rather than re-spawning a second copy.

`_git` is scheduled for rehoming out of this module (its generic-git-wrapper shape no longer fits
a module whose thesis is "this subsystem runs no git" now that the matcher is gone) — filed as a
backlog item by the EM, not authored in this chunk.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2 (DEC-1) — historical; the DEC-1
verdict this module implemented is deleted, not the plan's other chunks.

Negative-spec:
  - Does NOT expose `evaluate_commit_reality` or any commit-reality shipped-ness verdict — that
    surface is deleted (`state/kill-ledger.md`, this chunk's entry).
  - Does NOT write any file, git object, or repo state — pure read-only git subprocess helper.
"""

from __future__ import annotations

import subprocess
from typing import Sequence


#: Fallback mechanical-commit-subject denylist used only when a caller's policy dict omits
#: `mechanical_commit_denylist` (defensive default). Kept in sync with the plan's five prefixes,
#: plus the archival/migration-machinery prefixes `archive_stamp.resolve_source_ship_sha` /
#: `stamp_shipped_in`'s scope-derived walk-back added (2026-08-05): a handoff or plan's most
#: recent toucher is very often the fleet-archive sweep or a corpus-wide vocabulary migration, not
#: the work itself — a false `shipped_in` reads as authoritative to every later reconciler, so
#: this denylist is the single shared exclusion list `archive_stamp.py` consumes (see
#: `archive_stamp._mechanical_commit_denylist`, which imports this tuple directly rather than
#: keeping a second, driftable copy).
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
)

#: Denylist tokens matched as a SUBSTRING (family marker) rather than a prefix — catches a whole
#: commit-subject family wherever the marker sits (e.g. "chore: handoff.transition: ship <id>",
#: "fix(migrate_handoff_vocabulary): ..."). Every other denylist entry is matched as a
#: case-insensitive PREFIX only: a real feature commit whose subject merely CONTAINS a token like
#: "memo:" or "pickup:" mid-subject (not as a prefix) must not be silently denylisted.
_SUBSTRING_FAMILY_TOKENS: frozenset = frozenset({
    "handoff.transition",
    "migrate_handoff_vocabulary",
    "migrate handoff corpus",
})


def _git(worktree_root, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run a read-only git subcommand from worktree_root and return the CompletedProcess.

    Purpose: single choke point for this module's git subprocess invocations. Never passes a
    mutating verb — COMPUTE_ONLY-safe. Reused by `ops/completion_ops.py` (as `_reality_git`)
    rather than re-spawning a second copy of the same pattern.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(worktree_root),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


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
