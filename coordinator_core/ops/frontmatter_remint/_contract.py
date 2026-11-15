"""Resolver contract for the `frontmatter.remint` op.

A family resolver module (one per engine-owned field family) exposes:

    resolve(path, fm, repo_root) -> Resolved | Refused
    write(path, repo_root) -> Resolved | Refused      # optional

`resolve` answers one bounded question from recorded provenance and never
fabricates: no provenance means `Refused`. `Resolved.value` maps frontmatter
key -> value; the handler writes every key of every `Resolved` through one
`locked_rmw` per file. A family that owns its writer (review_stamp) exposes
`write`; the handler calls it after a `Resolved` from `resolve` (whose value may
be empty), and `write` returns the final `Resolved` or `Refused`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Union

REASONS = frozenset(
    {
        "resolver-unbuilt",
        "handoff-id-underivable",
        "handoff-id-referenced",
        "handoff-id-collision",
        "no-governing-plan",
        "plan-unauthorized",
        "review-run-absent",
        "register-no-stamp",
        "register-absent",
        "register-unsettled",
    }
)


@dataclass(frozen=True)
class Resolved:
    """Provenance found: `value` maps frontmatter key -> value to write."""

    value: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Refused:
    """Provenance absent or unsafe to apply; `reason` is a `REASONS` member."""

    reason: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason not in REASONS:
            raise ValueError(f"unknown refusal reason {self.reason!r}")


Result = Union[Resolved, Refused]
ResolveFn = Callable[[Path, Mapping[str, Any], Path], Result]
WriteFn = Callable[[Path, Path], Result]
