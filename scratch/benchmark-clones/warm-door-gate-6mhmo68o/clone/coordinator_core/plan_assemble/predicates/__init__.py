"""
coordinator_core.plan_assemble.predicates — the shared seam every Layer 0 /
Layer 1 / Layer 2 predicate producer imports: `PredicateContext` (the one
disk-reading entry point) and `undetermined(...)` (the one legal "cannot
compute" sentinel).

Purpose: the `plan-assemble` contract's 60 rows fan out across ten sibling
modules (triage, substrate_seven_dim, substrate_scans, composition_lints,
composition_graph, supersedes_index, exit_gates, plus Layer 1/2 composers).
Every one of them needs the SAME facts read off disk ONCE — the plan's own
frontmatter/body, the sizing object's frontmatter, the resolved route, and a
handful of one-bit caller signals the contract names by row number
(`:32`'s arrival split, `:100`'s DEC-4 `trampoline: true`, `:108`'s pass-state
bit). This module reads all of that exactly once via `PredicateContext.
from_paths(...)`; every leaf reader downstream takes a constructed context
and touches disk again ONLY where its own contract row explicitly names a
grep or a git call (e.g. `:89`'s peer-SHA lint, `:116`'s symbol-liveness
grep) — never to re-read the plan or sizing object it already has in hand.

`undetermined(reason)` is the ONLY legal way a row reports "cannot compute".
Per the plan's Executor hard constraints: never `False`, never absent, never
`None`, never a silent `try/except: pass`. Every row a chunk cannot resolve
(because an input this context needed — `--plan`, `--sizing-object`, a
`caller_flags` bit — was not supplied) emits this sentinel, carrying a
`reason` string a human or a downstream consumer can read.

Negative-spec:
  - Does NOT decide any `U`-classified (untrusted-gate) row. This module has
    no opinion on route resolution, disposition, or verdict fields — it is
    purely a disk-reading and sentinel-shaping seam. Judgment stays with the
    leaf readers' own contract rows (and, per AC4, never crosses into the
    engine at all for a `U`-classified row).
  - Does NOT cache or memoize `from_paths(...)` results across calls — one
    construction per CLI invocation, no process-lifetime state.
  - Does NOT validate `plan_frontmatter`/`sizing_frontmatter` against their
    JSON schemas. Reading is this module's job; schema conformance is
    `frontmatter.schema_validate`'s.
  - Does NOT infer `caller_flags` values from context (e.g. it never guesses
    `trampoline: true` from the plan body's prose). An absent flag is an
    absent flag; the row that reads it emits `undetermined`.
  - Does NOT touch `residue.py` — the envelope wiring that assembles this
    package's outputs into `gates.*` is chunk C13's exclusive write target.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter, parse_yaml


def undetermined(reason: str) -> dict[str, Any]:
    """The ONE legal "cannot compute" sentinel for a predicate row.

    Shape: `{"undetermined": True, "reason": "<why>"}`. Every predicate
    producer in this package emits this — never `False`, never an absent
    key, never `None` — when a row's required input (a missing `--plan`,
    a missing `--sizing-object`, an absent `caller_flags` bit, an
    unreadable artifact) is not available. `reason` is a short, specific,
    human-readable string naming which input was missing — not a stack
    trace, not an exception repr.
    """
    return {"undetermined": True, "reason": reason}


@dataclass(frozen=True)
class PredicateContext:
    """Everything the Layer 0 leaf readers need off disk, read exactly
    once via `from_paths(...)`.

    Fields:
      repo_root            — the repo root every relative grep/`ls` in a
                              leaf reader resolves against.
      plan_path             — the plan markdown file, or `None` if
                              `--plan` was not supplied.
      plan_frontmatter      — the plan's parsed YAML frontmatter (`dict`),
                              or `None` if `plan_path` is `None` or the
                              file carried no parseable frontmatter.
      plan_body              — the plan's body text (frontmatter stripped),
                              or `None` under the same conditions as
                              `plan_frontmatter`.
      sizing_object_path    — the sizing object file, or `None` if
                              `--sizing-object` was not supplied.
      sizing_frontmatter    — the sizing object's parsed top-level YAML
                              (a sizing object is a bare `.yaml` file, not a
                              markdown-with-frontmatter document — this
                              field is the whole parsed document, read via
                              `parse_yaml`, not `parse_frontmatter`), or
                              `None` under the same absence rule.
      resolved_route        — the route string as `plan-assemble` already
                              resolved it (mirrors `residue.py`'s
                              `resolved_route` — this context SURFACES that
                              value, it never re-derives it).
      caller_flags          — the one-bit signals the contract names by row
                              number (`:32`'s arrival split, `:100`'s
                              DEC-4 `trampoline: true`, `:108`'s pass-state
                              bit, and any future addition of the same
                              shape). A key absent from this dict means the
                              caller did not supply that signal; the row
                              that reads it emits `undetermined` — this
                              context never backfills a default.

    `plan_path`/`plan_frontmatter`/`plan_body` travel together: either all
    three are populated (a plan was supplied and parsed) or the latter two
    are both `None` (no plan, or an unparseable one) while `plan_path`
    tracks whatever path was supplied (or `None`). Same triad rule for
    `sizing_object_path`/`sizing_frontmatter` (a two-member pair — sizing
    objects have no body relevant to the contract's rows).
    """

    repo_root: Path
    plan_path: Optional[Path]
    plan_frontmatter: Optional[dict[str, Any]]
    plan_body: Optional[str]
    sizing_object_path: Optional[Path]
    sizing_frontmatter: Optional[dict[str, Any]]
    resolved_route: str
    caller_flags: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_paths(
        cls,
        *,
        repo_root: Path,
        plan_path: Optional[Path],
        sizing_object_path: Optional[Path],
        resolved_route: str,
        caller_flags: Optional[dict[str, Any]] = None,
    ) -> "PredicateContext":
        """Construct a `PredicateContext`, reading `plan_path` and
        `sizing_object_path` off disk if given.

        Read failures do not raise: an unreadable or absent-frontmatter
        plan/sizing-object resolves its frontmatter/body field(s) to `None`
        exactly as if the path had not been supplied at all — the row-level
        `undetermined` sentinel is the mechanism that surfaces this to a
        caller, not an exception out of this classmethod. A genuinely
        unresolvable explicit path is a USAGE error the CLI layer (`plan_
        assemble/__init__.py`) raises BEFORE calling this classmethod; by
        the time `from_paths` runs, `plan_path`/`sizing_object_path` are
        either `None` or known to exist on disk.
        """
        plan_frontmatter: Optional[dict[str, Any]] = None
        plan_body: Optional[str] = None
        if plan_path is not None:
            try:
                text = plan_path.read_text(encoding="utf-8")
            except OSError:
                text = None
            if text is not None:
                parsed = parse_frontmatter(text)
                plan_frontmatter = parsed["frontmatter"]
                plan_body = parsed["body"] if plan_frontmatter is not None else None

        sizing_frontmatter: Optional[dict[str, Any]] = None
        if sizing_object_path is not None:
            try:
                sizing_text = sizing_object_path.read_text(encoding="utf-8")
            except OSError:
                sizing_text = None
            if sizing_text is not None:
                try:
                    parsed_yaml = parse_yaml(sizing_text)
                except Exception:
                    parsed_yaml = None
                if isinstance(parsed_yaml, dict) and parsed_yaml:
                    sizing_frontmatter = parsed_yaml

        return cls(
            repo_root=repo_root,
            plan_path=plan_path,
            plan_frontmatter=plan_frontmatter,
            plan_body=plan_body,
            sizing_object_path=sizing_object_path,
            sizing_frontmatter=sizing_frontmatter,
            resolved_route=resolved_route,
            caller_flags=dict(caller_flags) if caller_flags else {},
        )


__all__ = ["PredicateContext", "undetermined"]
