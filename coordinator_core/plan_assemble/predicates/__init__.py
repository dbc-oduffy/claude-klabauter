from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter, parse_yaml


def undetermined(reason: str) -> dict[str, Any]:
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
