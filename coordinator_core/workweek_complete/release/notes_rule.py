"""Rule `release-notes`: pending-release file cites every confirmed entry, wrapper carries the real version, lint green.

Read-only; no git child. `ledger_rows` are mappings (or objects) with `id`, `status`, `exit_code`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, Sequence

from coordinator_core.session.record_homes import home_dir
from coordinator_core.workweek_complete.release.contract import (
    RuleResult,
    last_ledger_row,
    row_field,
)

RULE = "release-notes"
PENDING_GLOB = "*-pending-release.md"
WRAPPER_DIR = "archive/release-notes"
LINT_STEP_IDS = ("lint", "lint-frontmatter")
MAX_LISTED = 20
_PLACEHOLDER = re.compile(r"vX\.Y\.Z|(?<![\w.])X\.Y\.Z(?![\w.])|<version>")
_NONE_LINE = re.compile(r"^_?none this week_?$", re.IGNORECASE)


def _miss(reason: str, **evidence: object) -> RuleResult:
    return RuleResult(RULE, False, reason, dict(evidence))


def _has_content(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line == "---":
            continue
        if _NONE_LINE.match(line.strip("*- ").strip()):
            continue
        return True
    return False


def _lint_green(ledger_rows: Iterable[object]) -> bool:
    last = last_ledger_row(ledger_rows, LINT_STEP_IDS)
    return (
        last is not None
        and row_field(last, "status") == "ran"
        and row_field(last, "exit_code") == 0
    )


def evaluate(
    repo_root: Path | str,
    *,
    version: str,
    confirmed_entries: Sequence[str],
    ledger_rows: Sequence[object],
) -> RuleResult:
    root = Path(repo_root)
    pending = sorted(Path(home_dir(str(root), "week-changelog")).glob(PENDING_GLOB), key=lambda p: p.name)
    if not pending:
        return _miss("notes-missing")
    notes = pending[-1]
    notes_rel = notes.relative_to(root).as_posix()
    text = notes.read_text(encoding="utf-8")
    if not _has_content(text):
        return _miss("notes-empty", notes=notes_rel)

    entries = [e.replace("\\", "/") for e in confirmed_entries]
    uncited = [e for e in entries if e not in text]
    if uncited:
        return _miss(
            "entry-not-cited",
            notes=notes_rel,
            entry_count=len(entries),
            uncited_total=len(uncited),
            uncited=uncited[:MAX_LISTED],
        )

    tag = "v" + version.lstrip("v")
    wrappers = sorted((root / WRAPPER_DIR).glob(f"*-{tag}.md"))
    if len(wrappers) != 1:
        return _miss("placeholder-version", notes=notes_rel, tag=tag, wrapper_count=len(wrappers))
    wrapper = wrappers[0]
    wrapper_bytes = wrapper.read_bytes()
    wrapper_rel = wrapper.relative_to(root).as_posix()
    if _PLACEHOLDER.search(wrapper.name) or _PLACEHOLDER.search(wrapper_bytes.decode("utf-8", "replace")):
        return _miss("placeholder-version", notes=notes_rel, wrapper=wrapper_rel)

    if not _lint_green(ledger_rows):
        return _miss("lint-failed", notes=notes_rel, wrapper=wrapper_rel)

    return RuleResult(
        RULE,
        True,
        evidence={
            "notes": notes_rel,
            "wrapper": wrapper_rel,
            "entry_count": len(entries),
            "wrapper_sha256": hashlib.sha256(wrapper_bytes).hexdigest(),
        },
    )
