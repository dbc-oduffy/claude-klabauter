"""coordinator_core.hooks.nudge_plan_test_surface_tier — PreToolUse
(Write|Edit|MultiEdit) advisory op: plan-write-time ADVISORY closing the gap
where a dispatch-time-only guard never sees a plan body being authored.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/nudge-plan-test-surface-tier.py`.
Shape changes, all forced by this row's own op contract, none a behaviour
change:

  (a) stdin/stdout JSON I/O becomes the `params`-dict-in / envelope-dict-out
      contract every op in this package shares.
  (b) The source's own cross-plane `_classify()` seam (`_engine_root.
      resolve_claude_klabauter_root()` + `place_engine_root_on_path` to reach
      `coordinator_core.bash_guards.check_test_suite_invocation.
      classify_text` from OUTSIDE this repo) collapses to a plain same-repo
      import -- `classify_text` already lives in this tree, no plane
      boundary to cross, no sys.path surgery, no failure mode to fail open
      on for "engine root unresolvable" (that whole class of failure no
      longer exists at this call site).
  (c) `_message_envelope`'s `emit(message, CHANNEL_ADDITIONAL_CONTEXT)`
      (a stdout-writing side effect shaped for the old stdin/stdout hook
      entrypoint) becomes `coordinator_core._hook_envelope.allow_advisory`,
      returning the envelope dict directly -- matches this row's own
      `guard_python_syntax_on_write.py` precedent for the same reason.

Why this exists (unchanged from the source): an EM can write "run the full
suite green" into a chunk's test-surface row, the plan gets reviewed and
ratified carrying it, and the only catch is the dispatch guard firing
later, after the plan has already cleared review on the strength of a
test-surface row that was never enforceable as written. See
`coordinator/skills/plan/residue/shared-corpus.md` (a DoE-claude-resident
skill doc; the row's constraint restated here rather than re-fetched): the
test-surface row must name a Tier T, path-scoped surface, never the repo's
fast tier or full suite.

ADVISORY, NOT A DENY. A plan body legitimately QUOTES a suite command in
prose, an Anti-scope list, or a Tried/Failed record -- a dispatch prompt is
an instruction, a plan body is a document. Per global doctrine's "design
tooling as offers, not nags", this leads with the alternative: name the
chunk's own path-scoped (Tier T) tests -- global/cadence-tier verification
is EM-owned at the wave boundary, never a chunk deliverable.

Tripwire token: PLAN-TEST-SURFACE-TIER.

Classification -- ZERO new regex over suite commands. This hook reuses
`coordinator_core.bash_guards.check_test_suite_invocation.classify_text`
directly (same classifier a sibling dispatch-time guard uses). A
`classify_text` match with `position == "imperative"` is, by that module's
own contract, ALWAYS a Tier-F or Tier-U (suite-shaped, i.e. NOT
path/node-id-scoped) invocation -- `classify_text` never reports a Tier-T
match at all (see its module docstring's negative spec) -- so "any
imperative match" and "names the fast tier or full suite" are the same
predicate here; no separate tier check is needed. `position` in
{"fenced_code", "inline_code", "negated", "reported", "descriptive",
"unknown"} is exactly the "quoting it in prose, not instructing it" shape a
plan body legitimately carries, and is never advised on.

Plan-body detection -- mirrors the sibling engine-plane module
`coordinator_core.write_guards.block_subagent_plan_body_write`'s regex
idiom, narrowed to `docs/plans/` only (not `docs/problems/`, which is a
ratified problem-set, not a chunk-bearing plan body with a test-surface
row). Applied to the raw `file_path` string (separator-normalized), same
as that module -- no `Path.resolve()`, so this fires identically whether
the dispatching call named an absolute or repo-relative path. A
`tests/fixtures/**` path is exempted for the same reason that module
exempts it: static golden fixtures under a test package are not a live
plan a chunk deliverable will ever be dispatched from.

Reconstructing the write's content -- via the shared
`coordinator_core.write_guards._sentinel_write_guard.reconstruct_after`
(Write: `content` verbatim; Edit: single or `replace_all` substitution
into the on-disk `before`; MultiEdit: the same, applied sequentially).
This hook classifies `after` directly (not a before/after diff) -- unlike
a citation guard, there is no large legacy-violation corpus to avoid
re-flagging; a plan body carrying an un-fixed Tier-F/U test-surface row is
worth advising on every touch of that file until it is fixed.

Fail-open guards (all silent, `no_advisory()`): `tool_name` not in
{Write, Edit, MultiEdit}; no target path in `tool_input`; target not
plan-body-shaped (or under `tests/fixtures/`); `after`-content
reconstruction ambiguous; `classify_text` itself raising; zero imperative
matches. A broken/half-landed classifier must never brick plan authoring.

SECOND DETECTOR -- a plan cannot pick its execution vehicle. Kept from the
source unchanged: matches `_VEHICLE_PHRASE_PATTERNS` ONLY inside a plan
body's `## Anti-scope` section (the first `## Anti-scope` heading to the
next same-level `## ` heading, fenced code blocks excluded). A vehicle-
naming sentence anywhere else in the body is deliberately invisible to
this detector -- see the standing objection to whole-corpus prose-matching
this narrowing answers by construction rather than by tuning.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from coordinator_core._hook_envelope import allow_advisory, no_advisory
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

#: Mirrors `coordinator_core.write_guards.block_subagent_plan_body_write.
#: _PLAN_BODY_RE`, narrowed to `plans/` only -- see module docstring.
_PLAN_BODY_RE_SUFFIX = "/docs/plans/"

#: Mirrors that same module's `_FIXTURE_PATH_RE` test-fixture exemption.
_FIXTURE_SEGMENT = "/tests/fixtures/"

_TOKEN = "PLAN-TEST-SURFACE-TIER"

#: Kept narrow (literal execution-vehicle nouns, not generic mechanism/
#: registration words) so it never matches a legitimate hook/registration
#: prohibition sitting in the same section.
_VEHICLE_TOKEN = "A-PLAN-DOES-NOT-PICK-THE-EXECUTION-VEHICLE"

_ANTI_SCOPE_HEADING = "## Anti-scope"

_VEHICLE_PHRASE_PATTERNS: "tuple[re.Pattern[str], ...]" = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfan-out\b",
        r"\bem-sequenced\b",
        r"\bchunk[- ]at[- ]a[- ]time\b",
        r"\bhand-dispatch(?:ed)?\b",
        r"one executor owns the whole thing",
    )
)


def _extract_anti_scope_section(text: str) -> str:
    """Section boundary: the first `## Anti-scope` heading (exact, own
    line) to the next same-level `## ` heading, exclusive. Fenced code
    blocks (```...```) within that span are stripped before returning, so a
    quoted example inside a fence never reaches the phrase matcher. Returns
    "" when no `## Anti-scope` heading exists -- the caller treats that as
    "nothing to match".

    Fence parity is carried in from the top of the document, not reset to
    False at `start` -- a fence opened before `## Anti-scope` and still
    open when the section begins would otherwise make genuinely-fenced
    content read as plain text."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == _ANTI_SCOPE_HEADING:
            start = i + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    in_fence = False
    for line in lines[:start]:
        if line.strip().startswith("```"):
            in_fence = not in_fence

    kept: "list[str]" = []
    for line in lines[start:end]:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        kept.append(line)
    return "\n".join(kept)


def _vehicle_match(anti_scope_text: str) -> "str | None":
    """First `_VEHICLE_PHRASE_PATTERNS` match in `anti_scope_text`, or
    None. Caller is responsible for having already scoped `anti_scope_text`
    to a `## Anti-scope` section -- this function does no section detection
    of its own."""
    for pattern in _VEHICLE_PHRASE_PATTERNS:
        hit = pattern.search(anti_scope_text)
        if hit:
            return hit.group(0)
    return None


def _vehicle_advisory_message(target: str, phrase: str):
    return compose(
        f"{_VEHICLE_TOKEN}: Anti-scope names an execution vehicle (\"{phrase}\") -- "
        "a plan owns what changes, not how it is dispatched. Use instead: "
        "`a depends_on edge or a named carve-out`"
    )


def _normalize_path(file_path: str) -> str:
    """Native-separator -> slash, collapse repeated slashes -- same
    normalization intent as `block_subagent_plan_body_write._normalize_path`
    (so the two guards agree on what counts as a plan-body path), expressed
    via `os.sep` rather than a literal backslash."""
    normalized = file_path.replace(os.sep, "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _is_plan_body_path(normalized: str) -> bool:
    """True iff `normalized` is a `docs/plans/**/*.md` path, at any depth,
    and NOT under a `tests/fixtures/` segment."""
    if not normalized.endswith(".md"):
        return False
    if not (normalized.startswith("docs/plans/") or _PLAN_BODY_RE_SUFFIX in normalized):
        return False
    if _FIXTURE_SEGMENT in normalized or normalized.startswith("tests/fixtures/"):
        return False
    return True


def _classify(text: str, cwd: "str | None") -> "list[Any]":
    """Calls the local `classify_text` classifier. Returns [] (never
    raises) on any failure -- a broken/half-landed classifier must never
    brick plan authoring."""
    try:
        from coordinator_core.bash_guards.check_test_suite_invocation import (
            classify_text,
        )
    except Exception:
        return []

    try:
        return list(classify_text(text, cwd=cwd or os.getcwd()))
    except Exception:
        return []


def _advisory_message(target: str, detected: str):
    return compose(
        f"{_TOKEN}: names {detected} as test surface. Use instead: "
        "`Tier T, path-scoped to the chunk's own files`"
    )


@register_op("hooks.nudge_plan_test_surface_tier")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: advise (never deny) when a plan
    body's write leaves a Tier-F/U test-surface row, or an Anti-scope
    section naming an execution vehicle."""
    try:
        return _handle(params)
    except Exception:
        return no_advisory()


def _handle(params: dict) -> dict:
    if params.get("tool_name", "") not in _GUARDED_TOOLS:
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return no_advisory()

    normalized = _normalize_path(target_raw)
    if not _is_plan_body_path(normalized):
        return no_advisory()

    try:
        target_path = Path(target_raw)
        before = (
            target_path.read_text(encoding="utf-8", errors="replace")
            if target_path.is_file()
            else ""
        )
    except Exception:
        before = ""

    after = reconstruct_after(params.get("tool_name", ""), tool_input, before)
    if after is None:
        return no_advisory()

    cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None
    matches = _classify(after, cwd)
    imperative = [m for m in matches if getattr(m, "position", "") == "imperative"]
    if imperative:
        hit = imperative[0]
        detected = getattr(hit, "detected", "a test-suite command")
        context = render(_advisory_message(target_raw, detected))
        return allow_advisory("PreToolUse", context)

    anti_scope_text = _extract_anti_scope_section(after)
    if anti_scope_text:
        phrase = _vehicle_match(anti_scope_text)
        if phrase:
            context = render(_vehicle_advisory_message(target_raw, phrase))
            return allow_advisory("PreToolUse", context)

    return no_advisory()
