"""
coordinator_core.ops.verify_skill_anchor_links — plain module, no registered
op.

Purpose: checks that super-skill SKILL.md `§ <section>` anchor citations
resolve — **path-directed**. Each citation names its own target file:

    `coordinator/snippets/em-operating-doctrine.md` § How to Plan and Hand Off
    docs/wiki/scoped-safety-commits.md § Current Doctrine
    ~/.claude/CLAUDE.md § Engineering Defaults

For each citation the gate resolves the path THAT citation names, reads THAT
file's headings, and checks the cited section against THAT file's headings.
Invoked from `/update-docs` Phase 11h; on non-zero exit the caller surfaces to
the PM (does NOT auto-fix, does NOT halt `/update-docs`).

History: this gate previously resolved every citation against one hardcoded
file, `<plugin_root>/CLAUDE.md`. DoE retired that file on 2026-07-27 and
doctrine fanned out from a single file into a *set* of surfaces; the gate then
returned exit 2 ("could not check") on every run for a week while reading as a
mere gate failure. Path-directed resolution replaces single-file resolution;
the surface set is DoE-supplied DATA (see "The manifest" below), never a list
hardcoded here.

Spec backlink: coordinator/commands/update-docs.md § Phase 11h
Origin: verify-skill-anchor-links.sh (DoE b5a4192c, 2026-07-20)

Exit codes (parity-critical — the whole point of the 2026-07-27 incident):
  0 — CHECKED, no dead anchors (may still be nonzero OK/QUALIFIED/UNRESOLVED)
  1 — CHECKED, one or more DEAD anchors found
  2 — COULD NOT CHECK: the manifest is present but broken, or plugin_root
      itself is unresolvable or missing from disk

Result kinds:
  OK          — cited path resolved AND the section exists in that file
  QUALIFIED   — the citation names a surface this gate cannot resolve locally
                (`~/.claude/CLAUDE.md` and friends) and the citing line
                qualifies it as global; recorded, not checked
  DEAD        — cited path resolved to a real file, section NOT in it
  UNRESOLVED  — cited path resolved to nothing on disk

The manifest (OPTIONAL — its absence is NOT an error):
    <plugin_root>/doctrine-surfaces.json, overridable by
    COORDINATOR_DOCTRINE_MANIFEST. Shape v1:
      {"schema_version": 1,
       "surfaces": ["coordinator/snippets/em-operating-doctrine.md", ...],
       "aliases": {"~/.claude/CLAUDE.md": "global-doctrine/CLAUDE.md"}}
    Absent → path-directed mode with no alias map; alias-shaped citations stay
    QUALIFIED and the run still exits 0/1 normally. Present and valid → alias
    citations become genuinely checkable (OK/DEAD instead of QUALIFIED).
    `surfaces` is a disk-existence assertion ONLY — every listed path is
    checked to exist at manifest-load time (catching manifest drift
    promptly) but is never consulted again; it does not gate, restrict, or
    otherwise participate in citation resolution. Only `aliases` feeds
    resolution.

Negative-spec:
    - Headings are NEVER unioned across the surface set. A citation naming
      file A is checked against file A's headings alone. Under union
      resolution a citation naming A passes on a heading that exists only in
      B — partial success reading as health, which is the exact class of
      defect this rewrite exists to remove. Do not "simplify" the per-file
      section cache into one merged section list.
    - "Found nothing" (exit 0) and "looked at nothing" (exit 2) must never
      collapse into one code. Exit 2 means the gate could not perform the
      check; a clean check is exit 0. A future edit that returns 2 for an
      empty/absent optional input reintroduces the 2026-07-27 incident.
    - UNRESOLVED is NOT a dead anchor and must NOT set exit 1. Prose
      citations (`OVERVIEW.md § <cluster>`) and placeholders exist; they are
      counted and reported separately so they stay visible without being
      fatal.
    - The doctrine surface set is DoE-supplied data. Do NOT hardcode a file
      list here — a hardcoded list is the same defect as the hardcoded path
      with more entries and more ways to half-rot.
    - Does NOT walk the whole repo for SKILL.md files — the consumer list is
      a hardcoded allowlist (`_HARDCODED_CONSUMERS`); extend the list here
      (not via directory scan) when a new super-skill anchors into doctrine.
    - A missing consumer file is SKIPPED (stderr note), not an error —
      mirrors the bash oracle's `[ ! -f "$consumer" ]` early-continue. But if
      EVERY consumer is missing (the whole allowlist has rotted), that is
      COULD NOT CHECK (exit 2), not a clean 0 with zero coverage — a partial
      skip stays informational; a total wipeout is not "found nothing," it
      is "looked at nothing."
    - A `§` occurrence with no path-shaped citation immediately before it
      (format drift in the punctuation between path and `§`) is counted as
      `dropped_section_lines` — a diagnostic-only visibility signal (stderr
      note + summary field), never a factor in the exit code.
    - `§` occurrences are matched by longest-valid-heading-as-prefix, sorted
      section-list longest-first — reproduces the bash oracle's Python-inline
      `resolve_anchors` matching exactly (not reimplemented differently).
    - Only lines carrying a `<something>.md §` citation are scanned; a line
      with a bare `§` and no `.md` path before it is never inspected. A `§`
      with no path in front of it on a line that has an earlier citation
      carries that citation's path forward (`… § A … and § B` cites one file
      twice) — it is NOT re-resolved against a different surface. A CARRIED
      citation is a weaker signal than a path-naming one: DoE's prose puts
      the file after the section as often as before it (`§ Operating
      Assumptions (global ~/.claude/CLAUDE.md)`), so a carried citation that
      misses on a global-qualified line is QUALIFIED, not DEAD. A citation
      that names its own path and misses is always DEAD.
    - `(formerly § Old Heading)` is a rename-history annotation, NOT a live
      anchor — skipped, counted as `historical` in the summary. Checking it
      would report DEAD for the rename the sentence exists to document, and
      DoE's skills carry one of these beside most live citations; a gate
      wrong on most of its findings gets ignored wholesale.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, NamedTuple, Optional, Tuple

from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root


_HARDCODED_CONSUMERS = (
    "skills/plan/SKILL.md",
    "skills/review/SKILL.md",
    "skills/review-code/SKILL.md",
)

_MANIFEST_BASENAME = "doctrine-surfaces.json"
_MANIFEST_SCHEMA_VERSION = 1

#: A citation line carries `<path>.md` (optionally closed by a quote/backtick)
#: immediately before a `§`. `§§ A / B` is one citation with two sections, so
#: a run of markers is tolerated between the path and the section text.
_CITATION_LINE_RE = re.compile(r"\.md[`'\"]?[\s§]*§")

#: The cited path, anchored to the END of the text preceding a `§`.
_CITED_PATH_RE = re.compile(r"([~A-Za-z0-9_.][^\s`'\"()\[\]<>]*\.md)[`'\"]?[\s§]*$")

#: `(formerly § Old Heading…)` — a rename-history annotation, not a live
#: anchor. The old heading is expected to be gone; checking it would report a
#: DEAD anchor for text that documents the very rename that killed it.
_HISTORICAL_RE = re.compile(r"formerly[\s§]*$", re.IGNORECASE)


class AnchorResult(NamedTuple):
    kind: str  # "OK" | "QUALIFIED" | "DEAD" | "UNRESOLVED"
    consumer_rel: str
    line_no: int
    value: str  # matched heading (OK), or raw cited snippet
    cited_path: str


class ScanReport(NamedTuple):
    results: List[AnchorResult]
    skipped: List[str]
    error: str  # non-empty ⇒ COULD NOT CHECK ⇒ exit 2
    notes: List[str]  # informational stderr lines
    historical: int = 0  # `(formerly § …)` markers deliberately not checked
    dropped_section_lines: int = 0  # `§` present but no path-shaped citation
    # matched `_CITATION_LINE_RE` — a coarse format-drift visibility signal,
    # diagnostic only (never affects exit code). Distinct from a line with no
    # `§` at all, which is not a citation and is correctly never counted.


class Manifest(NamedTuple):
    path: str
    aliases: Dict[str, str]  # citation text → absolute resolved path


class ManifestError(Exception):
    """Manifest present but unusable — always exit 2, never a silent skip."""


def _plugin_root() -> str:
    """Resolve the plugin root (coordinator/) that owns the SKILL.md consumers.

    Env var CLAUDE_PLUGIN_ROOT wins if set, returned verbatim. Otherwise
    resolves via `coordinator_doe_root()` (see that module's own docstring
    for its env-var/machine-local resolution chain) and returns
    <doe_root>/coordinator.

    This does NOT derive from this module's own __file__ location. This
    module migrated from DoE-claude to claude-klabauter (DOE-PORT R2-R6,
    commit b644d5a9 there / 8a28a6ca here) while coordinator/skills/ stayed
    in DoE-claude — self-location now resolves to
    <claude-klabauter>/coordinator_core/ops/, a directory with no skills/ at all,
    which previously produced a false "CLAUDE.md not found" error instead of
    a loud, correctly-diagnosed resolution failure. `coordinator_doe_root()`
    is the correct authority for "where is the DoE-claude repo," independent
    of where THIS module happens to run from. A future reader must not
    "restore" __file__-based resolution to regain the old bash-oracle-adjacent
    shape — that is precisely what caused this break (see
    docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md,
    "skill-anchor-links" chunk).

    Fails loud with exit 2 (COULD NOT CHECK) if coordinator_doe_root() cannot
    resolve: this is a gate invoked from `/update-docs`, not a never-block
    hook, and an unresolvable DoE root means the gate examined nothing — which
    must never be reported with the same code as a clean run.
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return env
    root = coordinator_doe_root()
    if root is None:
        print(
            "verify_skill_anchor_links: cannot resolve the coordinator root — "
            "coordinator_doe_root() returned no result. Set repos.doe_claude in the "
            "machine-local registry, or set the DOE_ROOT/REPO_DOE_CLAUDE env var, or "
            "set CLAUDE_PLUGIN_ROOT directly. Nothing was checked.",
            file=sys.stderr,
        )
        sys.exit(2)
    return os.path.join(root, "coordinator")


def _extract_valid_sections(text: str) -> List[str]:
    sections: List[str] = []
    for line in text.splitlines():
        if line.startswith("### "):
            sections.append(line[len("### "):])
        elif line.startswith("## "):
            sections.append(line[len("## "):])
    return sections


def _is_qualified_global(line: str) -> bool:
    if "~/.claude/CLAUDE.md" in line:
        return True
    if re.search(r"global .*CLAUDE\.md", line):
        return True
    if "(global" in line:
        return True
    return False


def _candidate_roots(plugin_root: str, citing_file: Optional[str]) -> List[str]:
    """Resolution roots, in priority order, for a cited relative path.

    repo_root first: DoE citations name repo-relative paths
    (`coordinator/snippets/...`), which would otherwise collide with
    plugin-relative ones (`skills/plan/SKILL.md`).
    """
    roots = [os.path.dirname(plugin_root.rstrip(os.sep)), plugin_root]
    if citing_file:
        roots.append(os.path.dirname(citing_file))
    return [r for r in roots if r]


def _resolve_cited_path(
    cited: str,
    plugin_root: str,
    citing_file: Optional[str],
    manifest: Optional[Manifest],
) -> Optional[str]:
    if manifest is not None:
        aliased = manifest.aliases.get(cited)
        if aliased is not None:
            return aliased if os.path.isfile(aliased) else None
    if cited.startswith("~"):
        return None
    if os.path.isabs(cited):
        return cited if os.path.isfile(cited) else None
    # Review: code-reviewer — `cited` is trusted-content-derived (DoE's own
    # first-party doctrine prose, not adversarial input) and is joined
    # against `_candidate_roots` with no post-join containment check. No
    # guard is added here deliberately; this comment records that the trust
    # boundary is assumed, not enforced, so a future reader doesn't infer a
    # containment check exists.
    for root in _candidate_roots(plugin_root, citing_file):
        candidate = os.path.normpath(os.path.join(root, cited))
        if os.path.isfile(candidate):
            return candidate
    return None


def _cited_path_before(line: str, pos: int) -> Optional[str]:
    m = _CITED_PATH_RE.search(line[:pos])
    return m.group(1) if m else None


def _snippet_after(line: str, end: int) -> str:
    """The trimmed raw-text snippet following a `§` occurrence at `end`.

    Shared by the true MISS path in `_resolve_anchors` and by callers that
    need only the snippet (no section list to match against, e.g. an
    unresolved cited path) — rather than each reimplementing the
    candidate-zone/trim logic, or a caller faking it via
    `_resolve_anchors(line, [], ...)` with a sentinel empty section list.
    """
    tail = line[end:]
    hard_end = len(tail)
    for term in ("_", "\n"):
        i = tail.find(term)
        if i != -1 and i < hard_end:
            hard_end = i
    candidate_zone = tail[:hard_end]
    snippet = candidate_zone.strip()[:60]
    return re.sub(r"[.,;:\s]+$", "", snippet)


def _resolve_anchors(line: str, sections: List[str], start: int, end: int) -> Tuple[str, str]:
    """Match one `§ ` occurrence against `sections`.

    Returns (MATCH, heading) or (MISS, snippet). Longest-valid-heading-as-prefix
    over a longest-first section list — the bash oracle's `resolve_anchors`
    semantics, preserved verbatim in behavior.
    """
    tail = line[end:]
    hard_end = len(tail)
    for term in ("_", "\n"):
        i = tail.find(term)
        if i != -1 and i < hard_end:
            hard_end = i
    candidate_zone = tail[:hard_end]
    for sec in sorted(sections, key=len, reverse=True):
        if candidate_zone.startswith(sec):
            nxt = candidate_zone[len(sec):len(sec) + 1]
            if nxt == "" or not nxt.isalnum():
                return ("MATCH", sec)
    return ("MISS", _snippet_after(line, end))


def manifest_path(plugin_root: str) -> str:
    env = os.environ.get("COORDINATOR_DOCTRINE_MANIFEST")
    if env:
        return env
    return os.path.join(plugin_root, _MANIFEST_BASENAME)


def load_manifest(plugin_root: str) -> Optional[Manifest]:
    """Load the optional doctrine-surface manifest.

    Returns None when the manifest is absent — that is the expected steady
    state, not an error. Raises ManifestError (⇒ exit 2) when a manifest IS
    present but unusable, naming which of the failure modes it hit.
    """
    path = manifest_path(plugin_root)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError(
            f"doctrine manifest at {path} is unparseable JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ManifestError(
            f"doctrine manifest at {path} is malformed: top level must be an object, "
            f"got {type(raw).__name__}"
        )

    version = raw.get("schema_version")
    if version != _MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"doctrine manifest at {path} has unknown schema_version {version!r} — "
            f"this gate understands schema_version {_MANIFEST_SCHEMA_VERSION}"
        )

    surfaces = raw.get("surfaces", [])
    aliases = raw.get("aliases", {})
    if not isinstance(surfaces, list) or not all(isinstance(s, str) for s in surfaces):
        raise ManifestError(
            f"doctrine manifest at {path} is malformed: 'surfaces' must be a list of strings"
        )
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()
    ):
        raise ManifestError(
            f"doctrine manifest at {path} is malformed: 'aliases' must be an object of "
            f"string→string"
        )

    def _locate(rel: str) -> Optional[str]:
        if os.path.isabs(rel):
            return rel if os.path.isfile(rel) else None
        for root in _candidate_roots(plugin_root, None):
            candidate = os.path.normpath(os.path.join(root, rel))
            if os.path.isfile(candidate):
                return candidate
        return None

    # Review: code-reviewer — `surfaces` is a disk-existence assertion only
    # (catches manifest drift promptly), never a resolution input: generic
    # citation paths already resolve via `_candidate_roots` independent of
    # whether they're declared here. A future reader should not assume
    # membership in `surfaces` gates or restricts what this gate checks.
    resolved_aliases: Dict[str, str] = {}
    for entry in surfaces:
        if _locate(entry) is None:
            raise ManifestError(
                f"doctrine manifest at {path} names a surfaces entry missing from disk: "
                f"{entry}"
            )
    for alias, target in aliases.items():
        located = _locate(target)
        if located is None:
            raise ManifestError(
                f"doctrine manifest at {path} names alias {alias!r} → {target}, "
                f"which is missing from disk"
            )
        resolved_aliases[alias] = located

    return Manifest(path=path, aliases=resolved_aliases)


def scan(plugin_root: str, consumers: Optional[List[str]] = None) -> ScanReport:
    """Run the path-directed anchor-link verification.

    `error` is non-empty iff the gate COULD NOT CHECK (caller maps that to
    exit 2): plugin_root missing, or a present-but-broken manifest.
    """
    notes: List[str] = []

    if not os.path.isdir(plugin_root):
        return ScanReport([], [], f"ERROR: plugin root not found at {plugin_root}", notes)

    try:
        manifest = load_manifest(plugin_root)
    except ManifestError as exc:
        return ScanReport([], [], f"ERROR: {exc}", notes)

    if manifest is None:
        notes.append(
            f"note: no doctrine-surface manifest at {manifest_path(plugin_root)} — "
            "running in path-directed mode; alias citations (e.g. ~/.claude/CLAUDE.md) "
            "are recorded QUALIFIED rather than checked. This is informational, not an "
            "error."
        )

    consumer_list = consumers if consumers is not None else [
        os.path.join(plugin_root, rel) for rel in _HARDCODED_CONSUMERS
    ]

    results: List[AnchorResult] = []
    skipped: List[str] = []
    historical = 0
    dropped_section_lines = 0
    section_cache: Dict[str, List[str]] = {}

    def _sections_of(path: str) -> List[str]:
        if path not in section_cache:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                section_cache[path] = _extract_valid_sections(f.read())
        return section_cache[path]

    for consumer in consumer_list:
        if not os.path.isfile(consumer):
            skipped.append(consumer)
            continue

        rel = (
            consumer[len(plugin_root) + 1:]
            if consumer.startswith(plugin_root + os.sep)
            else consumer
        )

        with open(consumer, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.rstrip("\n")
                if not _CITATION_LINE_RE.search(line):
                    # Review: code-reviewer — a bare `§` with no path-shaped
                    # citation immediately before it (format drift, e.g. a
                    # stray comma between path and `§`) is otherwise silently
                    # never scanned. Count it as a coarse visibility signal,
                    # distinct from a line with no `§` at all.
                    if "§" in line:
                        dropped_section_lines += 1
                    continue

                carried: Optional[str] = None
                prev_end = 0
                for m in re.finditer(r"§\s+", line):
                    window = line[prev_end:m.start()]
                    prev_end = m.end()
                    if _HISTORICAL_RE.search(window):
                        historical += 1
                        continue

                    explicit = _cited_path_before(line, m.start())
                    cited = explicit or carried
                    if cited is None:
                        continue
                    carried = cited

                    target = _resolve_cited_path(cited, plugin_root, consumer, manifest)
                    if target is None:
                        kind = "QUALIFIED" if _is_qualified_global(line) else "UNRESOLVED"
                        snippet = _snippet_after(line, m.end())
                        results.append(AnchorResult(kind, rel, line_no, snippet, cited))
                        continue

                    verdict, value = _resolve_anchors(
                        line, _sections_of(target), m.start(), m.end()
                    )
                    if verdict == "MATCH":
                        kind = "OK"
                    elif explicit is None and _is_qualified_global(line):
                        kind = "QUALIFIED"
                    else:
                        kind = "DEAD"
                    results.append(AnchorResult(kind, rel, line_no, value, cited))

    # Review: code-reviewer — every consumer vanishing (all skipped, none
    # read) must COULD-NOT-CHECK, not a clean 0. Without this, "found
    # nothing" (exit 0, zero coverage) and "looked at nothing" collapse into
    # the same code — exactly the defect class this rewrite exists to close.
    if consumer_list and len(skipped) == len(consumer_list):
        joined = ", ".join(skipped)
        return ScanReport(
            [],
            skipped,
            f"ERROR: no consumer file could be read — all {len(consumer_list)} "
            f"consumer(s) missing from disk: {joined}",
            notes,
        )

    return ScanReport(results, skipped, "", notes, historical, dropped_section_lines)


def main(argv: List[str]) -> int:
    """CLI entry: --list mode or verify mode. Mirrors the bash oracle's MODE dispatch."""
    plugin_root = _plugin_root()
    mode = argv[0] if argv else "verify"

    if mode == "--list":
        for rel in _HARDCODED_CONSUMERS:
            print(os.path.join(plugin_root, rel))
        return 0

    report = scan(plugin_root)
    if report.error:
        print(report.error, file=sys.stderr)
        return 2

    for note in report.notes:
        print(note, file=sys.stderr)
    for consumer in report.skipped:
        print(f"SKIPPED (not found): {consumer}", file=sys.stderr)
    if report.dropped_section_lines:
        print(
            f"note: {report.dropped_section_lines} line(s) contained a bare "
            "'§' with no path-shaped citation immediately before it — "
            "possible format drift, not counted as UNRESOLVED/DEAD. "
            "Diagnostic only; does not affect exit code.",
            file=sys.stderr,
        )

    counts = {"OK": 0, "QUALIFIED": 0, "DEAD": 0, "UNRESOLVED": 0}
    exit_code = 0

    for r in report.results:
        counts[r.kind] += 1
        if r.kind == "DEAD":
            exit_code = 1
        print(f"{r.kind:<10} {r.consumer_rel}:{r.line_no}  {r.cited_path} § {r.value}")

    print()
    print(
        f"summary: total={len(report.results)} ok={counts['OK']} "
        f"qualified={counts['QUALIFIED']} dead={counts['DEAD']} "
        f"unresolved={counts['UNRESOLVED']} historical={report.historical} "
        f"dropped_section_lines={report.dropped_section_lines}"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
