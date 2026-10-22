"""plan.merge shared contract: refusal codes, report dataclasses and the newline normaliser. Stdlib only.

Every plan.merge module builds against these shapes; a change here moves the op's wire result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, Tuple

REFUSAL_CODES = (
    "ID_COLLISION",
    "REKEY_DANGLING",
    "DELTA_UNMATCHED_ROW",
    "DELTA_UNMATCHED_ANCHOR",
    "SECTION_UNACCOUNTED",
    "SPINE_MALFORMED",
    "SCHEMA_INVALID",
    "DANGLING_EDGE",
    "FIDELITY_BREACH",
    "OUTPUT_IS_SOURCE",
    "OUTPUT_EXISTS",
)

ROW_STATUSES = ("untouched", "amended", "new")
EDIT_CLASSES = ("fields", "note", "drop", "rekey")
CHECK_KEYS = (
    "row_partition",
    "single_fence",
    "unique_ids",
    "schema_valid",
    "sections_accounted",
    "edges_resolve",
)


def normalize_newlines(text: str) -> str:
    """The one definition of "byte-identical after LF normalization": CRLF and lone CR become LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Refusal:
    """A merge declined with nothing written. `code` is one of REFUSAL_CODES."""

    code: str
    message: str
    sources: Tuple[str, ...] = ()
    row_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.code not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code: {self.code!r}")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "sources": list(self.sources),
            "row_ids": list(self.row_ids),
        }


@dataclass(frozen=True)
class Section:
    """A `## ` section of a source plan; text before the first `## ` is heading `(preamble)`."""

    source: str
    heading: str
    text: str


@dataclass(frozen=True)
class SourceRow:
    """A plan-spine row as it stood in `source`: original bytes, LF-normalised."""

    source: str
    row_id: str
    text: str


@dataclass(frozen=True)
class RowAccount:
    """How one output row relates to the sources. `edit_classes` is a subset of EDIT_CLASSES."""

    row_id: str
    source: str
    status: str
    edit_classes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ROW_STATUSES:
            raise ValueError(f"unknown row status: {self.status!r}")
        bad = [c for c in self.edit_classes if c not in EDIT_CLASSES]
        if bad:
            raise ValueError(f"unknown edit classes: {bad!r}")


@dataclass(frozen=True)
class MergeReport:
    """`rows` accounts every output row; `carried` lists every source row id carried in;
    `dropped` lists declared-dropped section headings; `checks` has exactly CHECK_KEYS."""

    rows: Tuple[RowAccount, ...]
    carried: Tuple[str, ...]
    dropped: Tuple[str, ...]
    checks: Dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if tuple(sorted(self.checks)) != tuple(sorted(CHECK_KEYS)):
            raise ValueError(f"checks must have exactly the keys {CHECK_KEYS!r}")

    def counts(self) -> Dict[str, int]:
        by = {s: sum(1 for r in self.rows if r.status == s) for s in ROW_STATUSES}
        source_rows = len(self.carried)
        return {
            "source_rows": source_rows,
            "untouched": by["untouched"],
            "amended": by["amended"],
            "unaccounted": source_rows - by["untouched"] - by["amended"],
            "new": by["new"],
            "out": len(self.rows),
            "dropped_sections": len(self.dropped),
        }

    def to_dict(self) -> dict:
        return {
            "rows": [asdict(r) | {"edit_classes": list(r.edit_classes)} for r in self.rows],
            "carried": list(self.carried),
            "dropped": list(self.dropped),
            "checks": dict(self.checks),
            "counts": self.counts(),
        }
