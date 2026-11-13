"""Frozen record shapes of the changelog-first backlog pass, and the token proxy that prices them.

A WeekEntry is one calendar week walked; its FlaggedChanges are the changelog bullets judged
impactful; only the artifacts those cite are opened, each reduced to an ArtifactRecord; records
cluster into TopicDigests, the only text a model reads.

Token proxy is bytes/4, the same axis as `benchmarks/distill_run_cost.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BYTES_PER_TOKEN", "FlaggedChange", "WeekEntry", "ArtifactRecord", "TopicDigest", "token_proxy"]

BYTES_PER_TOKEN = 4


def token_proxy(text: str) -> int:
    """Estimated tokens of `text`: UTF-8 byte length // 4."""
    return len(text.encode("utf-8")) // BYTES_PER_TOKEN


@dataclass(frozen=True)
class FlaggedChange:
    """One impactful changelog bullet: its title, the day it landed, its commit shas, and the
    repo-relative paths it cites (an id that resolved to no path is not listed here)."""

    title: str
    day: str
    commits: tuple[str, ...] = ()
    cited_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeekEntry:
    """One calendar week walked. `summary_path` is None for a week with daily summaries but no
    WEEK-SUMMARY.md; such a week is walked from its dailies and still counts."""

    week: str
    summary_path: str | None = None
    flagged: tuple[FlaggedChange, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ArtifactRecord:
    """A cited artifact mechanically reduced: its headings, decisions, outcomes and Tasks ledger rows."""

    path: str
    headings: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    tasks_ledger: tuple[str, ...] = ()


@dataclass(frozen=True)
class TopicDigest:
    """One topic's compact digest: the records merged under `topic_key`, the rendered `text`, and
    its `tokens` (token_proxy of `text`)."""

    topic_key: str
    records: tuple[ArtifactRecord, ...]
    text: str
    tokens: int
