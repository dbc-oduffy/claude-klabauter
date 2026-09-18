"""The environment-story family: the invariant core, the composed
non-strictest stories, the DoE-side selection seam, and the reader for the
engine's own guard-enforcement join.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W2-C2.
Ported from DoE-claude's `coordinator/hooks/scripts/_environment_story.py`,
`_environment_stories.py`, `_environment_selection.py` and
`_guard_enforcement_join.py` -- one package because the four modules are one
family with one consumer set (see `story.py`'s and `stories.py`'s own module
docstrings for the resolution ladder and the composed cloud story).

`_environment_story_omission_ledger.py` did NOT move with this package
(reviewer finding 9, ACCEPT carve-out): its sole consumer,
`emit-omission-register.py`, resolves a register that exists only in
DoE-claude, so both stay DoE-repo tooling. This package imports nothing
named `omission_ledger` and carries no `__main__` regeneration block --
`stories.py`'s dev-time regeneration path stays DoE-resident; the derived
constant moved here as data only.

No symbols are re-exported at package level -- callers import the specific
module (`story`, `stories`, `selection`, `guard_enforcement_join`) they need,
matching the DoE originals' single-file-module convention.
"""

from __future__ import annotations
