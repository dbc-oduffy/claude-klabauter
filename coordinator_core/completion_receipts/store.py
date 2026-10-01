"""coordinator_core.completion_receipts.store — append-only receipt store and readers.

Writes are exclusive-create and claimed; reads touch disk only. `introducing_commits` is the one
git spawn, batched over every path, so no reader pays a spawn per receipt.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from coordinator_core.completion_receipts.model import (
    RECEIPTS_DIR,
    receipt_rel_path,
    render,
    validate,
)
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_SECS = 30
_HEADER = "\x02"


class ReceiptWriteError(Exception):
    """A receipt failed validation or its path already exists."""


def write_receipt(worktree_root: Path, fm: dict, prose: str) -> str:
    """Validate, exclusive-create and claim one receipt; returns its repo-relative path.

    Opens with O_BINARY: a text-mode fd on Windows would translate LF to CRLF.
    """
    errors = validate(fm)
    if errors:
        raise ReceiptWriteError("receipt invalid: " + "; ".join(errors))
    rel = receipt_rel_path(fm["receipt_id"], fm["concluded_at"])
    if fm.get("prose_ref") != rel:
        raise ReceiptWriteError(f"prose_ref {fm.get('prose_ref')!r} is not the receipt path {rel!r}")
    target = Path(worktree_root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    data = render(fm, prose).encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError as exc:
        raise ReceiptWriteError(f"receipt already exists: {rel}") from exc
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    declare_write(str(target))
    return rel


def read_receipts(worktree_root: Path, *, month: str | None = None) -> list[dict]:
    """Frontmatter dicts (plus `_path`) of every parseable receipt, optionally one `YYYY-MM`."""
    root = Path(worktree_root)
    base = root / RECEIPTS_DIR
    if month is not None:
        base = base / month
    if not base.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(base.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        try:
            fm = yaml.safe_load(split.fm_text)
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict) or fm.get("schema") != "completion-receipt":
            continue
        fm["_path"] = path.relative_to(root).as_posix()
        out.append(fm)
    return out


def current_heads(receipts: list[dict]) -> dict[str, dict]:
    """receipt_id -> receipt, for every receipt no other receipt supersedes."""
    superseded = {r.get("supersedes") for r in receipts if r.get("supersedes")}
    return {r["receipt_id"]: r for r in receipts if r.get("receipt_id") not in superseded}


def introducing_commits(worktree_root: Path, rel_paths: list[str]) -> dict[str, str | None]:
    """path -> sha of the commit that added it (None when none did); ONE git spawn for all paths."""
    result: dict[str, str | None] = {p: None for p in rel_paths}
    if not rel_paths:
        return result
    proc = subprocess.run(
        [
            "git", "-c", "core.quotepath=off", "log", "--diff-filter=A", "--name-only",
            f"--format={_HEADER}%H", "--", *rel_paths,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(worktree_root),
        timeout=_GIT_TIMEOUT_SECS,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )
    if proc.returncode != 0:
        return result
    sha = None
    for line in proc.stdout.splitlines():
        if line.startswith(_HEADER):
            sha = line[len(_HEADER):]
        elif line and line in result and result[line] is None:
            result[line] = sha
    return result
