"""Typed AlternativeRecord: the members a guard message may offer the reader.

Frozen dataclasses with a construction-time invariant: a record whose only
actionable member is an Override is refused (the rung-C shape). Pure data, no
spawn; the capability manifest is read once per process as a data file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple, Union

_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "bash_guards" / "harness_capability_manifest.json"
)


class Audience(Enum):
    EM = "em"
    SUBAGENT = "subagent"
    UNRESOLVED = "unresolved"


@lru_cache(maxsize=1)
def _capability_ids() -> frozenset:
    with open(_MANIFEST_PATH, encoding="utf-8") as fh:
        return frozenset(json.load(fh).get("capabilities", {}))


@dataclass(frozen=True)
class ReadyCommand:
    """Rung A: a command the reader can run as written."""

    text: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("ReadyCommand needs text")

    def rendered(self) -> str:
        return self.text


@dataclass(frozen=True)
class AdaptableOffer:
    """Rung B: a template the reader adapts before running."""

    template: str
    what_to_adapt: str


@dataclass(frozen=True)
class CapabilityRef:
    """A harness capability named by manifest id, never asserted in prose."""

    capability_id: str

    def __post_init__(self) -> None:
        if self.capability_id not in _capability_ids():
            raise ValueError(
                "capability_id %r is not in harness_capability_manifest.json"
                % (self.capability_id,)
            )


@dataclass(frozen=True)
class EscalationRoute:
    """Who to tell and what they must authorize; the subagent's only way out."""

    report_what: str
    authorize_what: str


@dataclass(frozen=True)
class Override:
    """EM-audience escape hatch. Never the sole actionable member."""

    env_var: str
    reason_shaped: bool = False


Member = Union[ReadyCommand, AdaptableOffer, CapabilityRef, EscalationRoute, Override]

VERDICT_KINDS = ("deny", "advisory", "rewrite")


@dataclass(frozen=True)
class AlternativeRecord:
    verdict_kind: str
    summary: str
    members: Tuple[Member, ...]
    guard_name: str
    updated_input: Optional[dict] = field(default=None)
    audience: Audience = Audience.UNRESOLVED
    em_text: Optional[str] = None
    rewrite_allow: bool = True

    def __post_init__(self) -> None:
        if self.em_text == "":
            raise ValueError("em_text must be None or non-empty")
        if self.verdict_kind not in VERDICT_KINDS:
            raise ValueError("verdict_kind must be one of %r" % (VERDICT_KINDS,))
        if not any(not isinstance(m, Override) for m in self.members):
            raise ValueError("an AlternativeRecord needs a non-Override member")
        if self.verdict_kind == "rewrite" and self.updated_input is None:
            raise ValueError("a rewrite record needs updated_input")
