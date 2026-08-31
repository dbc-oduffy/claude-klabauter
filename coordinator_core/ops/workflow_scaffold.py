"""
coordinator_core.ops.workflow_scaffold — JSON-RPC "workflow.scaffold" operation.

Purpose: pure-generation RPC that composes a green-by-construction fleet
Workflow `.mjs` skeleton from a caller-supplied name/description/phases, plus
one of the four named house patterns (coordinator_core.ops._workflow_patterns,
the C3 template SSOT), embedded as a commented fill-in block. Returns the
composed script TEXT — it does not write to disk; the caller (a DoE bash
veneer, the claude-klabauter op-invoke CLI via stdout redirect, or an EM) places the
file. Scope "none" / COMPUTE_ONLY, mirrors ops/cartography_symbols.py's
registration pattern.

Wire params:
    name (str, required)          — meta.name value (pure literal).
    description (str, required)   — meta.description value (pure literal).
    phases (list[dict], optional) — [{"title": str, "detail": str}, ...];
                                     empty/omitted emits a single default
                                     "Run" phase so the scaffold is always
                                     phase-conformant by construction.
    pattern (str, optional)       — one of "pipeline-default",
                                     "disk-poll-fanout", "adversarial-verify",
                                     "loop-until-dry". OMITTED (DoE consult
                                     note 3) -> "pipeline-default" (matches
                                     the harness "default to pipeline()"
                                     guidance) — never errors, never silently
                                     defaults to a fan-out shape.

Reply fields:
    {"script": "<text>"} — the composed .mjs script text. NO file write.

Emitted-script conformance guarantee: the composed `meta` block is a PURE
LITERAL (no variables/calls/spreads/interpolation); every `phase()` call title
exactly matches a `meta.phases` entry; every placeholder `agent()` call carries
an ACTIVE `model: 'sonnet'` (PM ruling 2026-07-12 — Opus is a deliberate edit,
not the accidental session-model inheritance the model-default WARN guards
against) AND a `phase:` option matching `meta.phases`. This makes scaffold
output model-default-WARN-clean and phase-conformant BY CONSTRUCTION — proven
by this op's own round-trip test feeding scaffold output straight into
`workflow.validate` (C2) for all four patterns.

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
     The handler builds a string in memory from caller-supplied params and
     the HOUSE_PATTERNS dict; no file is opened, no git object is touched.
  2. Writes into rag's relational store?                                 No.
     Returns a structured dict ({"script": "<text>"}) to the caller; no rag
     interaction of any kind.
  3. Opens any file for write (including sentinel creation)?             No.
     No file I/O of any kind — pure string composition in memory.
  4. Mutates shared mutable state outside its own module?                No.
     Reads (never mutates) the module-level HOUSE_PATTERNS dict; every other
     value is a local string built fresh per call. No shared/global state is
     written by this handler.
  5. Persistent state changes observable across process boundaries?     No.
     Nothing is written to disk; the only observable effect is the return
     value handed back to the caller. Placing the returned text into a file
     is the CALLER's responsibility (the DoE veneer, an op-invoke CLI stdout
     redirect, or an EM) — deliberately kept out of this op's budget/surface
     (see plan § "workflow.scaffold returns text; it does NOT write to disk").
  Git-shelling-is-read-only precedent: this handler shells out to nothing and
  reads nothing — it is pure-Python string composition, a strictly narrower
  profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess reads
  (DR-208's own table: "all subprocess calls are read-only git queries"). No
  subprocess call, and no file read, is made here at all.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: pln-workflow-skeleton-stamper-maki-adab0d § C3
"""

from __future__ import annotations

from typing import Optional
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.ops._workflow_patterns import HOUSE_PATTERNS

_DEFAULT_PATTERN = "pipeline-default"
_DEFAULT_PHASE_TITLE = "Run"


def _js_string_literal(value: str) -> str:
    """Render `value` as a single-quoted JS string literal, escaping
    backslashes/single-quotes/newlines so the emitted meta block stays a
    PURE LITERAL (no unescaped delimiter can prematurely close the string)."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
    )
    return f"'{escaped}'"


def _normalize_phases(phases: Optional[list]) -> list:
    """Return a non-empty list of {"title", "detail"} dicts. An empty/omitted
    `phases` param falls back to a single default phase so the emitted
    script is always phase-conformant by construction (a script with zero
    phases would have nothing for the scaffold's placeholder phase()/agent()
    calls to match against)."""
    if not phases:
        return [{"title": _DEFAULT_PHASE_TITLE, "detail": "primary work"}]
    normalized = []
    for entry in phases:
        title = entry.get("title") or _DEFAULT_PHASE_TITLE
        detail = entry.get("detail") or ""
        normalized.append({"title": title, "detail": detail})
    return normalized


def _compose_script(name: str, description: str, phases: list, pattern: str) -> str:
    phase_titles = [p["title"] for p in phases]
    meta_phases_literal = ", ".join(_js_string_literal(t) for t in phase_titles)

    meta_block = (
        "export const meta = {\n"
        f"  name: {_js_string_literal(name)},\n"
        f"  description: {_js_string_literal(description)},\n"
        f"  phases: [{meta_phases_literal}],\n"
        "};\n"
        "// whenToUse: '...' — optional: describe the trigger conditions for this Workflow\n"
    )

    phase_calls = "\n".join(
        f"  phase({_js_string_literal(p['title'])});"
        f"  // {p['detail']}" if p["detail"] else f"  phase({_js_string_literal(p['title'])});"
        for p in phases
    )

    agent_calls = "\n".join(
        f"  await agent('TODO: prompt for {p['title']}', "
        f"{{ label: 'work:{p['title']}', phase: {_js_string_literal(p['title'])}, "
        f"model: 'sonnet' }});"
        for p in phases
    )

    template = HOUSE_PATTERNS[pattern]

    # Top-level, never `async function run(ctx) { ... }` -- the harness
    # Workflow contract executes the script BODY directly and never looks
    # for, defines, or calls a `run` export; see
    # coordinator_core/ops/dispatch_emit/emit.py module docstring §
    # Top-level body, never a defined-but-uninvoked wrapper.
    return (
        f"{meta_block}\n"
        f"{phase_calls}\n"
        f"{agent_calls}\n"
        "\n"
        f"// house pattern: {pattern}\n"
        f"{template}"
    )


@register_op("workflow.scaffold")
def _workflow_scaffold(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.scaffold" handler.

    Args (via params):
        name (str): meta.name value.
        description (str): meta.description value.
        phases (list[dict], optional): [{"title", "detail"}, ...]; falls
            back to a single default "Run" phase when omitted/empty.
        pattern (str, optional): one of HOUSE_PATTERNS' keys; OMITTED ->
            "pipeline-default" (DoE consult note 3 — never errors, never
            silently defaults to a fan-out shape).

    Returns:
        {"script": "<text>"} — composed .mjs script text, no file write.

    Raises:
        ValueError — if `name` or `description` is missing (descriptive
        message naming the required param, matching the cartography.symbols
        error contract), or if `pattern` is supplied but not one of
        HOUSE_PATTERNS' keys.
    """
    name = params.get("name")
    if not name:
        raise ValueError("workflow.scaffold requires param: name")
    description = params.get("description")
    if not description:
        raise ValueError("workflow.scaffold requires param: description")

    pattern = params.get("pattern") or _DEFAULT_PATTERN
    if pattern not in HOUSE_PATTERNS:
        raise ValueError(
            f"workflow.scaffold received unknown pattern {pattern!r} — "
            f"expected one of {sorted(HOUSE_PATTERNS.keys())}"
        )

    phases = _normalize_phases(params.get("phases"))
    script = _compose_script(name, description, phases, pattern)

    return {"script": script}
