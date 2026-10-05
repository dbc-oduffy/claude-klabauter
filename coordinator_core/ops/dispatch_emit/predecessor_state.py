"""Live predecessor-handoff state for dispatched row prompts.

A plan's rows are frozen at plan time; the handoff is not. The block built here
carries facts learned after the plan was written (committed WIP for a chunk)
into every executor prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit.spine_read import load_frontmatter_doc
from coordinator_core.session.record_homes import home_dir

PREDECESSOR_HEADING = "## Predecessor handoff state"


def _frontmatter_of_text(text: str) -> dict:
    """Frontmatter dict, or ``{}`` on anything unparsable."""
    split = split_frontmatter(text)
    if split is None:
        return {}
    try:
        data = load_frontmatter_doc(split.fm_text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _frontmatter(path: Path) -> dict:
    try:
        return _frontmatter_of_text(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _current_state_section(text: str) -> Optional[str]:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "## Current State"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip()


def resolve_predecessor_state(plan_text: str, repo_root: Path) -> str:
    """Body of the ``## Predecessor handoff state`` section, built once per emit.

    Reads live ``state/handoffs/*.md`` only (never archive): the plan's
    ``predecessor_handoff`` plus, for a truthy ``deliverable_id``, every handoff
    whose frontmatter ``deliverable_id`` equals it. A falsy ``deliverable_id``
    skips the corpus scan -- an empty prefilter would match every handoff.
    """
    root = repo_root.resolve()
    handoffs_dir = Path(home_dir(str(root), "handoffs")).resolve()
    front = _frontmatter_of_text(plan_text)
    deliverable_id = front.get("deliverable_id")
    if not (isinstance(deliverable_id, str) and deliverable_id.strip()):
        deliverable_id = None
    declared = front.get("predecessor_handoff")
    declared = declared if isinstance(declared, str) and declared.strip() else None

    found: dict[Path, None] = {}
    declared_not_live = False
    if declared:
        cand = Path(declared)
        cand = (cand if cand.is_absolute() else root / cand).resolve()
        if cand.is_file() and cand.parent == handoffs_dir:
            found[cand] = None
        else:
            declared_not_live = True
    if deliverable_id and handoffs_dir.is_dir():
        for path in sorted(handoffs_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if deliverable_id in text and _frontmatter(path).get("deliverable_id") == deliverable_id:
                found[path.resolve()] = None

    parts: list[str] = []
    for path in sorted(found):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        section = _current_state_section(path.read_text(encoding="utf-8"))
        if section is None:
            parts.append(f"### `{rel}`\n\nThis handoff has no `## Current State` section.")
        else:
            parts.append(f"### `{rel}`\n\n{section}")
    if declared_not_live:
        parts.append(
            f"The declared predecessor handoff (`{declared}`) is not live: it does not "
            "resolve under `state/handoffs/` (consumed or archived)."
        )
    if not found and not declared_not_live:
        return (
            "No live predecessor handoff carries this plan's `deliverable_id` "
            "and none is declared as its `predecessor_handoff`."
        )
    if not found:
        return "\n\n".join(parts)
    return (
        "Check these facts (for example, committed WIP for your chunk) before "
        "authoring anything.\n\n" + "\n\n".join(parts)
    )


def predecessor_state_section(plan_text: Optional[str], repo_root: Optional[Path]) -> Optional[str]:
    """The full section text to append to a row prompt, or ``None`` when the
    emit has no plan text or repo root to resolve against. Takes the plan text
    the caller already read: the emit opens the plan file a fixed number of
    times."""
    if not plan_text or repo_root is None:
        return None
    return f"{PREDECESSOR_HEADING}\n\n{resolve_predecessor_state(plan_text, Path(repo_root))}"
