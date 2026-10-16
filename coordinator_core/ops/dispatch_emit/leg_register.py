"""Leg register: the declared class and reason of every LLM leg an emitted workflow composes.

`emitted_legs` reads the `agent(` call sites of a composed script; `REGISTER` holds one `LegDecl`
per (workflow, label). Imports nothing from an emitter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from coordinator_core.ops._workflow_contract import (
    _AGENT_CALL_SITE,
    _find_matching_paren,
    _quoted_end,
    _template_end,
    scrub,
)


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


RUNTIME_FLOOR_RULING = "state/apm-rulings/2026-10-15-engine-ask-phase-consolidation.md"


def _earns(workflow: str, label: str, reason: str) -> LegDecl:
    return LegDecl(workflow, label, LegClass.EARNS_ITS_COST, reason)


def _floor(workflow: str, label: str, reason: str) -> LegDecl:
    return LegDecl(workflow, label, LegClass.OP_WRAPPER, reason, runtime_floor=RUNTIME_FLOOR_RULING)


_COMMIT_FLOOR = "the commit guard admits only coordinator:git-commit-agent, so it cannot fold into a preceding agent"
_REVIEW_ROLES = (
    ("review:coordinator:code-reviewer", "reads the diff and rules on it"),
    ("review:coordinator:integrator", "applies the accepted review fixes"),
)


def _review_legs(workflow: str, *, prep: Optional[LegDecl] = None) -> tuple[LegDecl, ...]:
    """The review roles every workflow composes; `prep` is absent where the wave runs `review.prep` itself."""
    return (*((prep,) if prep else ()), *(_earns(workflow, label, reason) for label, reason in _REVIEW_ROLES))


REGISTER: tuple[LegDecl, ...] = (
    _earns("ask", "size", "scouts the ask and judges its size and route"),
    _earns("ask", "size-reword", "rewords a refused exit criterion; absorbed the gate op and sizing-assemble that ran after it"),
    _earns("ask", "accept", "the APM rules on the exit criterion"),
    _earns("ask", "xl-exit", "the APM picks the XL exit; absorbed the split fanout op"),
    _earns("ask", "plan", "authors the plan"),
    _earns("ask", "plan-review", "reviews the plan"),
    _earns("ask", "plan-revise", "revises the plan against its review"),
    _earns("ask", "work:", "executes one row"),
    _earns("ask", "verify:", "runs a row's scoped tests and reports"),
    _earns(
        "ask", "verify",
        "runs the manifest's scoped tests, writes the declared docs and note, then rules the criterion and the "
        "ledger; absorbed the terminal test, criterion-judge, docs and ledger-check legs and the reviewer rows' verdict-write",
    ),
    _floor("ask", "gate", "runs the gate op where no agent precedes it; a preceding size agent absorbs it"),
    _floor("ask", "commit", _COMMIT_FLOOR),
    _floor("ask", "halt-commit", _COMMIT_FLOOR),
    _floor("ask", "commit:wave-", _COMMIT_FLOOR),
    *_review_legs("ask"),
    _earns("emit", "work:", "executes one row"),
    _earns("emit", "seam:wave-", "relays the wave's seam record and runs reachability over its landed range; "
          "runs the landed python test files when any landed"),
    _earns("emit", "verify:", "runs a row's scoped tests and reports"),
    _earns("emit", "falsifier-integrity:", "judges whether the falsifier can report red; absorbed the verdict-cache write"),
    _earns("emit", "chatty-overseer", "oversees the chatty roster"),
    _earns("emit", "chatty-survey", "surveys the chatty roster"),
    _earns("emit", "chatty-summary", "summarises the chatty run"),
    _earns("emit", "continue:", "continues a chatty executor"),
    _earns(
        "emit", "verify",
        "runs the plan's scoped tests, then rules the exit criterion in one agent on the judge's type, "
        "absorbing the serial test:terminal and exit-criterion-judge legs of the judge arm",
    ),
    _earns("emit", "test:terminal", "runs the plan's scoped tests once, after every wave, and reports"),
    _earns("emit", "review:coordinator:test-runner", "freezes the diff, runs tests and reports"),
    _earns("emit", "review:coordinator:overengineering-reviewer", "reads the diff for overbuild"),
    _earns("emit", "review:coordinator:exit-criterion-judge", "rules whether the plan's exit criterion is met"),
    _earns("emit", "review:general-purpose", "reviews the diff as a general reviewer"),
    _floor("emit", "run-base", "first step of the script: git rev-parse HEAD, no agent precedes it"),
    _floor("emit", "commit:wave-", _COMMIT_FLOOR),
    *_review_legs("emit", prep=_earns("emit", "review:coordinator:review-prep", "multi-step review prep")),
    _earns("grind", "size", "scouts a row and judges its size; absorbed the sizing-assemble op that ran after it"),
    _earns("grind", "triage", "classifies a row's fix verdict"),
    _earns("grind", "fix", "fixes the row"),
    _earns("grind", "close", "closes the row's record"),
    _earns("grind", "verify-agent", "runs the row's tests and reports"),
    _earns("grind", "adjudicate", "the APM adjudicates a held row"),
    _floor("grind", "verify-op", "verify runs on an agent with no Edit/Write so the fix agent cannot launder the gate and absorbs the undo op and every non-landing row's undo"),
    _floor("grind", "ledger-sweep:drain", "the drain runs after every row, no agent precedes it; absorbs the run-record op"),
    _floor("grind", "commit", _COMMIT_FLOOR),
    _floor("grind", "review-fix-commit", _COMMIT_FLOOR),
    *_review_legs("grind", prep=_earns("grind", "review:coordinator:review-prep", "multi-step review prep")),
    *_review_legs("review", prep=_earns("review", "review:coordinator:review-prep", "multi-step review prep")),
    _earns("review", "review:coordinator:overengineering-reviewer", "reads the diff for overbuild"),
    _earns("review", "review:coordinator:staff-eng", "reviews the diff as staff engineer"),
    _earns("review", "review:coordinator:test-runner", "freezes the diff, runs tests and reports"),
    _earns("review", "review:general-purpose", "reviews the diff as a general reviewer"),
)

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


_REGEX_KEYWORDS = frozenset({"return", "typeof", "case", "in", "of", "void", "delete", "throw", "else", "do"})
_TRAILING_WORD = re.compile(r"[\w$]+$")


def _regex_may_start(script: str, i: int) -> bool:
    """True when the `/` at `i` opens a regex literal rather than dividing: the token before it is not a value."""
    before = script[:i].rstrip()
    if not before:
        return True
    last = before[-1]
    if last.isalnum() or last in "_$":
        word = _TRAILING_WORD.search(before)
        return bool(word) and word.group(0) in _REGEX_KEYWORDS
    return last not in ")]}'\"`"


def _mask_regex_literals(script: str) -> str:
    """Same-length copy with every regex literal's body blanked.

    Trap: `scrub` reads a quote inside a regex literal (`/'/g`) as a string delimiter and inverts its
    quote state for the rest of the script, hiding every `agent(` call after it.
    """
    out = list(script)
    n = len(script)
    i = 0
    while i < n:
        ch, nxt = script[i], script[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = script.find("\n", i)
            i = n if j < 0 else j
        elif ch == "/" and nxt == "*":
            j = script.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif ch in ("'", '"'):
            i = _quoted_end(script, i + 1, ch) + 1
        elif ch == "`":
            i = _template_end(script, i + 1) + 1
        elif ch == "/" and _regex_may_start(script, i):
            j, in_class = i + 1, False
            while j < n and script[j] != "\n" and (in_class or script[j] != "/"):
                if script[j] == "\\":
                    j += 1
                elif script[j] == "[":
                    in_class = True
                elif script[j] == "]":
                    in_class = False
                j += 1
            if j < n and script[j] == "/":
                out[i + 1 : j] = "x" * (j - i - 1)
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def emitted_legs(script: str) -> list[EmittedLeg]:
    """One `EmittedLeg` per `agent(` call site, in source order; a templated label keeps its literal prefix."""
    scrubbed = scrub(_mask_regex_literals(script))
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
