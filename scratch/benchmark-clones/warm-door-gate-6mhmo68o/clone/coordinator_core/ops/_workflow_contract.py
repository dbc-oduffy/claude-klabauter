"""
coordinator_core.ops._workflow_contract — Workflow-script correctness-contract SSOT.

Purpose: single source of truth for the machine-readable correctness contract a
fleet Workflow `.mjs` script must satisfy, encoded as a DATA-DRIVEN CHECK
REGISTRY — (pattern/code/message/severity) tuples consumed by a generic
apply-checks function, not hardcoded per-check branches — so contract drift
against the evolving harness Workflow tool description (the living SSOT) is a
data edit, not a logic rewrite. Imported by both `workflow.validate` (C2) and
`workflow.scaffold` (C3) so neither op re-implements a check.

Spec backlink: pln-workflow-skeleton-stamper-maki-adab0d § C1

Pure functions only; no I/O, no JS parser (no AST) — a bounded regex/line-based
textual contract over conformant-shaped scripts, per the plan's "regex, not
parser" design decision. HOUSE_PATTERNS (the four scaffold templates) is NOT in
this module — it lives in C3's `_workflow_patterns.py`; this module owns only
the shared checks + scrubber + severity map.

Negative-spec:
  - Does NOT build an AST or understand JS statements/expressions — only which
    spans are "inside a string/template/comment" vs "code" (see `scrub`).
  - Does NOT contain the house-pattern scaffold templates (C3's surface).
  - Does NOT perform any file I/O — callers pass script text in, get findings
    out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Set

# ---------------------------------------------------------------------------
# Severity + Finding
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """ERROR = checks that actually throw at parse/runtime (impure meta,
    forbidden globals). WARN = everything else — advisory, never a hard fail
    (design-as-offers: a check that hard-fails false positives becomes a nag
    EMs route around)."""

    ERROR = "ERROR"
    WARN = "WARN"


@dataclass(frozen=True)
class Finding:
    """A single contract violation.

    Attributes:
        severity: ERROR (fails validation) or WARN (advisory).
        code: short machine-stable identifier, e.g. "forbidden-global-date-now".
        message: actionable human-readable text — names the offending token
            AND the fix, never a bare "invalid" style message.
        line: 1-indexed line number if known, else None.
    """

    severity: Severity
    code: str
    message: str
    line: Optional[int] = None


# ---------------------------------------------------------------------------
# Scrubber (the Staff Engineer F1) — string / template-literal / comment masking
# ---------------------------------------------------------------------------
#
# A minimal, pure-Python, JS-AWARE-BUT-NOT-A-PARSER state machine: it tracks
# only "am I currently inside a string / template-literal / comment span" and
# replaces the CONTENTS of each such span with a neutral placeholder
# character, preserving every newline (so line numbers reported against the
# scrubbed text still line up with the original script) and preserving the
# delimiters themselves (so a subsequent check can still see e.g. a `` ` ``
# opened-and-not-closed shape if that ever matters). This is NOT a JS parser:
# it does not build an AST or understand statements, only span membership.


def scrub(script: str) -> str:
    """Return `script` with string/template-literal/comment CONTENTS masked.

    Masks the contents (not the delimiters) of:
      - single-quoted strings: 'like this'
      - double-quoted strings: "like this"
      - template literals: `like this`, including `${...}` interpolations
        (the interpolation's own code is also masked — the contract has no
        need to see inside it, and masking it uniformly avoids re-entering
        "am I in code" state tracking for nested expressions)
      - line comments: // like this
      - block comments: /* like this */

    Escape sequences (`\\'`, `\\"`, `` \\` ``, `\\\\`) are honored so an
    escaped delimiter does not prematurely end a span. Newlines inside a
    masked span are preserved verbatim so line numbers of the scrubbed text
    match the original script exactly (a masked char replaces a masked char,
    1:1, except the delimiters and newlines which pass through unchanged).

    This is the F1 fix: every ERROR-tier textual check runs against
    `scrub(script)`, never raw text, so e.g. an `agent()` prompt template
    literal that literally contains the text "Date.now()" is masked before
    any forbidden-global regex ever sees it.
    """
    out: List[str] = []
    i = 0
    n = len(script)
    placeholder = "#"
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        if ch == "/" and nxt == "/":
            out.append("/")
            out.append("/")
            i += 2
            while i < n and script[i] != "\n":
                out.append(placeholder)
                i += 1
            continue

        if ch == "/" and nxt == "*":
            out.append("/")
            out.append("*")
            i += 2
            while i < n and not (script[i] == "*" and i + 1 < n and script[i + 1] == "/"):
                out.append(placeholder if script[i] != "\n" else "\n")
                i += 1
            if i < n:
                out.append("*")
                out.append("/")
                i += 2
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n and script[i] != quote:
                if script[i] == "\\" and i + 1 < n:
                    out.append(placeholder)
                    out.append(placeholder if script[i + 1] != "\n" else "\n")
                    i += 2
                    continue
                out.append(placeholder if script[i] != "\n" else "\n")
                i += 1
            if i < n:
                out.append(quote)
                i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# Forbidden globals (ERROR tier — run against scrubbed text)
# ---------------------------------------------------------------------------

FORBIDDEN_GLOBALS = [
    (
        # Review: code-reviewer (Finding 4, nit) — negative lookbehind so a
        # namespaced property access (myObj.Date.now()) does not false-positive
        # on the global Date.now() this check targets.
        re.compile(r"(?<!\.)\bDate\.now\s*\("),
        "forbidden-global-date-now",
        "Date.now() is unavailable in Workflow scripts and throws at runtime "
        "(breaks resume) — remove this call; if you need a timestamp, derive "
        "it from op output or a passed-in param instead.",
    ),
    (
        re.compile(r"(?<!\.)\bMath\.random\s*\("),
        "forbidden-global-math-random",
        "Math.random() is unavailable in Workflow scripts and throws at "
        "runtime (breaks resume) — remove this call; use a deterministic "
        "value or an op-provided id instead.",
    ),
    (
        re.compile(r"new\s+Date\s*\(\s*\)"),
        "forbidden-global-new-date",
        "new Date() (argless) is unavailable in Workflow scripts and throws "
        "at runtime (breaks resume) — pass an explicit argument "
        "(new Date(someTimestamp)) or remove it; argless new Date() is the "
        "only forbidden form, new Date(arg) is fine.",
    ),
]


def check_forbidden_globals(scrubbed: str) -> List[Finding]:
    """ERROR for each forbidden-global occurrence in `scrubbed` text."""
    findings: List[Finding] = []
    for pattern, code, message in FORBIDDEN_GLOBALS:
        for m in pattern.finditer(scrubbed):
            line = scrubbed.count("\n", 0, m.start()) + 1
            findings.append(Finding(Severity.ERROR, code, message, line))
    return findings


# ---------------------------------------------------------------------------
# meta block extraction + pure-literal check
# ---------------------------------------------------------------------------

META_REQUIRED_FIELDS = ["name", "description"]

_META_START = re.compile(r"export\s+const\s+meta\s*=\s*\{")


def extract_meta_block(source: str) -> Optional[str]:
    """Locate `export const meta = {` in `source` and return the block text
    INCLUDING the enclosing braces, via a brace-count scan to the matching
    close brace. Returns None if no `meta` export is found or the braces
    never balance (unterminated block).

    Runs against RAW (un-scrubbed) text deliberately: the pure-literal check
    over the returned block needs to see actual `` ` ``/`${`/field-value
    content, which the scrubber would mask. Braces inside string/template
    literals inside the block are rare in a conformant meta object (it's
    meant to be a flat literal) and are accepted as an out-of-scope
    limitation of the bounded regex/line contract (see plan's "not a style
    linter, not an AST" boundary)."""
    m = _META_START.search(source)
    if not m:
        return None
    start = m.end() - 1  # index of the opening '{'
    depth = 0
    i = start
    n = len(source)
    while i < n:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    return None


_PURE_LITERAL_VIOLATIONS = [
    (re.compile(r"\$\{"), "meta-impure-interpolation", "template-literal interpolation (${...})"),
    (re.compile(r"`"), "meta-impure-template-literal", "a backtick template literal"),
    (re.compile(r"\.\.\."), "meta-impure-spread", "a spread (...) expression"),
    (
        re.compile(r"\b[A-Za-z_$][\w$]*\s*\("),
        "meta-impure-call",
        "a function/method call",
    ),
]

# A bare identifier used as a VALUE (not a key) — e.g. `name: someVar,` — is
# impure. We look for `: <identifier>` NOT followed by a quote/brace/bracket/
# digit and not one of the JS literal keywords.
_BARE_IDENTIFIER_VALUE = re.compile(
    r":\s*([A-Za-z_$][\w$]*)\s*[,}\n]"
)
_JS_LITERAL_KEYWORDS = {"true", "false", "null", "undefined", "NaN", "Infinity"}


def check_meta_pure_literal(block: str) -> List[Finding]:
    """ERROR for each pure-literal violation found in the scrubbed meta
    block text (caller must pass `extract_meta_block(scrub(script))`, not
    a raw block — see `run_checks`): `${`, backticks, spreads (`...`),
    call-shape `\\w+\\s*\\(`, and bare-identifier values. `export const
    meta = {...}` must be a pure object literal — any of these shapes
    fails at parse time in the harness."""
    findings: List[Finding] = []
    for pattern, code, label in _PURE_LITERAL_VIOLATIONS:
        for m in pattern.finditer(block):
            line = block.count("\n", 0, m.start()) + 1
            findings.append(
                Finding(
                    Severity.ERROR,
                    code,
                    f"meta must be a pure literal but contains {label} "
                    f"({m.group(0)!r}) — meta computed at parse time throws; "
                    "replace with a literal value.",
                    line,
                )
            )
    for m in _BARE_IDENTIFIER_VALUE.finditer(block):
        ident = m.group(1)
        if ident in _JS_LITERAL_KEYWORDS:
            continue
        line = block.count("\n", 0, m.start()) + 1
        findings.append(
            Finding(
                Severity.ERROR,
                "meta-impure-bare-identifier",
                f"meta must be a pure literal but value {ident!r} is a bare "
                "identifier (a variable reference), not a literal — meta "
                "computed at parse time throws; inline the literal value.",
                line,
            )
        )
    return findings


def check_meta_required_fields(block: str) -> List[Finding]:
    """ERROR for each field in META_REQUIRED_FIELDS missing from `block`."""
    findings: List[Finding] = []
    for field in META_REQUIRED_FIELDS:
        pattern = re.compile(r"\b" + re.escape(field) + r"\s*:")
        if not pattern.search(block):
            findings.append(
                Finding(
                    Severity.ERROR,
                    f"meta-missing-{field}",
                    f"meta is missing required field {field!r} — add "
                    f"`{field}: '...'` to the meta object literal.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# phase() / meta.phases / agent-options phase: set-diff (WARN, both surfaces)
# ---------------------------------------------------------------------------

_PHASE_CALL = re.compile(r"\bphase\s*\(\s*['\"]([^'\"]*)['\"]")
_META_PHASES_ENTRY = re.compile(r"['\"]([^'\"]*)['\"]")
_AGENT_OPTIONS_PHASE = re.compile(r"\bphase\s*:\s*['\"]([^'\"]*)['\"]")


def phase_titles(source: str) -> Set[str]:
    """Return the set of phase titles from every `phase('X')` CALL site in
    `source` text (the phase()-call surface, distinct from the agent-options
    `phase:` surface — see `agent_options_phase_titles`). Runs against RAW
    (un-scrubbed) text — the title itself is inside a string literal, which
    the scrubber would mask."""
    return {m.group(1) for m in _PHASE_CALL.finditer(source)}


def agent_options_phase_titles(source: str) -> Set[str]:
    """Return the set of `phase: 'X'` values pulled from agent()/parallel-item
    OPTIONS OBJECTS in `source` text — a DISTINCT surface from the
    `phase()` call: an agent/parallel-item can carry its own `phase:` field
    that silently buckets into its own progress group if unmatched against
    `meta.phases`, independent of any `phase()` call elsewhere in the script.
    Runs against RAW (un-scrubbed) text for the same reason as
    `phase_titles`."""
    return {m.group(1) for m in _AGENT_OPTIONS_PHASE.finditer(source)}


def meta_phase_titles(block: str) -> Set[str]:
    """Return the set of phase titles declared in `meta.phases` within the
    (already-extracted, RAW) meta `block` text. Returns an empty set if no
    `phases:` array is present."""
    m = re.search(r"phases\s*:\s*\[([^\]]*)\]", block)
    if not m:
        return set()
    return {entry.group(1) for entry in _META_PHASES_ENTRY.finditer(m.group(1))}


def check_phase_mismatch(source: str, block: Optional[str]) -> List[Finding]:
    """WARN for each phase() title (and each agent-options phase: title)
    absent from meta.phases. Both surfaces are checked independently — a
    title present via phase() but not agent-options (or vice versa) is
    still only flagged for the surface where it's actually missing from
    meta.phases. Never ERROR: an unmatched phase title is non-fatal — it
    just gets its own separate progress group in the harness.

    `source` and `block` are RAW (un-scrubbed) text/sub-text — phase titles
    live inside string literals, which the scrubber would mask."""
    declared = meta_phase_titles(block) if block else set()
    findings: List[Finding] = []

    for title in sorted(phase_titles(source) - declared):
        findings.append(
            Finding(
                Severity.WARN,
                "phase-call-title-mismatch",
                f"phase({title!r}) is not listed in meta.phases — it will "
                "still run but gets its own separate (ungrouped) progress "
                f"group; add {title!r} to meta.phases to group it.",
            )
        )

    for title in sorted(agent_options_phase_titles(source) - declared):
        findings.append(
            Finding(
                Severity.WARN,
                "agent-options-phase-title-mismatch",
                f"agent()/parallel-item option phase: {title!r} is not "
                "listed in meta.phases — it will still run but gets its own "
                f"separate (ungrouped) progress group; add {title!r} to "
                "meta.phases to group it.",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Barrier-vs-pipeline heuristic (WARN)
# ---------------------------------------------------------------------------

_PARALLEL_CALL = re.compile(r"\bawait\s+parallel\s*\(")
_AGENT_CALL = re.compile(r"\bagent\s*\(")


def check_barrier_vs_pipeline(scrubbed: str) -> List[Finding]:
    """WARN once if the script contains `await parallel(` ... a non-agent
    transform ... `await parallel(` again — a shape that usually means the
    author wanted `pipeline()` (no barrier between stages) but reached for
    two sequential barriers instead, wasting wall-clock. A legitimate
    barrier (dedup/early-exit across ALL results before continuing) is a
    real use case, so this NEVER hard-fails — it is a single advisory WARN,
    not a per-occurrence ERROR."""
    matches = list(_PARALLEL_CALL.finditer(scrubbed))
    if len(matches) < 2:
        return []
    for first, second in zip(matches, matches[1:]):
        between = scrubbed[first.end() : second.start()]
        if not _AGENT_CALL.search(between):
            return [
                Finding(
                    Severity.WARN,
                    "barrier-vs-pipeline",
                    "two await parallel(...) calls with a non-agent "
                    "transform between them and no agent() work in between — "
                    "did you mean pipeline() instead? pipeline() runs items "
                    "through all stages with no barrier; two parallel() "
                    "calls force a full-barrier wait between stages, wasting "
                    "wall-clock unless you genuinely need to dedup/early-exit "
                    "across ALL results before continuing.",
                )
            ]
    return []


# ---------------------------------------------------------------------------
# Model-default heuristic (WARN)
# ---------------------------------------------------------------------------

_AGENT_CALL_SITE = re.compile(r"\bagent\s*\(")


def _find_matching_paren(scrubbed: str, open_paren_idx: int) -> int:
    """Return the index just past the matching close paren for the '(' at
    open_paren_idx, via a depth-count scan. Returns len(scrubbed) if
    unterminated."""
    depth = 0
    i = open_paren_idx
    n = len(scrubbed)
    while i < n:
        if scrubbed[i] == "(":
            depth += 1
        elif scrubbed[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def check_model_default(scrubbed: str) -> List[Finding]:
    """WARN for each agent(...) call site with no explicit `model:` key in
    its argument list. An agent() call with no model: inherits the SESSION
    model (Opus on an Opus session) — a ~4x-cost defect for mechanical
    fan-outs. Advisory only: the validator cannot textually distinguish a
    fan-out (should pin model:'sonnet') from a legitimate judgment agent
    (correctly inheriting Opus), so this is never ERROR."""
    findings: List[Finding] = []
    for m in _AGENT_CALL_SITE.finditer(scrubbed):
        open_paren = m.end() - 1
        close_paren = _find_matching_paren(scrubbed, open_paren)
        args = scrubbed[open_paren:close_paren]
        if not re.search(r"\bmodel\s*:", args):
            line = scrubbed.count("\n", 0, m.start()) + 1
            findings.append(
                Finding(
                    Severity.WARN,
                    "agent-model-default",
                    "agent() call has no explicit model: — it inherits the "
                    "session model (Opus on an Opus session), a ~4x-cost "
                    "defect for mechanical fan-outs; set model:'sonnet' for "
                    "mechanical fan-outs (a genuine judgment agent may "
                    "legitimately inherit Opus).",
                    line,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Registry + entry point
# ---------------------------------------------------------------------------
#
# A "check" here is any Callable[[str], List[Finding]] taking the scrubbed
# script text (and, for the two meta-block checks, closing over the
# extracted block via a thin wrapper) — apply_checks folds a registry of
# these into a single findings list. This is the data-driven-registry shape
# AC1 requires: adding a new textual check is "append a function to the
# list," not "add a branch to a giant if/elif."

CheckFn = Callable[[str], List[Finding]]


def run_checks(script: str) -> List[Finding]:
    """Run the full correctness contract against `script` and return every
    Finding, ERROR and WARN together, in a stable order: meta checks,
    forbidden globals, phase mismatch, barrier heuristic, model-default
    heuristic.

    Two views of the script are used, deliberately:
      - the RAW text, for checks that need to see actual string/template
        CONTENTS as data (meta field values, phase titles) — masking those
        would make the check blind to its own subject matter.
      - the SCRUBBED text (the Staff Engineer F1), for the ERROR-tier forbidden-globals
        check plus the barrier/model-default heuristics, which must NOT be
        tripped by a forbidden-global token or agent()/parallel( call-shape
        that merely appears as string/comment CONTENT (e.g. inside an
        agent() prompt template literal) rather than as real code.
    """
    scrubbed = scrub(script)
    block = extract_meta_block(script)  # raw block, kept for phase-title extraction
    # Review: code-reviewer (Finding 1, P0) — check_meta_pure_literal/check_meta_required_fields
    # must consume the SCRUBBED meta block, not raw text, per F1's mandate: a conformant
    # description string whose VALUE contains a call-shape token (e.g. "rank them (top 10)")
    # was false-positiving as a meta-impure-call ERROR because the raw block still exposes
    # the string CONTENTS the check's regexes match against.
    scrubbed_block = extract_meta_block(scrubbed) if block is not None else None

    findings: List[Finding] = []

    if block is None:
        findings.append(
            Finding(
                Severity.ERROR,
                "meta-missing",
                "no `export const meta = {...}` block found (or it never "
                "closes) — add a top-level `export const meta = { name: "
                "'...', description: '...' };` object literal.",
            )
        )
    else:
        findings.extend(check_meta_pure_literal(scrubbed_block))
        findings.extend(check_meta_required_fields(scrubbed_block))

    findings.extend(check_forbidden_globals(scrubbed))
    findings.extend(check_phase_mismatch(script, block))
    findings.extend(check_barrier_vs_pipeline(scrubbed))
    findings.extend(check_model_default(scrubbed))

    return findings
