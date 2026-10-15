"""Mint one open `tf` bug-backlog row per failing test file, idempotent per run. Implements `MintRows`.

A row is identified by its file stem under `state/bug-backlog/`. An open `tf` row on the same surface
is reused: the run tag is added by re-appending it through `append_queue_entry`, then the superseded
file is unlinked (the appender's filename carries a content digest, so a tag change lands as a new
file). No git spawn.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.ops.queue_append import append_queue_entry
from coordinator_core.ops.queue_family import load_family_records

SCHEMA = "bug-backlog"
TF_TAG = "tf"

CRASH_TOKENS = (
    "segfault",
    "sigsegv",
    "sigabrt",
    "panic",
    "out of memory",
    "maximum call stack",
)

_MESSAGE_CAP = 300


def is_crash_shaped(message: str) -> bool:
    """True when `message` contains one of the closed `CRASH_TOKENS`, case-insensitively."""
    low = message.lower()
    return any(token in low for token in CRASH_TOKENS)


def _first_line(message: str) -> str:
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line[:_MESSAGE_CAP]
    return ""


def _group_by_file(tests: tuple[FailingTest, ...]) -> dict[str, list[FailingTest]]:
    grouped: dict[str, list[FailingTest]] = {}
    for test in tests:
        grouped.setdefault(test.file, []).append(test)
    return grouped


def _body(file: str, group: list[FailingTest]) -> str:
    lines = ["Failing tests and first message line:"]
    for test in group:
        text = f"- {file}::{test.name}"
        first = _first_line(test.message)
        if first:
            text += f" :: {first}"
        lines.append(text)
    # The appender expands a literal backslash-n into a newline.
    return "\n".join(lines).replace("\\n", " ")


def _open_tf_rows_by_surface(repo_root: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for rec in load_family_records(SCHEMA, repo_root, where="status=open"):
        fm = rec.get("frontmatter") or {}
        if TF_TAG not in (fm.get("tags") or []):
            continue
        surface = fm.get("surface")
        if surface:
            rows.setdefault(str(surface), rec)
    return rows


def _row_id(path: str) -> str:
    return Path(path).stem


def _append(repo_root: Path, **fields: object) -> str:
    result = append_queue_entry(SCHEMA, caller_worktree=repo_root, **fields)
    return _row_id(result["out_path"])


def _reuse(repo_root: Path, rec: dict, tag: str) -> str:
    fm = rec["frontmatter"]
    old_rel = rec["path"]
    tags = list(fm.get("tags") or [])
    if tag in tags:
        return _row_id(old_rel)
    tags.append(tag)
    fields = {
        key: fm[key]
        for key in (
            "title",
            "body",
            "surface",
            "severity",
            "evidence",
            "repro_steps",
            "from_repo",
            "environment",
            "created",
        )
        if fm.get(key) is not None
    }
    row_id = _append(repo_root, status="open", tags=tags, **fields)
    old_abs = repo_root / old_rel
    if _row_id(old_rel) != row_id and old_abs.is_file():
        os.unlink(old_abs)
    return row_id


def mint_tf_rows(
    repo_root: Path,
    run_id: str,
    tests: tuple[FailingTest, ...],
    *,
    target_ref: str,
) -> list[str]:
    """Mint or reuse one open `tf` row per failing test file; returns row ids in first-seen file order.

    Reuse is keyed on `surface` plus the `tf` tag among open rows, so a second call for the same run
    returns the same ids and writes nothing.
    """
    repo_root = Path(repo_root)
    tag = run_tag(run_id)
    existing = _open_tf_rows_by_surface(repo_root)
    ids: list[str] = []
    for file, group in _group_by_file(tuple(tests)).items():
        rec = existing.get(file)
        if rec is not None:
            ids.append(_reuse(repo_root, rec, tag))
            continue
        severity = "P0" if any(is_crash_shaped(t.message) for t in group) else "P1"
        row_id = _append(
            repo_root,
            title=f"TF: {file} ({len(group)} failing)",
            body=_body(file, group),
            status="open",
            surface=file,
            severity=severity,
            tags=[TF_TAG, tag],
            evidence=target_ref,
            repro_steps=f"targeted-test-run --row {file}",
        )
        ids.append(row_id)
    return ids
