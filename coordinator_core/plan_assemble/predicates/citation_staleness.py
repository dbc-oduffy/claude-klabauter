"""
coordinator_core.plan_assemble.predicates.citation_staleness — the
`gates.substrate.staleness.*` Layer 0 leaf readers for contract row
`:85-87`, the hardest row in the `plan-assemble` contract.

Purpose: `:85-87` asks two DIFFERENT staleness questions about the plan
under assembly, at very different cost.

  Leg 1 (cheap) — SCOPE-PATH staleness: for each `scope:` entry in the
  plan's own frontmatter, has the path gone missing since the plan cited
  it? Composed directly over `coordinator_core.ops.doc_staleness.
  _log_follow_records` (the shipped `git log --follow` reader) — this
  module does not re-derive rename-aware history walking.

  Leg 2 (the hard one) — CITED-LINE staleness: the plan's own prose cites
  specific `` `path/to/file.py:N` `` targets (the same backtick
  `path:line` shape this repo's own docs use, e.g.
  `` `plan.schema.json:43` ``). For each one, this module recovers what
  the citation CLAIMS lives at that line — the nearest backtick-quoted
  span adjacent to the citation token in the surrounding prose, called
  the citation's ANCHOR TEXT — and re-locates that text in the target
  file's CURRENT content.

THE TRAP, and why Leg 2 is not a line-equality check: line numbers are
the least stable identifier in a repo. A peer commit that inserts one
line above a citation's target would make a naive `current_line ==
cited_line` check report every such citation stale — a false-positive
generator running constantly on a machine this active. The correct build
is CONTENT-ANCHORED: locate the anchor text in the target file's current
lines, and report the line it lives on NOW. A citation whose target moved
by N lines but is textually intact is `stale: False` with a `moved_to`
line, never a false positive (AC8).

HARD FACT, deliberately not reused backwards: `coordinator_core.ops.
doc_content_verify.Citation.line` is the line a citation APPEARS ON in
the CITING doc — never the line it POINTS AT. This module's own
`appears_at_line` field (Leg 2 evidence) mirrors that same "appears on"
meaning; `cited_line` is the DIFFERENT number the citation text itself
claims as its target's location. The two are never interchanged here.

ANCHORING LADDER (Leg 2, documented per the contract's instruction that
this row's fuzzy-match strategy is this module's call, not a decision the
contract makes for it) — three rungs, tried in order, first hit wins:
  1. exact-substring — the anchor text appears verbatim inside some
     target-file line.
  2. normalised-whitespace — the anchor text, with all whitespace
     collapsed, appears inside some target-file line under the same
     collapse. Catches a re-wrapped or re-indented line whose tokens are
     otherwise unchanged.
  3. enclosing-symbol match — when the anchor text is a bare identifier
     (`^[A-Za-z_][A-Za-z0-9_.]*$`), a `class <name>`/`def <name>` line, or
     a `<name>:` key line (YAML/JSON-shaped), anywhere in the target file.
     Catches a symbol whose surrounding line was reworded but whose own
     declaration/key line still names it.

RESOLUTION RULE for what "no anchor resolves" means (this module's own
reading of the contract's instruction to "emit undetermined ... rather
than guessing"): `undetermined` is reserved for citations this module
cannot even ATTEMPT to check — no plan body, no adjacent quoted anchor
text to compare, or a target path this repo root cannot resolve. Once
the ladder actually RUNS against a target file's current content, its
outcome is a determination, not a guess:
  - some rung matches at the CITED line      -> `stale: False`
  - some rung matches at a DIFFERENT line    -> `stale: False`,
                                                  `moved_to: <line>`
  - no rung matches anywhere in the file     -> `stale: True` (AC8's
                                                  "genuinely changed" case
                                                  — distinct from the
                                                  moved case above)
  - target file does not exist at all        -> `stale: True`

Known failure mode of this ladder (recorded, not hidden): a citation
whose target line was REWORDED (not moved, not deleted, but paraphrased)
while preserving no verbatim substring, no whitespace-normalised
substring, and no bare-identifier declaration line, reports `stale: True`
even though a human would call it "still basically the same claim,
worded differently". This module cannot tell a genuine content change
from a heavy paraphrase — that residual gap is why the ladder resolves to
a determination only after three independent rungs miss, not after one.

Negative-spec:
  - Does NOT treat `doc_content_verify.Citation`/`extract_citations` as
    this row's extraction mechanism. That module's citation shape is
    path-only (existence/broken-link checking); `:85-87` needs the
    `path:line` shape with a claimed target line, which
    `doc_content_verify` does not extract at all. This module owns its
    own small extractor instead of bending that one to a shape it was
    not built for.
  - Does NOT re-derive `_log_follow_records`'s `git log --follow` walk,
    its whitespace/link-only diff classification, or its sweep-drive-by
    filter — Leg 1 composes over the shipped reader for existence-of-
    history evidence only, it does not re-implement `doc_staleness.py`'s
    content-modifying-commit discrimination.
  - Does NOT guess a `stale` verdict for a citation whose anchor text
    could not be extracted from the surrounding prose at all — that is
    `undetermined`, never a coin-flip `False`.
  - Does NOT mutate any file, and does NOT shell out for Leg 2 — Leg 2 is
    a pure current-tree read (`Path.read_text`); only Leg 1 shells to git,
    via the composed `_log_follow_records`.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C11
"""
from __future__ import annotations

import re
from typing import Any, Optional

from coordinator_core.ops.doc_staleness import _log_follow_records as _doc_staleness_log_follow_records
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined


def _history_records(repo_root, rel_path):
    """Thin, deliberately-named wrapper over `doc_staleness._log_follow_records`.

    Review: code-reviewer flagged the direct cross-module import of an
    underscore-prefixed, non-`__all__` helper as undocumented-contract
    coupling — `doc_staleness.py` is free to rename or reshape that
    symbol without breaking its own public API. This wrapper does not
    solve that (it cannot, without `doc_staleness.py` promoting the
    symbol itself) but it localizes the coupling to one call site, so a
    future signature change surfaces here rather than at every call in
    `scope_paths_staleness`, and is the explicit acknowledgment the
    reviewer asked for that both sides know this is deliberate composed-
    over reuse (see module docstring, Leg 1), not an accident."""
    return _doc_staleness_log_follow_records(repo_root, rel_path)

#: `path:line` (or `path:line-line2`) citation tokens, backtick-quoted,
#: matching the shape this repo's own docs already use (e.g.
#: `` `plan.schema.json:43` ``). The path segment requires a dotted
#: extension so a bare contract-row reference like `` `:85-87` `` (no
#: path, just a colon-prefixed number) is never mistaken for a citation.
_CITATION_RE = re.compile(
    r"`([A-Za-z0-9_./\-]+\.[A-Za-z0-9_]+):(\d+)(?:-\d+)?`"
)

#: Any backtick-quoted span, used to locate a citation's adjacent anchor text.
_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")

#: How far (in characters) either side of a citation token this module
#: looks for an adjacent backtick-quoted anchor span.
_ANCHOR_SEARCH_WINDOW = 80

#: Anchor text shaped like a bare identifier is eligible for ladder rung 3
#: (enclosing-symbol match).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _extract_citations(plan_body: str) -> list[dict[str, Any]]:
    """Every `path:line` citation token in `plan_body`, each paired with
    its recovered anchor text (or `None` if no adjacent quoted span was
    found) and the line the citation token itself APPEARS ON in the plan
    body (never confused with the DIFFERENT `cited_line` the token claims
    as its target)."""
    citations: list[dict[str, Any]] = []
    for match in _CITATION_RE.finditer(plan_body):
        path_token, line_str = match.group(1), match.group(2)
        appears_at_line = plan_body.count("\n", 0, match.start()) + 1
        citations.append(
            {
                "path": path_token,
                "cited_line": int(line_str),
                "appears_at_line": appears_at_line,
                "anchor_text": _find_anchor_text(
                    plan_body, match.start(), match.end()
                ),
            }
        )
    return citations


def _find_anchor_text(text: str, start: int, end: int) -> Optional[str]:
    """The nearest backtick-quoted span adjacent to a citation token —
    preceding span preferred (the repo's own convention is `` `SYMBOL`
    at/in `path:line` ``), falling back to the next following span."""
    preceding = text[max(0, start - _ANCHOR_SEARCH_WINDOW) : start]
    preceding_spans = [
        m for m in _BACKTICK_SPAN_RE.finditer(preceding) if m.group(1).strip()
    ]
    if preceding_spans:
        return preceding_spans[-1].group(1)
    following = text[end : end + _ANCHOR_SEARCH_WINDOW]
    following_spans = [
        m for m in _BACKTICK_SPAN_RE.finditer(following) if m.group(1).strip()
    ]
    if following_spans:
        return following_spans[0].group(1)
    # Review: code-reviewer — a whitespace-only backtick span (` ` `) is
    # filtered out above rather than returned as anchor_text: it would
    # otherwise match almost every non-empty target line at rung 1
    # (`anchor_text in line`), producing a bogus non-undetermined match
    # instead of the `undetermined` this module's RESOLUTION RULE reserves
    # for "no adjacent quoted anchor text found".
    return None


def _anchor_line(target_lines: list[str], anchor_text: str) -> Optional[int]:
    """Run the anchoring ladder against `target_lines`; return the 1-based
    line the anchor resolves to, or `None` if all three rungs miss."""
    for idx, line in enumerate(target_lines):
        if anchor_text in line:
            return idx + 1

    normalized_anchor = re.sub(r"\s+", "", anchor_text)
    if normalized_anchor:
        for idx, line in enumerate(target_lines):
            if normalized_anchor in re.sub(r"\s+", "", line):
                return idx + 1

    if _IDENTIFIER_RE.match(anchor_text):
        declaration_re = re.compile(
            rf"^\s*(?:class|def)\s+{re.escape(anchor_text)}\b"
        )
        key_re = re.compile(rf'^\s*"?{re.escape(anchor_text)}"?\s*:')
        for idx, line in enumerate(target_lines):
            if declaration_re.match(line) or key_re.match(line):
                return idx + 1

    return None


def scope_paths_staleness(context: PredicateContext) -> dict[str, Any]:
    """`:85-87` Leg 1 -> `gates.substrate.staleness.scope_paths_stale`
    (bool), `.stale_paths` (list).

    For each `scope:` entry in the plan's frontmatter, a path present on
    disk is never stale. A missing path is reported stale, with evidence
    of whether `_log_follow_records` (composed, not re-derived) found any
    prior history for it — a path with no history at all is likely a
    typo rather than something that was deleted or renamed away, and the
    evidence says which."""
    if context.plan_frontmatter is None:
        return undetermined("no plan frontmatter available (--plan not supplied or unparseable)")

    scope = context.plan_frontmatter.get("scope")
    if not scope:
        return {"scope_paths_stale": False, "stale_paths": []}
    if isinstance(scope, str):
        scope = [scope]

    stale_paths: list[dict[str, Any]] = []
    for rel_path in scope:
        rel_path = str(rel_path)
        if (context.repo_root / rel_path).exists():
            continue
        records = _history_records(context.repo_root, rel_path)
        stale_paths.append(
            {
                "path": rel_path,
                "reason": (
                    "absent on disk but has prior git history "
                    "(deleted or renamed away)"
                    if records
                    else "absent on disk, no git history found for this path"
                ),
            }
        )

    return {"scope_paths_stale": bool(stale_paths), "stale_paths": stale_paths}


def cited_lines_staleness(context: PredicateContext) -> dict[str, Any]:
    """`:85-87` Leg 2 -> `gates.substrate.staleness.cited_lines_stale`
    (bool), `.stale_citations` (list) — the content-anchored re-read.

    See the module docstring's ANCHORING LADDER and RESOLUTION RULE for
    the full contract. One entry per citation found in the plan body,
    every entry carrying `path`/`cited_line`/`appears_at_line`; a
    citation whose anchor text could not be extracted from the
    surrounding prose carries `undetermined: True` instead of a `stale`
    verdict — this module never guesses one for it."""
    if context.plan_body is None:
        return undetermined("no plan body available (--plan not supplied or unparseable)")

    citations = _extract_citations(context.plan_body)
    if not citations:
        return {"cited_lines_stale": False, "stale_citations": []}

    stale_citations: list[dict[str, Any]] = []
    any_stale = False

    for cite in citations:
        entry: dict[str, Any] = {
            "path": cite["path"],
            "cited_line": cite["cited_line"],
            "appears_at_line": cite["appears_at_line"],
        }

        if cite["anchor_text"] is None:
            entry["undetermined"] = True
            entry["reason"] = "no adjacent quoted anchor text found near citation"
            stale_citations.append(entry)
            continue

        entry["anchor_text"] = cite["anchor_text"]
        target_path = context.repo_root / cite["path"]

        if not target_path.is_file():
            entry["stale"] = True
            entry["reason"] = "target file absent"
            any_stale = True
            stale_citations.append(entry)
            continue

        try:
            target_text = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            entry["undetermined"] = True
            entry["reason"] = "target file unreadable"
            stale_citations.append(entry)
            continue

        target_lines = target_text.splitlines()
        found_line = _anchor_line(target_lines, cite["anchor_text"])

        if found_line is None:
            entry["stale"] = True
            entry["reason"] = (
                "anchor text not found anywhere in target file "
                "(genuine content change)"
            )
            any_stale = True
        elif found_line != cite["cited_line"]:
            entry["stale"] = False
            entry["moved_to"] = found_line
        else:
            entry["stale"] = False

        stale_citations.append(entry)

    return {"cited_lines_stale": any_stale, "stale_citations": stale_citations}


__all__ = ["scope_paths_staleness", "cited_lines_staleness"]
