"""The ``work:<row-id>`` workflow label, as a grammar rather than an f-string.

An emitted workflow tags each executor call with ``label: 'work:<row-id>'``.
That token is the only on-disk record of how a plan's task-spine expanded into
chunks: dispatch sidecars cover a fraction of the corpus, so a reader asking
"which chunks did this plan actually produce" has the committed workflow
scripts and nothing else. DoE-claude's plan-completeness ledger is that reader.

Before this module the token existed twice as an f-string inside
``emit.py``'s wave composer and once more as a string literal in a DoE test,
with the module docstring documenting a third spelling that no call site used.
A consumer had no declared grammar to build against, only a literal to copy.

``build`` and ``parse`` are inverses over every id the emitter accepts, and
``emit.py`` calls ``build`` at both of its call sites -- the emitter and the
grammar cannot drift apart while that holds, which is what
``tests/test_work_label.py`` pins.

Negative spec -- what this module does NOT do:
  - It does NOT validate that a row id names a real row. ``parse`` answers
    what the label says, not whether it is true; resolving an id against a
    spine is the caller's job and needs the spine.
  - It does NOT JS-escape. The label reaches a script through
    ``_js_string_literal`` at the call site, and escaping here would
    double-escape there.
  - It does NOT own the label vocabulary as a whole. Commit, preflight and
    test phases carry their own labels, composed at their own call sites;
    this grammar is the per-chunk executor label only.
"""

from __future__ import annotations

#: The one prefix. A label not starting with it is not a chunk label.
WORK_LABEL_PREFIX = "work:"


def build_work_label(row_id: str) -> str:
    """Return the workflow label carrying ``row_id``."""
    return f"{WORK_LABEL_PREFIX}{row_id}"


def parse_work_label(label: str) -> str | None:
    """Return the row id in ``label``, or None when it is not a chunk label.

    An empty id (a bare ``work:``) returns None: the emitter never produces
    one, so a consumer meeting it has found drift, not a chunk.
    """
    if not label.startswith(WORK_LABEL_PREFIX):
        return None
    row_id = label[len(WORK_LABEL_PREFIX) :]
    return row_id or None
