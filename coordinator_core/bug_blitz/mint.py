"""Mint one open `tf` bug-backlog row per failing test file, idempotent per run. Implements `MintRows`.

A row is identified by its file stem under `state/bug-backlog/`. An open `tf` row on the same surface
is reused in place: the run tag is appended to the row's block-list `tags`, the id and every other
byte stay, and nothing is deleted; a reused row still loads through `load_family_records`. Every
reuse is validated before any write, so a row the edit cannot handle aborts the mint with nothing
written. No git spawn.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.ops.queue_append import append_queue_entry
from coordinator_core.ops.queue_family import load_family_records
from coordinator_core.repo_relative_text import to_repo_relative
from coordinator_core.session.declared_writes import declare_write

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
        text = f"- {file}::{test.name}" if test.name else f"- {file}"
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


def _with_tag(text: str, tag: str, rel: str) -> str:
    """Return `text` with `tag` added as one more item of its block-list `tags:`; raises on any other shape."""
    lines = text.split("\n")
    try:
        start = lines.index("tags:")
    except ValueError:
        raise ValueError(f"{rel}: `tags` is not a block list; add the tag by hand") from None
    last = start
    while last + 1 < len(lines) and lines[last + 1].startswith("  - "):
        last += 1
    if last == start:
        raise ValueError(f"{rel}: `tags` is not a block list; add the tag by hand")
    lines.insert(last + 1, f"  - {tag}")
    return "\n".join(lines)


def _plan_reuse(repo_root: Path, rec: dict, tag: str) -> str | None:
    """The rewritten text of an open TF row gaining `tag`, or None when it already carries it. Writes nothing."""
    if tag in (rec["frontmatter"].get("tags") or []):
        return None
    rel = rec["path"]
    with open(repo_root / rel, encoding="utf-8", newline="") as handle:
        text = handle.read()
    return _with_tag(text, tag, rel)


def _write_reuse(repo_root: Path, rel: str, updated: str) -> None:
    """Replace the row at `rel` with `updated`; the tmp is unlinked on any failure, so no partial file survives."""
    path = repo_root / rel
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    declare_write(rel)


def _evidence_ref(repo_root: Path, target_ref: str) -> str:
    """Repo-relative form of an absolute target under the repo; anything else is returned as given."""
    if not os.path.isabs(target_ref):
        return target_ref
    return to_repo_relative(target_ref, str(repo_root)) or target_ref


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
    grouped = _group_by_file(tuple(tests))
    planned: dict[str, str | None] = {}
    errors: list[str] = []
    for file in grouped:
        rec = existing.get(file)
        if rec is None:
            continue
        try:
            planned[file] = _plan_reuse(repo_root, rec, tag)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    ids: list[str] = []
    for file, group in grouped.items():
        rec = existing.get(file)
        if rec is not None:
            updated = planned[file]
            if updated is not None:
                _write_reuse(repo_root, rec["path"], updated)
            ids.append(_row_id(rec["path"]))
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
            evidence=_evidence_ref(repo_root, target_ref),
            repro_steps=f"targeted-test-run --row {file}",
        )
        ids.append(row_id)
    return ids
