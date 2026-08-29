"""Regression cover: `check_destructive_git_orphan`'s per-segment gate must
survive a quote-split verb.

The 2026-08-29 quote-split sweep closed this class across the chain but left
one member: the `for seg in ...` loop inside `check_destructive_git_orphan`
skipped every segment whose raw text carried no `\bgit\b`, so
`'g''it' reset --hard origin/main` never reached CHECK 1 at all. The sweep
(`test_quote_split_verb_bypass_sweep.py`) is calibrated against the plain
spelling in whatever tree it runs in, and in the claude-klabauter worktree the plain
spelling is also refused by `_check_destructive_git_revert_full` -- whose gate
was already fixed -- so the sweep agreed on `deny`/`deny` and the surviving
leg stayed invisible. It was measured on the published mirror, where the
revert-full leg only advises: `git reset --hard origin/main` denied,
`'g''it' reset --hard origin/main` was ALLOWED, for a host session, a
dispatched executor and a reviewer alike.

This file is deliberately tree-independent where the sweep is not. It drives
CHECK 2 (force-push), which is textual and needs no repository state, so the
verdict does not move with anyone's worktree.
"""

from __future__ import annotations

from coordinator_core.bash_guards.dispatch_checks import check_destructive_git_orphan

#: Built at runtime rather than written as a literal so this file does not
#: itself carry a spelled-out force-push token for source scanners.
_Q = chr(39)
_PLAIN = "git push --force origin main"
_SPLIT = _Q + "g" + _Q + _Q + "it" + _Q + " push --force origin main"


def test_plain_spelling_denies():
    assert check_destructive_git_orphan(_PLAIN) is not None


def test_quote_split_spelling_denies_too():
    assert check_destructive_git_orphan(_SPLIT) is not None, (
        "quote-splitting the verb walked past the orphan guard's per-segment "
        "gate -- the gate is scanning raw text again"
    )
