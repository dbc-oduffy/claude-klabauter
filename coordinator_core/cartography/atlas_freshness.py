"""Commit-time atlas drift repair: compute which system pages a commit's own path list touches,
repair what is mechanical, and record the rest as an already-acted fact.

Interface only; bodies are filled by the implementing chunk. Contract: no git spawn, reads bounded
by the commit's path list plus the atlas pages (never the staged index).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

ACTED_LEDGER_RELPATH = ".coordinator-local/freshness/atlas-acted.jsonl"
"""Repo-root-relative ledger of acted facts, one JSON object per line."""


@dataclass(frozen=True)
class RepairPlan:
    """A mechanical repair of one system page: `files_delta` is the net change to its `files:` count."""

    page_relpath: str
    files_delta: int


@dataclass(frozen=True)
class ActedFact:
    """What the engine already did about drift on `page` at commit `sha`.

    `kind` is "repaired", "content_drift" or "uncovered". `at` is an ISO-8601 UTC date so a reader
    can order it against a page's `last_attested` date (a sha is not orderable against a date).
    """

    page: str
    sha: str
    kind: str
    detail: str
    at: str = ""


def plan_repair(
    root: str | Path,
    paths: Sequence[str],
    deleted_paths: Collection[str],
    added_paths: Collection[str] = frozenset(),
) -> tuple[list[RepairPlan], list[ActedFact]]:
    """Plan the repair a commit of `paths` implies.

    `deleted_paths`: members of `paths` absent from the working tree (a stat, no spawn).
    `added_paths`: members of `paths` absent from HEAD -- the value `commit_scoped` already holds
    (`_v2_state_records_chunked` record `m_head == "000000"`, equivalently `pre_head_blobs`
    missing the path). A path in neither set is a modification.
    """
    raise NotImplementedError


def apply_repair(root: str | Path, plans: Sequence[RepairPlan]) -> list[str]:
    """Write each plan's page edit under `root`; return the page relpaths actually written."""
    raise NotImplementedError


def record_acted(root: str | Path, facts: Sequence[ActedFact], sha: str) -> None:
    """Append `facts` to the ledger at `ACTED_LEDGER_RELPATH`, stamped with `sha`."""
    raise NotImplementedError


def read_acted(root: str | Path) -> dict[str, ActedFact]:
    """Return the newest ledger fact per page; an absent ledger is an empty dict."""
    raise NotImplementedError
