"""Commit-time atlas drift repair: compute which system pages a commit's own path list touches,
repair what is mechanical, and record the rest as an already-acted fact.

Contract: no subprocess; reads bounded by the commit's path list plus the atlas pages (one
`load_recorded_atlas` parse), never a tree walk. Only a page's `files:` frontmatter count is
rewritten; page prose is never touched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.cartography.atlas_record import (
    ATLAS_PARSED_ZERO_RULES,
    ATLAS_UNREADABLE,
    is_source_candidate,
    load_recorded_atlas,
    recorded_system_for_path,
)

ACTED_LEDGER_RELPATH = ".coordinator-local/freshness/atlas-acted.jsonl"
"""Repo-root-relative ledger of acted facts, one JSON object per line."""

_SYSTEMS_DIR = "docs/architecture/systems"
_FILES_LINE_RE = re.compile(r"^(files:[ \t]*)(\d+)([ \t]*\r?)$", re.MULTILINE)
_UNCOMMITTED_SHA = "uncommitted"
_DETAIL_PATH_CAP = 5


@dataclass(frozen=True)
class RepairPlan:
    """A mechanical repair of one system page: `files_delta` is the net change to its `files:` count."""

    page_relpath: str
    files_delta: int


@dataclass(frozen=True)
class ActedFact:
    """What the engine already did about drift on `page` at commit `sha`.

    `kind` is "repaired", "content_drift", "uncovered", "page_dirty" or "fold_error". `at` is an
    ISO-8601 UTC date so a reader can order it against a page's `last_attested` date (a sha is not
    orderable against a date).
    """

    page: str
    sha: str
    kind: str
    detail: str
    at: str = ""


def _norm(relpath: str) -> str:
    return relpath.replace("\\", "/").lstrip("/")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _detail(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:_DETAIL_PATH_CAP])
    extra = len(paths) - _DETAIL_PATH_CAP
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def plan_repair(
    root: str | Path,
    paths: Sequence[str],
    deleted_paths: Collection[str],
    added_paths: Collection[str] = frozenset(),
) -> tuple[list[RepairPlan], list[ActedFact]]:
    """Plan the repair a commit of `paths` implies.

    `deleted_paths`: members of `paths` absent from the working tree (a stat, no spawn).
    `added_paths`: members of `paths` absent from HEAD -- the value `commit_scoped` already holds
    (`_v2_state_records_chunked` record `m_head == "000000"`, equivalently `pre_head_blobs`
    missing the path). A path in neither set is a modification.

    Returns `([], [])` when the atlas is unreadable or its rule-10 package table is absent: the
    path-to-system rules are claude-klabauter's own layout, so a repo they do not describe is never judged.
    An added covered path is `+1` on its page, a deleted one `-1`; a covered modification is a
    `content_drift` fact; an added or modified source candidate no rule maps is `uncovered`.
    """
    atlas = load_recorded_atlas(root)
    if atlas.error in (ATLAS_UNREADABLE, ATLAS_PARSED_ZERO_RULES):
        return [], []

    root_path = Path(root)
    deleted = {_norm(p) for p in deleted_paths}
    added = {_norm(p) for p in added_paths}

    deltas: dict[str, int] = {}
    drifted: dict[str, list[str]] = {}
    uncovered: dict[str, list[str]] = {}

    for raw in dict.fromkeys(_norm(p) for p in [*paths, *deleted]):
        if not is_source_candidate(raw) or raw.startswith("state/"):
            continue
        is_deleted = raw in deleted
        is_added = raw in added
        if is_deleted and is_added:
            continue
        system = recorded_system_for_path(raw, atlas)
        if system is None:
            if not is_deleted:
                uncovered.setdefault("docs/architecture/file-index.md", []).append(raw)
            continue
        page = f"{_SYSTEMS_DIR}/{system}.md"
        if system not in atlas.system_files or not (root_path / page).is_file():
            if not is_deleted:
                uncovered.setdefault("docs/architecture/file-index.md", []).append(raw)
            continue
        if is_deleted:
            deltas[page] = deltas.get(page, 0) - 1
        elif is_added:
            deltas[page] = deltas.get(page, 0) + 1
        else:
            drifted.setdefault(page, []).append(raw)

    plans = [RepairPlan(page, delta) for page, delta in sorted(deltas.items()) if delta != 0]
    at = _today()
    facts = [
        ActedFact(page, "", "content_drift", _detail(files), at)
        for page, files in sorted(drifted.items())
    ]
    facts += [
        ActedFact(page, "", "uncovered", _detail(files), at)
        for page, files in sorted(uncovered.items())
    ]
    return plans, facts


def _dirty_pages(root: Path, pages: Sequence[str]) -> set[str]:
    """Pages whose working copy may differ from the index; an unreadable index marks all dirty."""
    from coordinator_core.git.git_index import scoped_status

    try:
        verdicts = scoped_status(root, list(pages))
    except Exception:  # noqa: BLE001 -- conservative: never sweep a page we cannot prove clean
        return set(pages)
    return {p for p in pages if verdicts.get(p) != "clean"}


def apply_repair(root: str | Path, plans: Sequence[RepairPlan]) -> list[str]:
    """Write each plan's page edit under `root`; return the page relpaths actually written.

    Rewrites only the `files:` frontmatter line. A page already modified in the working tree is
    skipped and recorded as a `page_dirty` fact, so a peer's uncommitted page edit is never swept
    into this commit.
    """
    root_path = Path(root)
    if not plans:
        return []
    dirty = _dirty_pages(root_path, [p.page_relpath for p in plans])
    written: list[str] = []
    skipped: list[ActedFact] = []
    at = _today()
    for plan in plans:
        page = plan.page_relpath
        if page in dirty:
            skipped.append(
                ActedFact(page, _UNCOMMITTED_SHA, "page_dirty", f"files_delta {plan.files_delta:+d} not applied", at)
            )
            continue
        target = root_path / page
        try:
            text = target.read_bytes().decode("utf-8")
        except OSError:
            continue
        fm_end = text.find("\n---", 3) if text.startswith("---") else -1
        if fm_end < 0:
            continue
        match = _FILES_LINE_RE.search(text, 0, fm_end)
        if match is None:
            continue
        new_count = max(0, int(match.group(2)) + plan.files_delta)
        if new_count == int(match.group(2)):
            continue
        new_text = text[: match.start()] + f"{match.group(1)}{new_count}{match.group(3)}" + text[match.end():]
        target.write_bytes(new_text.encode("utf-8"))
        written.append(page)
    if skipped:
        record_acted(root_path, skipped, _UNCOMMITTED_SHA)
    return written


def record_acted(root: str | Path, facts: Sequence[ActedFact], sha: str) -> None:
    """Append `facts` to the ledger at `ACTED_LEDGER_RELPATH`, stamped with `sha`.

    A fact that already carries a `sha` keeps it; an empty `at` is stamped with today's UTC date.
    """
    if not facts:
        return
    ledger = Path(root) / ACTED_LEDGER_RELPATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    today = _today()
    lines = [
        json.dumps(
            {
                "page": f.page,
                "sha": f.sha or sha,
                "kind": f.kind,
                "detail": f.detail,
                "at": f.at or today,
            },
            ensure_ascii=False,
        )
        for f in facts
    ]
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def read_acted(root: str | Path) -> dict[str, ActedFact]:
    """Return the newest ledger fact per page; an absent ledger is an empty dict.

    Malformed lines are skipped; a later line for the same page replaces an earlier one.
    """
    ledger = Path(root) / ACTED_LEDGER_RELPATH
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return {}
    newest: dict[str, ActedFact] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            fact = ActedFact(
                page=str(row["page"]),
                sha=str(row["sha"]),
                kind=str(row["kind"]),
                detail=str(row.get("detail", "")),
                at=str(row.get("at", "")),
            )
        except (ValueError, KeyError, TypeError):
            continue
        newest[fact.page] = fact
    return newest
