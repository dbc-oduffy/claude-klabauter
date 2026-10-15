"""Leg register: the declared class and reason of every LLM leg an emitted workflow composes.

`emitted_legs` reads the `agent(` call sites of a composed script; `REGISTER` holds one `LegDecl`
per (workflow, label). Imports nothing from an emitter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from coordinator_core.ops._workflow_contract import _AGENT_CALL_SITE, _find_matching_paren, scrub


class LegClass(str, Enum):
    OP_WRAPPER = "op-wrapper"
    VERDICT_READER = "verdict-reader"
    DUPLICATE = "duplicate"
    OVER_MODELLED = "over-modelled"
    EARNS_ITS_COST = "earns-its-cost"


REFUSED_CLASSES = frozenset({LegClass.OP_WRAPPER, LegClass.VERDICT_READER})


@dataclass(frozen=True)
class LegDecl:
    """`runtime_floor` (an APM-ruling path) is admitted only on an op-wrapper."""

    workflow: str
    label: str
    cls: LegClass
    reason: str
    delivered_elsewhere: Optional[str] = None
    runtime_floor: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(f"leg {self.workflow}/{self.label}: reason is required")
        if self.runtime_floor is not None and self.cls is not LegClass.OP_WRAPPER:
            raise ValueError(f"leg {self.workflow}/{self.label}: runtime_floor is admitted only on an op-wrapper")


@dataclass(frozen=True)
class EmittedLeg:
    label: str
    phase: Optional[str]
    agent_type: Optional[str]
    model: Optional[str]


REGISTER: tuple[LegDecl, ...] = ()

_STRING_START = re.compile(r"""\s*(['"`])""")
_KEY_VALUE = r"\b{key}\s*:\s*"


def _option_value(scrubbed_args: str, raw_args: str, key: str) -> Optional[str]:
    """Raw text of the first `key:` option that sits in code (never inside a string), or None."""
    m = re.search(_KEY_VALUE.format(key=key), scrubbed_args)
    if not m:
        return None
    return raw_args[m.end():]


def _literal_prefix(value: str) -> Optional[str]:
    """Literal text a string expression starts with, cut at `${` or the first `+`; None when it opens with code."""
    m = _STRING_START.match(value)
    if not m:
        return None
    quote = m.group(1)
    out: list[str] = []
    i = m.end()
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
            continue
        if ch == quote or (quote == "`" and value.startswith("${", i)):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _string_option(scrubbed_args: str, raw_args: str, key: str) -> Optional[str]:
    value = _option_value(scrubbed_args, raw_args, key)
    if value is None:
        return None
    return _literal_prefix(value)


def _label(scrubbed_args: str, raw_args: str) -> str:
    value = _option_value(scrubbed_args, raw_args, "label")
    if value is None:
        return ""
    prefix = _literal_prefix(value)
    if prefix is not None:
        return prefix
    ident = re.match(r"[A-Za-z_$][\w$.]*", value)
    return ident.group(0) if ident else ""


def emitted_legs(script: str) -> list[EmittedLeg]:
    """One `EmittedLeg` per `agent(` call site, in source order; a templated label keeps its literal prefix."""
    scrubbed = scrub(script)
    legs: list[EmittedLeg] = []
    for m in _AGENT_CALL_SITE.finditer(scrubbed):
        open_paren = m.end() - 1
        close = _find_matching_paren(scrubbed, open_paren)
        s_args, r_args = scrubbed[open_paren:close], script[open_paren:close]
        legs.append(
            EmittedLeg(
                label=_label(s_args, r_args),
                phase=_string_option(s_args, r_args, "phase"),
                agent_type=_string_option(s_args, r_args, "agentType"),
                model=_string_option(s_args, r_args, "model"),
            )
        )
    return legs


def lookup(workflow: str, label: str) -> Optional[LegDecl]:
    for decl in REGISTER:
        if decl.workflow == workflow and decl.label == label:
            return decl
    return None
