"""Contract for artifact.adopt: op name, command text, plan predicates, result shape.

No I/O, stdlib only. The op, the post-write nudge and their tests share this file so the
adopt command text and the plan-path and provenance predicates live in one place.
"""

from __future__ import annotations

import json
import re
from typing import TypedDict

OP_NAME = "artifact.adopt"

_PLAN_ID_PROVENANCE = re.compile(r"""^plan_id:[ \t]*["']?pln-""", re.MULTILINE)


class AdoptResult(TypedDict):
    """Result of artifact.adopt. `diff` covers the frontmatter text only; `changes` holds
    one `key: before -> after` string per edit; `unfilled_required` is the validator's error
    list in its existing hint shape."""

    path: str
    type: str
    dry_run: bool
    applied: bool
    changed: bool
    diff: str
    changes: list[str]
    unfilled_required: list[str]
    command: str


def adopt_command(rel_path: str) -> str:
    """One-line `coordinator-invoke` command that adopts `rel_path` with `write` set."""
    params = {"path": rel_path, "write": True}
    return f"coordinator-invoke {OP_NAME} '{json.dumps(params, separators=(',', ':'))}'"


def has_plan_producer_provenance(fm_text: str) -> bool:
    """True when the frontmatter text has a `plan_id:` whose value starts with `pln-`
    (bare or quoted). Null, absent and `dlv-` values are false."""
    return _PLAN_ID_PROVENANCE.search(fm_text) is not None


def is_plan_path(rel_path: str) -> bool:
    """True for `docs/plans/<name>.md` with exactly one dot in the basename. Sidecars
    (`<stem>.<suffix>.md`) and nested directories are false."""
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) != 3 or parts[0] != "docs" or parts[1] != "plans":
        return False
    name = parts[2]
    return name.count(".") == 1 and name.endswith(".md") and len(name) > len(".md")
