"""
coordinator_core.session.dispatch_nudge_sentinel — single-source resolver for
the ``.dispatch-nudge-ok`` suppression sentinel, keyed by session id.

This sentinel has exactly ONE home: the platform temp directory, resolved via
``tempfile.gettempdir()``. Nothing in either repo (claude-klabauter or
DoE-claude) writes any other home — verified by census, not assumed. The
reader ``nudge_em_code_dispatch.py`` historically carried a second, git-tree
candidate location (``<repo_root>/coordinator-sessions/<sid>/.dispatch-nudge-ok``)
that nothing ever wrote; C2 deletes it as part of this plan.

That dead second lane is what produced the rehomed W3 review note below —
knowledge worth keeping even though the code it described is going away:

    A W3 change substituted ``ctx.repo_root`` (worktree path) with
    ``repo_root`` (git-common-dir path). Since ``repo_root`` already IS the
    ``.git`` dir, the code's extra ``".git"`` join double-nested to
    ``<repo>/.git/.git/coordinator-sessions/<sid>`` — a path that never
    exists, so both the ``.dispatch-nudge-ok`` and ``.autonomous`` sentinels
    were silently never found.

The same "what exactly does ``git_root`` hold?" ambiguity has now surfaced
three times at this seam. A single-source resolver — one function, one
constant, one call site per writer/reader — is what eliminates a recurring
ambiguity about one variable's meaning; it cannot itself be double-nested,
because there is nothing left to double-nest.

This is not a blanket ruling against dual-home reads.
``docs/decisions/DR-222-health-sentinel-durability-parity-settings-home-dual-read.md``
is the closest prior ruling on when a dual-home read IS legitimate: it
deliberately kept a dual-lane union read because BOTH lanes were genuinely
written (a `CLAUDE_HOME`-sandboxed writer lane and a default-home writer
lane). The distinction is what was written, not how many places are read —
this sentinel has one writer and one home, so it gets one resolver; DR-222's
sentinel had two writers and two homes, so it correctly kept two reads.
Do not read this module as evidence that dual-home reads are always wrong —
they are not; DR-222 is the counterexample.

Spec backlink: pln-dual-home-sentinel-trap-one-re-de4676 § C1.

Negative-spec:
    - Do NOT hardcode ``/tmp`` or reach for ``tempfile.gettempdir()``
      directly at a new call site for this sentinel — import and call
      ``sentinel_path()`` so there is exactly one place this convention can
      drift again.
    - Do NOT add a second candidate location or re-introduce an OR-list of
      candidate homes for this sentinel — it has exactly one home.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

_SENTINEL_PREFIX = "coordinator-dispatch-nudge-ok-"


def sentinel_path(session_id: str) -> Path:
    """Return the dispatch-nudge suppression sentinel path for ``session_id``.

    Resolves the platform temp directory via ``tempfile.gettempdir()``
    (honours TMPDIR/TEMP/TMP per-platform) rather than a hardcoded POSIX
    ``/tmp`` — the single point of truth for both the sentinel writer and
    every reader.
    """
    return Path(tempfile.gettempdir()) / f"{_SENTINEL_PREFIX}{session_id}"
