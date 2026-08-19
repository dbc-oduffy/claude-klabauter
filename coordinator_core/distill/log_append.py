"""
coordinator_core.distill.log_append — canonical distillation-log WRITER.

Purpose: claude-klabauter owns the canonical-log writer so a hand-rolled append can never drift the
format `_common.parse_distillation_log` consumes. Given (path, disposition, fate, run_id),
this module renders EXACTLY ONE canonical row —

    - <path> -> <disposition>, <fate> (run: <run_id>)

— grouped under a `## Run <run_id>` header, and appends it to the target log file. If a
`## Run <run_id>` header already exists in the file, the row is appended directly under
that existing run's row block (no duplicate header); otherwise a new `## Run <run_id>`
header is opened at end-of-file. `append_rows` is the bulk sibling: many rows, one
invocation, validate-all-then-write-once — one invalid row fails the whole batch with
NOTHING written, so a 176-disposal distill run never degrades to hand-rolled appends.

Negative-spec: this module never mutates existing rows, never reorders runs, and never
writes a disposition outside `_common.DISPOSITIONS` — a caller-supplied invalid disposition
raises ValueError rather than silently writing a row `_common`'s parser would skip. A batch
is all-or-nothing: no partial batch ever reaches disk.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C7
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.distill._common import DISPOSITIONS, _RUN_HEADER_RE

__all__ = ["render_row", "append_row", "append_rows"]


@dataclass(frozen=True)
class LogAppendResult:
    """Outcome of an append_row call: the rendered row text and whether a new
    `## Run <id>` header was opened (vs. joining an existing run's block)."""

    row_text: str
    header_opened: bool


def render_row(path: str, disposition: str, fate: str, run_id: str) -> str:
    """Render the exact canonical row text (no trailing newline) for one
    (path, disposition, fate, run_id) tuple: `- <path> -> <disposition>, <fate> (run: <run_id>)`.

    Raises ValueError if `disposition` is not one of `_common.DISPOSITIONS`, or if `path`,
    `fate`, or `run_id` is empty — a malformed row must never reach disk silently.
    """
    if disposition not in DISPOSITIONS:
        raise ValueError(
            f"render_row: disposition {disposition!r} not in {sorted(DISPOSITIONS)}"
        )
    if not path:
        raise ValueError("render_row: path must be non-empty")
    if not fate:
        raise ValueError("render_row: fate must be non-empty")
    if not run_id:
        raise ValueError("render_row: run_id must be non-empty")
    return f"- {path} -> {disposition}, {fate} (run: {run_id})"


def append_row(
    log_path: Path,
    path: str,
    disposition: str,
    fate: str,
    run_id: str,
) -> LogAppendResult:
    """Append exactly one canonical row for (path, disposition, fate, run_id) to
    `log_path`, grouped under `## Run <run_id>`.

    - If `log_path` does not exist, it is created with a fresh `## Run <run_id>` header
      followed by the row.
    - If `log_path` exists and already has a `## Run <run_id>` header, the row is appended
      immediately after that run's last existing row (before the next `## Run` header or
      EOF) — never duplicating the header.
    - If `log_path` exists but has no `## Run <run_id>` header yet, a new header block is
      appended at end-of-file.

    Raises ValueError via `render_row` on an invalid disposition/empty field — no row is
    written in that case.
    """
    row_text = render_row(path, disposition, fate, run_id)

    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"## Run {run_id}\n{row_text}\n", encoding="utf-8", newline="\n")
        return LogAppendResult(row_text=row_text, header_opened=True)

    original_text = log_path.read_text(encoding="utf-8")
    new_text, header_opened = _insert_row_text(original_text, row_text, run_id)
    log_path.write_text(new_text, encoding="utf-8", newline="\n")
    return LogAppendResult(row_text=row_text, header_opened=header_opened)


def append_rows(
    log_path: Path,
    rows: list[dict[str, str]],
) -> list[LogAppendResult]:
    """Append MANY canonical rows to `log_path` in one invocation, all-or-nothing.

    Each row is a dict with keys `path`, `disposition`, `fate`, `run_id` (same semantics
    as `append_row`; rows in one batch may carry different run_ids and each is grouped
    under its own `## Run <run_id>` block per the `append_row` rules).

    Validation is atomic: EVERY row is rendered through `render_row` BEFORE any write —
    one invalid row (bad disposition, empty field, missing key) raises ValueError naming
    the offending row index, and NOTHING is written. On success the file is written
    exactly once. An empty batch raises ValueError (a no-op batch is a caller bug, not
    a silent success).
    """
    if not rows:
        raise ValueError("append_rows: empty batch — at least one row is required")

    rendered: list[tuple[str, str]] = []  # (row_text, run_id)
    for idx, row in enumerate(rows):
        try:
            missing = [k for k in ("path", "disposition", "fate", "run_id") if k not in row]
            if missing:
                raise ValueError(f"missing key(s): {', '.join(missing)}")
            rendered.append(
                (
                    render_row(row["path"], row["disposition"], row["fate"], row["run_id"]),
                    row["run_id"],
                )
            )
        except ValueError as exc:
            raise ValueError(f"append_rows: row {idx}: {exc}") from exc

    if log_path.exists():
        text = log_path.read_text(encoding="utf-8")
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        text = ""

    results: list[LogAppendResult] = []
    for row_text, run_id in rendered:
        text, header_opened = _insert_row_text(text, row_text, run_id)
        results.append(LogAppendResult(row_text=row_text, header_opened=header_opened))

    log_path.write_text(text, encoding="utf-8", newline="\n")
    return results


def _insert_row_text(original_text: str, row_text: str, run_id: str) -> tuple[str, bool]:
    """Pure text transform shared by append_row/append_rows: insert `row_text` into
    `original_text` under `## Run <run_id>` (joining an existing block or opening a new
    one at EOF). Returns (new_text, header_opened). Performs no I/O."""
    if not original_text:
        return f"## Run {run_id}\n{row_text}\n", True

    lines = original_text.splitlines()

    run_header_line_idx: int | None = None
    next_header_idx: int | None = None
    for idx, line in enumerate(lines):
        match = _RUN_HEADER_RE.match(line)
        if not match:
            continue
        if run_header_line_idx is None and match.group("run_id") == run_id:
            run_header_line_idx = idx
            continue
        if run_header_line_idx is not None and idx > run_header_line_idx:
            next_header_idx = idx
            break

    if run_header_line_idx is None:
        # No existing header for this run — open a new block at EOF.
        # Review: code-reviewer (Finding 3, 2026-07-12) — removed dead
        # `needs_leading_blank` local; it was computed but never referenced.
        separator = "\n" if original_text.endswith("\n") else "\n\n"
        new_text = f"{original_text}{separator}## Run {run_id}\n{row_text}\n"
        return new_text, True

    insert_at = next_header_idx if next_header_idx is not None else len(lines)
    new_lines = lines[:insert_at] + [row_text] + lines[insert_at:]
    return "\n".join(new_lines) + "\n", False
