"""The canonical memo-kind enum, in a module with no imports of its own.

Purpose: be the one place `kind:` values are written down, at an import cost of
zero. Six sites need this tuple and only this tuple; before this module existed
they reached it through `_memo_compose`, which drags `_common` ->
`ops.ceremony.git_native`, `session.core`, `git.commit_trailers`,
`coordinator_core.dag`, and `lifecycle` behind it -- measured at 36.8ms of the
248ms it costs to import `frontmatter/schema_validate.py`, a module a SIBLING
REPO loads by file path (CLAUDE.md, Architecture). Paying a 36.8ms transitive
chain to read a five-element tuple is the shape this repo's brightline calls
break-class, so the tuple moved here and the chain went away.

Negative-spec:
  - This module imports NOTHING, and must not start. Its entire value is that
    reaching it costs nothing; one `from coordinator_core.x import y` here
    silently re-creates the cost it was split out to remove.
  - It holds the enum only. Validation, message text, and schema emission stay
    with their own callers -- each phrases its own refusal, and this module has
    no opinion about which words those are.
  - `ack` is NOT a kind and never becomes one. Acknowledgement is receipt-state
    on a delivered memo, not something a sender declares at authoring time.

Mirrors: the two `coordinator/bin` CLI scripts (`coordinator-doc-new.py`,
`cross-repo-memo.py`) still hold literal tuples rather than importing this,
because they resolve `coordinator_core` lazily and a module-level import would
put package resolution on their interpreter start. They are pinned to this
tuple by `coordinator/bin/tests/test_memo_kind_enum_mirrors.py`, which is what
makes hand-mirroring safe there rather than merely traditional.
"""

from __future__ import annotations

#: Legal `kind:` values for a cross-repo memo, in the order every refusal
#: message renders them.
VALID_KINDS = ("ask", "consult", "fyi", "proposal", "bug")
