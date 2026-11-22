"""Record shape and op reply shapes for the accepted-PM-rulings register.

The four `rulings.*` ops bind to the TypedDicts below; DoE consumes the same names.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from typing import Any, NotRequired, Optional, TypedDict

STATUS_ACCEPTED = "accepted"
STATUS_SUPERSEDED = "superseded"
STATUSES = (STATUS_ACCEPTED, STATUS_SUPERSEDED)

REGISTER_DIR = "state/pm-rulings"

GATE_ROUTE = "coordinator:apm"


COMPARATORS = ("==", "<=", "<", ">=", ">")


def scope_token_match(scope: "list[str] | tuple[str, ...]", tokens: "list[str] | tuple[str, ...]") -> bool:
    """True when a ruling scope token equals (case-insensitive) a lowercase query token or one
    glob-matches the other. The single scope matcher for `rulings.search` and the ledger view."""
    for have in (s.lower() for s in scope):
        for want in tokens:
            if have == want or fnmatchcase(want, have) or fnmatchcase(have, want):
                return True
    return False


@dataclass(frozen=True)
class Target:
    """A numeric bound a ruling fixes: `quantity` is the phrase a body uses for it
    ("gate threshold"), `comparator` the relation a stated value must satisfy against `value`
    in `unit` ("" for unitless)."""

    quantity: str
    value: float
    unit: str = ""
    comparator: str = "=="

    @classmethod
    def from_dict(cls, raw: Any) -> "Target":
        """Raises ValueError on a missing or ill-typed field."""
        if not isinstance(raw, dict):
            raise ValueError("target is not a mapping")
        q = raw.get("quantity")
        if not isinstance(q, str) or not q.strip():
            raise ValueError("target 'quantity' missing or not a non-empty string")
        v = raw.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("target 'value' must be a number")
        unit = raw.get("unit", "")
        if unit is None:
            unit = ""
        if not isinstance(unit, str):
            raise ValueError("target 'unit' must be a string")
        cmp_ = raw.get("comparator", "==")
        if cmp_ not in COMPARATORS:
            raise ValueError(f"target comparator {cmp_!r} not in {COMPARATORS}")
        return cls(quantity=q.strip(), value=float(v), unit=unit.strip(), comparator=cmp_)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "value": self.value,
            "unit": self.unit,
            "comparator": self.comparator,
        }


@dataclass(frozen=True)
class Ruling:
    """One accepted PM ruling.

    `pm_turn` cites the PM's words: sizing path + register row id, or run_id + anchor.
    `scope` is a tuple of tokens (repo, subsystem, op, path globs). `text` is the PM
    verbatim. `enforcement` is a repo-relative test or gate path, or None when the
    ruling binds by prose only. `recorded` is an ISO date (YYYY-MM-DD). `targets` are the
    numeric bounds the ruling fixes (empty when it fixes none; records lacking the key load).
    """

    id: str
    pm_turn: str
    scope: tuple[str, ...]
    text: str
    status: str = STATUS_ACCEPTED
    superseded_by: Optional[str] = None
    enforcement: Optional[str] = None
    recorded: str = ""
    targets: tuple[Target, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scope"] = list(self.scope)
        if self.targets:
            d["targets"] = [t.to_dict() for t in self.targets]
        else:
            del d["targets"]
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Ruling":
        """Build from a parsed yaml mapping; raises ValueError on a missing or ill-typed field."""
        if not isinstance(raw, dict):
            raise ValueError("ruling record is not a mapping")
        for key in ("id", "pm_turn", "text"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"field {key!r} missing or not a non-empty string")
        scope = raw.get("scope") or []
        if isinstance(scope, str):
            scope = [scope]
        if not isinstance(scope, (list, tuple)) or not all(isinstance(s, str) for s in scope):
            raise ValueError("field 'scope' must be a list of strings")
        status = raw.get("status", STATUS_ACCEPTED)
        if status not in STATUSES:
            raise ValueError(f"status {status!r} not in {STATUSES}")
        for key in ("superseded_by", "enforcement"):
            v = raw.get(key)
            if v is not None and not isinstance(v, str):
                raise ValueError(f"field {key!r} must be a string or null")
        recorded = raw.get("recorded", "")
        if hasattr(recorded, "isoformat"):
            recorded = recorded.isoformat()
        raw_targets = raw.get("targets") or []
        if not isinstance(raw_targets, (list, tuple)):
            raise ValueError("field 'targets' must be a list of mappings")
        targets = tuple(Target.from_dict(t) for t in raw_targets)
        return cls(
            id=raw["id"].strip(),
            pm_turn=raw["pm_turn"].strip(),
            scope=tuple(s.strip() for s in scope if s.strip()),
            text=raw["text"],
            status=status,
            superseded_by=raw.get("superseded_by"),
            enforcement=raw.get("enforcement"),
            recorded=str(recorded),
            targets=targets,
        )


class TargetDict(TypedDict):
    quantity: str
    value: float
    unit: str
    comparator: str


class RulingDict(TypedDict):
    targets: NotRequired[list[TargetDict]]
    id: str
    pm_turn: str
    scope: list[str]
    text: str
    status: str
    superseded_by: Optional[str]
    enforcement: Optional[str]
    recorded: str


class Refusal(TypedDict):
    id: str
    reason: str


class IndexReply(TypedDict, total=False):
    """rulings.index reply: `rulings` + `unreadable` for verb=list; `applied` + `refused` for verb=append."""

    rulings: list[RulingDict]
    unreadable: list[Refusal]
    applied: list[str]
    refused: list[Refusal]


class SearchMatch(TypedDict):
    id: str
    text: str
    pm_turn: str
    scope: list[str]


class SearchReply(TypedDict):
    matches: list[SearchMatch]


class GateReply(TypedDict):
    """rulings.gate reply. `route` is GATE_ROUTE on refuse, absent on pass."""

    verdict: str
    deferred: list[str]
    contradicted: list[str]
    unnamed_deferrals: list[str]
    reasons: NotRequired[dict[str, list[str]]]
    route: NotRequired[str]
    message: str


class CoverageEntry(TypedDict):
    id: str
    enforcement: str


class CoverageReply(TypedDict):
    covered: list[CoverageEntry]
    prose_only: list[str]
    dangling: list[CoverageEntry]
