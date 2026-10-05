"""Record types of the declared-pair census.

Field names, order, and types are a contract read by both callers and the
oracle serialiser; `Verdict` is owned by `staleness_git` and never redefined.
"""

from __future__ import annotations

from dataclasses import dataclass

from coordinator_core.ops.staleness_git import Verdict


@dataclass(frozen=True)
class Pair:
    generator: str
    artifact: str
    stamp_key: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class GeneratorRecord:
    generator: str
    pairs: tuple[Pair, ...]
    verdict: Verdict | None
    detail: str
    mutates: tuple[str, ...] = ()
