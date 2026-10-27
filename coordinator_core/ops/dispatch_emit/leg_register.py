"""Leg register: the declared class and reason of every LLM leg an emitted workflow composes.

`emitted_legs` reads the `agent(` call sites of a composed script; `REGISTER` holds one `LegDecl`
per (workflow, label). Imports nothing from an emitter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
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
    prompt: Optional[str] = None


RELAY_MARKERS: tuple[str, ...] = (
    "return its JSON reply verbatim",
    "with the Read tool and return its JSON content verbatim",
    "Run exactly `",
    "rev-parse HEAD",
)


def relay_markers(prompt: Optional[str]) -> tuple[str, ...]:
    """The `RELAY_MARKERS` a leg prompt contains, in marker order; empty for None.

    `EmittedLeg.prompt` is the prompt's literal prefix (cut at the first `${` or `+`), so a marker
    after an interpolation, or a prompt that opens with code, is not seen."""
    if prompt is None:
        return ()
    return tuple(marker for marker in RELAY_MARKERS if marker in prompt)


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
    _earns("ask", "verify:", "runs a row's or a wave's scoped tests and reports"),
    _earns(
        "ask", "verify",
        "runs the manifest's scoped tests, writes the declared docs and note, then rules the criterion and the "
        "ledger; absorbed the terminal test, criterion-judge, docs and ledger-check legs and the reviewer rows' verdict-write",
    ),
    _earns("ask", "footprint", "reads the baton for an XS sizing's write footprint when the sizing carries none"),
    _earns("ask", "size-retry", "re-runs the scout when its first pass did not assemble the sizing"),
    _earns("ask", "apply-rulings", "applies the EM's J<n> rulings to a pulled plan in place; spawned only when the gate's pending_rulings is non-empty"),
    _earns("ask", "pull-split", "slices a pulled plan into smaller batons when its blocking findings exceed one baton"),
    _earns("ask", "ledger-replan", "re-plans the rows a resumed ledger still owes"),
    _earns("ask", "ledger-remaining", "executes the rows a resumed ledger still owes"),
    _earns("ask", "ledger-fix:", "fixes one reopened chunk behind a not_met ledger row inside its footprint; the chunks run in parallel"),
    _earns("ask", "ledger-reverify", "re-judges only the reopened rows and re-runs only the reopened chunks' scoped tests"),
    _earns("ask", "halt-fix", "closes the owned failing tests and blocked-slice findings inside the review footprint before the review_blocked halt"),
    _earns("ask", "halt-reverify", "re-runs only the owned failing ids and re-judges only the blocked slices' findings after a halt-fix leg"),
    _earns("ask", "ledger-recovery-record", "writes recovery.json once the loop's final chunk statuses are known; a held-only run has no other agent to fold the write into"),
    _floor(
        "ask", "blitz-stage-read",
        "stages when readyOp did not run and returns the stage op's reply (a manifest handle), only when plan-blitz "
        "did not run readyOp; no judgment agent precedes it",
    ),
    _floor(
        "ask", "xs-plan-stage",
        "stages the existing plan an XS sizing's plan edge names and returns the stage op's reply (a manifest "
        "handle), only when the gate's resume_plan is set; no judgment agent follows the gate before it",
    ),
    _floor("ask", "gate", "runs the gate op where no agent precedes it; a preceding size agent absorbs it"),
    _floor("ask", "commit", _COMMIT_FLOOR),
    _floor("ask", "halt-commit", _COMMIT_FLOOR),
    _floor("ask", "commit:wave-", _COMMIT_FLOOR),
    *_review_legs("ask"),
    _earns("emit", "work:", "executes one row"),
    _earns("emit", "seam:wave-", "relays the wave's seam record and runs reachability over its landed range; "
          "runs the landed python test files when any landed"),
    _earns("emit", "seam-fix:wave-", "repairs an execution-failed seam drift inside the wave's declared "
          "footprint, once per wave"),
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
    _earns("reverify", "review:general-purpose", "re-verifies a prior delivery FAIL's unbacked claims at HEAD"),
    _earns(
        "reverify", "review:coordinator:exit-criterion-judge",
        "runs the plan's scoped tests, then rules the exit criterion in one agent on the judge's type",
    ),
    _earns("reverify", "review:coordinator:test-runner", "re-runs the plan's scoped tests where no judge is rostered"),
    _earns("ask", "review:coordinator:test-runner", "re-runs the scoped tests over review-applied edits"),
    _earns("grind", "review:coordinator:test-runner", "re-runs the scoped tests over review-applied edits"),
    _earns("reverify", "review:coordinator:criterion-judge", "rules the plan's criterion against the register"),
    *_review_legs("review", prep=_earns("review", "review:coordinator:review-prep", "multi-step review prep")),
    _earns("review", "review:coordinator:overengineering-reviewer", "reads the diff for overbuild"),
    _earns("review", "review:coordinator:staff-eng", "reviews the diff as staff engineer"),
    _earns("review", "review:coordinator:test-runner", "freezes the diff, runs tests and reports"),
    _earns("review", "review:general-purpose", "reviews the diff as a general reviewer"),
)


class Disposition(str, Enum):
    """What the engine does with a leg: ENGINE never emits it, FOLD composes it only on a fallback arm,
    HOOK runs it in a hook with the agent call kept as the reader fallback, FLOOR is a closed op-wrapper,
    JUDGMENT stays an agent."""

    ENGINE = "engine"
    FOLD = "fold"
    HOOK = "hook"
    FLOOR = "floor"
    JUDGMENT = "judgment"


_DISPOSITION_WORKFLOWS = ("ask", "emit", "review")

#: Non-JUDGMENT dispositions. Two labels are split by arm in the plan's inventory but hold one key:
#: ("ask", "size-retry") is its no-values arm (the values arm composes under `gate`), and
#: ("ask", "verify") carries both the pre-rule fold and the fully-decided skip.
_NON_JUDGMENT: dict[tuple[str, str], tuple[Disposition, str]] = {
    ("ask", "footprint"): (
        Disposition.FOLD,
        "fold condition: the baton names write paths, so declared_footprint answers and the agent stays only as the fallback",
    ),
    ("ask", "verify"): (
        Disposition.FOLD,
        "fold condition: the engine pre-rule decides the ledger fields and --select-tests picks the tests; "
        "when nothing is owed the whole leg is skipped; otherwise the agent stays",
    ),
    ("ask", "gate"): (Disposition.FLOOR, "the gate op where no agent precedes it, and the values arm of size-retry"),
    ("ask", "blitz-stage-read"): (Disposition.FLOOR, "stage op relay with no preceding judgment agent"),
    ("ask", "xs-plan-stage"): (Disposition.FLOOR, "stage op relay with no judgment agent between the gate and it"),
    ("ask", "commit"): (Disposition.FLOOR, _COMMIT_FLOOR),
    ("ask", "halt-commit"): (Disposition.FLOOR, _COMMIT_FLOOR),
    ("ask", "commit:wave-"): (Disposition.FLOOR, _COMMIT_FLOOR),
    ("emit", "commit:wave-"): (Disposition.FLOOR, _COMMIT_FLOOR),
    ("emit", "seam:wave-"): (
        Disposition.FLOOR,
        "the seam record relay and reachability run is mechanical: the Sonnet executor becomes a haiku test-runner",
    ),
    ("emit", "review:coordinator:review-prep"): (
        Disposition.FOLD,
        "fold condition: prep_in_wave runs review prep inside the review wave; the leg stays otherwise",
    ),
    ("emit", "test:terminal"): (
        Disposition.FOLD,
        "fold condition: --select-tests derives the edited set; the leg stays when the selection is empty or refused",
    ),
}


def _dispositions() -> dict[tuple[str, str], tuple[Disposition, str]]:
    table = dict(_NON_JUDGMENT)
    for decl in REGISTER:
        if decl.workflow in _DISPOSITION_WORKFLOWS:
            table.setdefault((decl.workflow, decl.label), (Disposition.JUDGMENT, decl.reason))
    return table


DISPOSITIONS: Mapping[tuple[str, str], tuple[Disposition, str]] = MappingProxyType(_dispositions())

#: (workflow, step) -> (trigger label, fallback label, reason). Steps are not REGISTER labels. Empty until
#: the hook that runs a step has landed.
HOOK_STEPS: Mapping[tuple[str, str], tuple[str, str, str]] = MappingProxyType({})


def engine_labels(workflow: str) -> frozenset[str]:
    """Labels of `workflow` whose disposition is ENGINE: never emitted."""
    return frozenset(label for (wf, label), (d, _) in DISPOSITIONS.items() if wf == workflow and d is Disposition.ENGINE)


def hook_labels(workflow: str) -> frozenset[str]:
    """Fallback labels of `workflow`'s HOOK_STEPS: the agent calls that survive behind a hook."""
    return frozenset(fallback for (wf, _), (_, fallback, _) in HOOK_STEPS.items() if wf == workflow)


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
                prompt=_literal_prefix(r_args[1:]),
            )
        )
    return legs


def lookup(workflow: str, label: str) -> Optional[LegDecl]:
    for decl in REGISTER:
        if decl.workflow == workflow and decl.label == label:
            return decl
    return None
