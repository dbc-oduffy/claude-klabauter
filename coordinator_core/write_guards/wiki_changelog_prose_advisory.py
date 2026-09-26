"""coordinator_core.write_guards.wiki_changelog_prose_advisory -- advisory guard.

New module (no `.sh` reference; this repo's guards are otherwise ports of
retired DoE bash hooks, but this one has no such ancestor -- see
INTERFACE.md's provenance rule, which this docstring satisfies with a spec
backlink instead of a `.sh` path).

Spec backlink: docs/plans/2026-09-07-doctrine-enforcement-surfaces.md, row
C5 ("Leg (b) write-time advisory guard for docs/wiki/ changelog prose, only
if C2 reads go"). Armed only because C2's spike
(docs/research/spike-verdicts/2026-09-10-doctrine-enforcement-surfaces-c5-perf-windows-spike.md)
returned verdict: viable/go against DR-344's 200ms-process-time bar.

Purpose: DoE-claude's own doctrine (`CLAUDE.md` § Conventions, "Doctrine is
not changelog") states the rule this guard advises on: a wiki page states
the rule as it stands, present tense -- no ruling dates, no `DR-` supersession
chains, no "was P, now Q" phrasing. That rule currently has no artifact that
discharges it at write time; an author drifts a wiki page into changelog
shape and nothing short of a human re-read catches it. This guard is that
artifact for the narrow, mechanically-detectable slice of the rule (dated
entries, `DR-NNN` references, "was X, now Y" phrasing) -- advisory only, it
never blocks the write, matching the doctrine's own stated exemption
(a retirement whose *absence* is the operative rule is deliberately exempted
in prose; this guard cannot tell that case from ordinary drift, hence
advisory, never hard-deny).

Scope-root discrimination (eng-director F3, BLOCKING; reverses this row's
prior anti-scope clause, now struck): this module does NOT string-match a
bare `docs/wiki/` path segment. `coordinator_core.write_guards.engine` is a
single shared engine evaluating writes for every repo it serves via
klabauter -- a bare path-segment match would arm this guard fleet-wide by
default, in every repo with a `docs/wiki/` directory, not just the one this
row's doctrine text is about. Instead this module resolves the write's
OWN enclosing repo root (`coordinator_core.write_guards._repo_root`, the
existing zero-spawn shared resolver already used by ~10 sibling guards) and
scopes to `<repo_root>/docs/wiki/` -- the same in-tree repo-discrimination
shape `block_dev_side_mirror_wiki.py` uses for its own `docs/wiki` match
(there: a resolved plugin root; here: a resolved repo root). An unresolvable
repo root fails open (no scope, no advisory) rather than falling back to a
bare-segment match.

Delta-scoped (per C2's spike, § (ii)): reads `tool_input["content"]` (Write),
`tool_input["new_string"]` (Edit), or the concatenation of each
`edits[].new_string` (MultiEdit) directly off the payload -- never the
reconstructed whole file `nudge_windows_subprocess_popup.py`'s machinery
builds for its own, unrelated purpose. C2 names the tradeoff this buys:
losing whole-file CONTEXT is the same class of blind spot DR-077 closed for
a different guard by moving fragment-scoped detection to whole-file: a
`DR-NNN` reference or a "was X now Y" phrase split across an edit boundary
(old_string ends mid-sentence, the rest is unchanged surrounding prose) is
invisible to a delta-only scan. This guard accepts that tradeoff for the
cheaper scoping C2 measured (~2.6-6ms, well under the 200ms bar) -- a
whole-file rescan of every docs/wiki/ edit was not itself measured here and
is out of this row's scope.

Named exemption mechanism (staff-eng F7; Anti-scope) -- PATH CONVENTION,
named at build time: a wiki page whose subject matter IS a changelog by
design is exempt when its filename (final path segment) contains the
substring "changelog" (case-insensitive) -- e.g. `2026-09-changelog.md`,
`CHANGELOG.md`, `release-changelog-notes.md`. This is the exemption
mechanism this guard implements; it does not also implement an
allowlist file or a frontmatter field -- the row's body names three
candidate mechanisms and requires exactly one, named at build time (this
module).

Detectors (stdlib `re` only, per AC8 -- no third-party regex/AST/NLP dep):
  1. An ISO-shaped date, `\\b\\d{4}-\\d{2}-\\d{2}\\b` -- a "ruling date".
  2. A `DR-NNN` reference, `\\bDR-\\d+\\b` -- a supersession-chain citation.
  3. "was ... now ..." phrasing (case-insensitive, same-line-ish window,
     bounded to <= 60 chars between the two words so an unrelated "was" and
     "now" elsewhere in a long paragraph don't false-positive) --
     `\\bwas\\b.{0,60}?\\bnow\\b`, re.IGNORECASE | re.DOTALL is NOT set (the
     window stays single-line: `.` does not match `\\n` by default).
Any one match on the delta text fires the advisory (first match wins for the
message; the advisory names which detector fired, not every match).

Negative-spec:
  - Does NOT hard-deny -- CLASS = "advisory" throughout; the write always
    lands (INTERFACE.md envelope: `additionalContext` only, no
    `permissionDecision`).
  - Does NOT scan the whole file -- delta-scoped only (see above); a
    changelog-shaped sentence already present in the file and untouched by
    this write is not re-flagged.
  - Does NOT apply outside `<repo_root>/docs/wiki/` -- a matching phrase in
    a plan, a handoff, a lesson, or any non-wiki doc never fires this guard.
  - Does NOT raise `module_count_ceiling` for `write_guards.engine`
    (Anti-scope) -- this module is discovered via the engine's existing
    two-stage lazy AST scan (`_cheap_guard_metadata`) with CLASS/MATCHERS/
    PRIORITY as plain top-level literals; its own body imports nothing
    beyond stdlib `re` (already imported process-wide), so it is never even
    an eager import cost for a call that never reaches this guard's
    `check()`.
  - Does NOT read stdin, does NOT reconstruct the on-disk file, does NOT
    spawn a subprocess (repo-root resolution is the existing zero-spawn
    walk-only delegate).
  - Never raises: any unexpected input shape or internal error is treated
    as ALLOW/no-op (fail-open on error), matching this package's advisory
    convention.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 222

_EXEMPT_FILENAME_SUBSTRING = "changelog"

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DR_REF_RE = re.compile(r"\bDR-\d+\b")
_WAS_NOW_RE = re.compile(r"\bwas\b.{0,60}?\bnow\b", re.IGNORECASE)

_DETECTORS = (
    (_DATE_RE, "a ruling date (YYYY-MM-DD)"),
    (_DR_REF_RE, "a DR-NNN supersession reference"),
    (_WAS_NOW_RE, "'was ... now ...' phrasing"),
)


def _delta_text(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else ""
    if tool_name == "Edit":
        new_string = tool_input.get("new_string")
        return new_string if isinstance(new_string, str) else ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return ""
        parts: List[str] = []
        for edit in edits:
            if isinstance(edit, dict):
                new_string = edit.get("new_string")
                if isinstance(new_string, str):
                    parts.append(new_string)
        return "\n".join(parts)
    return ""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = tool_input.get("file_path") or ""
        if not isinstance(file_path, str) or not file_path:
            return None

        filename = file_path.replace("\\", "/").rsplit("/", 1)[-1]
        if _EXEMPT_FILENAME_SUBSTRING in filename.lower():
            return None

        repo_root = resolve_repo_root(payload.get("cwd"))
        if not repo_root:
            return None

        # separator swap left this comparison case-SENSITIVE, so on the
        scope_root = casefold_path(repo_root).rstrip("/") + "/docs/wiki/"
        file_path_norm = casefold_path(file_path)
        if not file_path_norm.startswith(scope_root):
            return None

        tool_name = payload.get("tool_name") or ""
        delta = _delta_text(tool_name, tool_input)
        if not delta:
            return None

        for pattern, label in _DETECTORS:
            if pattern.search(delta):
                reason = (
                    f"{filename} reads changelog-shaped ({label}), not "
                    "doctrine-shaped.\n"
                    "Use instead:\n"
                    f"  if a changelog by design, name it so (contains "
                    f"{_EXEMPT_FILENAME_SUBSTRING!r})"
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": reason,
                    }
                }

        return None
    except Exception:
        return None
