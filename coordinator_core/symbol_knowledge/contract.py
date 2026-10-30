"""
coordinator_core.symbol_knowledge.contract -- the interface the symbol-knowledge builders share.

Purpose: pins ``FlaggedSymbol`` and the three entry points the leg composes. Each function delegates
to the module that owns its body (lazy import, so this module stays cheap and cycle-free):
``flag.flag_symbols``, ``agent_batches.launch_batches``, ``traps.record_traps``.

Negative-spec:
  - ``flag_symbols`` spawns no git process and writes nothing.
  - ``record_traps`` writes nothing: it returns trap lines; the detached batch job is the sidecar's
    only writer.
  - ``launch_batches`` launches at most one Haiku batch (``adjust``) and one Sonnet batch
    (``net-new``), detached; it never runs a model call in-process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

KIND_ADJUST = "adjust"
KIND_NET_NEW = "net-new"
KINDS = (KIND_ADJUST, KIND_NET_NEW)


@dataclass(frozen=True)
class FlaggedSymbol:
    """A symbol the terminal commit changed. ``qualified_name`` is rag's qualified-name grammar and
    the sidecar key; ``kind`` is ``adjust`` (a definition may already exist) or ``net-new``."""

    qualified_name: str
    path: str
    kind: str
    public: bool

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown flagged-symbol kind: {self.kind!r}")


PathLike = Union[str, Path]


def flag_symbols(common_dir: PathLike, sha: str, graph_db: PathLike) -> "list[FlaggedSymbol]":
    """Public symbols whose spans the commit *sha* changed, read in-process against the SCIP symtab
    beside *graph_db*; 0 git spawns, <= 50 ms process time."""
    from coordinator_core.symbol_knowledge.flag import flag_symbols as impl

    return impl(common_dir, sha, graph_db)


def launch_batches(
    root: PathLike,
    run_id: str,
    flagged: Sequence[FlaggedSymbol],
    traps: Mapping[str, "list[str]"],
    sha: Optional[str] = None,
) -> str:
    """Detach the batch job for *flagged* with the trap lines *traps*, stamping definitions with the
    flagged commit *sha* (HEAD when None); returns the leg's detail line."""
    from coordinator_core.symbol_knowledge.agent_batches import launch_batches as impl

    return impl(root, run_id, flagged, traps, sha=sha)


def record_traps(record: Mapping[str, Any], flagged: Sequence[FlaggedSymbol]) -> "dict[str, list[str]]":
    """Qualified name -> one-line trap strings for each flagged symbol a revise or halt in the run's
    close-job *record* touched; symbols with no trap are absent."""
    from coordinator_core.symbol_knowledge.traps import record_traps as impl

    return impl(record, flagged)
