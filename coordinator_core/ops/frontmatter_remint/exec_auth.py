"""execution_authorized_* resolver for `frontmatter.remint`: copy the governing plan's stamp.

The four fields are the PM's sign-off recorded on the plan; they are copied verbatim and
never computed. A handoff without a resolvable `governing_plan`, or one whose plan lacks
any of the four, is refused rather than given a fabricated authorization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved
from coordinator_core.ops.handoff_phase_stamp import _EXEC_FIELDS


def resolve(path: Path, fm: Mapping[str, Any], repo_root: Path) -> Resolved | Refused:
    """Copy the four execution_authorized_* values from `fm["governing_plan"]`'s frontmatter."""
    raw = fm.get("governing_plan")
    rel = raw.strip() if isinstance(raw, str) else ""
    if not rel or rel.lower() in {"null", "none"}:
        return Refused("no-governing-plan", "governing_plan is absent or null")

    plan = contained_path(repo_root / rel, [repo_root])
    if plan is None or not plan.is_file():
        return Refused("no-governing-plan", f"governing_plan does not resolve to a file under the repo: {rel}")

    try:
        split = split_frontmatter(plan.read_text(encoding="utf-8"))
    except OSError as exc:
        return Refused("no-governing-plan", f"cannot read governing_plan {rel}: {exc}")
    if split is None:
        return Refused("no-governing-plan", f"governing_plan has no YAML frontmatter: {rel}")

    values: dict[str, Any] = {}
    missing: list[str] = []
    for key in _EXEC_FIELDS:
        value = read_fm_field_unquoted(split.fm_text, key)
        if value is None or not str(value).strip():
            missing.append(key)
        else:
            values[key] = value
    if missing:
        return Refused("plan-unauthorized", f"{rel} lacks {', '.join(missing)}")
    return Resolved(values)
