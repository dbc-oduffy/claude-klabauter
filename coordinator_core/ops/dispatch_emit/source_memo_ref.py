"""Resolve the run's source memo: an inbox memo path named in ask/sizing text or the plan's `source_memo`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Optional

_INBOX_REF = re.compile(
    r"(?<![\w./-])(state/cross-repo/inbox/[^\s'\"`()<>/]*?\.md)(?![\w-])"
)


def inbox_memo_refs(text: str) -> tuple[str, ...]:
    """Distinct repo-relative `state/cross-repo/inbox/<name>.md` paths in `text`, posix, first-seen order.

    A repo-prefixed or absolute path (another repo's inbox) is not taken; neither is a `..` name
    or the `<memo>` placeholder.
    """
    if not isinstance(text, str) or not text:
        return ()
    seen: dict[str, None] = {}
    for m in _INBOX_REF.finditer(text.replace("\\", "/")):
        ref = m.group(1)
        name = ref.rsplit("/", 1)[1]
        if ".." in name or name == ".md":
            continue
        seen.setdefault(ref)
    return tuple(seen)


def sizing_texts(sizing: Mapping) -> list:
    """The sizing's intent, pm_verbatims and requirement-register source texts, in that order."""
    if not isinstance(sizing, Mapping):
        return []
    texts: list = [sizing.get("intent")]
    verbatims = sizing.get("pm_verbatims")
    if isinstance(verbatims, list):
        texts.extend(v.get("text") if isinstance(v, Mapping) else v for v in verbatims)
    register = sizing.get("requirement_register")
    rows = register.get("rows") if isinstance(register, Mapping) else None
    if isinstance(rows, list):
        texts.extend(r.get("source_text") for r in rows if isinstance(r, Mapping))
    return texts


def sizing_source_memo(sizing: Mapping) -> Optional[str]:
    """The single inbox memo path named across the sizing's texts (`sizing_texts`); else None."""
    found: dict[str, None] = {}
    for t in sizing_texts(sizing):
        if isinstance(t, str):
            for ref in inbox_memo_refs(t):
                found.setdefault(ref)
    return next(iter(found)) if len(found) == 1 else None


def resolve_source_memo(root: Path, plan_rel: Optional[str]) -> Optional[str]:
    """Plan frontmatter `source_memo`, else the plan's `sizing_object` via `sizing_source_memo`; None if neither.

    Never raises on an unreadable plan or sizing.
    """
    if not plan_rel:
        return None
    try:
        from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

        plan = Path(plan_rel)
        if not plan.is_absolute():
            plan = Path(root) / plan
        split = split_frontmatter(plan.read_text(encoding="utf-8"))
        if split is None:
            return None
        memo = read_fm_field_unquoted(split.fm_text, "source_memo")
        if memo not in (None, "", "null", "~"):
            return memo
        sizing_rel = read_fm_field_unquoted(split.fm_text, "sizing_object")
        if sizing_rel in (None, "", "null", "~"):
            return None
        from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

        return sizing_source_memo(load_sizing(Path(root), sizing_rel))
    except Exception:
        return None
