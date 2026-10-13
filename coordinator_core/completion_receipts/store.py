"""coordinator_core.completion_receipts.store — append-only receipt store and readers.

Writes are exclusive-create and claimed; reads touch disk only. `introducing_commits` is the one
git reader, batched over every path (one spawn for tracked receipts, one trailer walk for local
ones), so no reader pays a spawn per receipt.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from coordinator_core.completion_receipts.model import (
    LOCAL_RECEIPTS_DIR,
    RECEIPT_TRAILER,
    RECEIPTS_DIR,
    receipt_rel_path,
    render,
    validate,
)
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.session.declared_writes import declare_write

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
    homes = (receipt_rel_path(fm["receipt_id"], fm["concluded_at"]),
             receipt_rel_path(fm["receipt_id"], fm["concluded_at"], local=True))
    rel = fm.get("prose_ref")
    if rel not in homes:
        raise ReceiptWriteError(f"prose_ref {rel!r} is not a receipt path ({' or '.join(homes)})")
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
    """Frontmatter dicts (plus `_path`) of every parseable receipt, tracked and local, optionally
    one `YYYY-MM`."""
    root = Path(worktree_root)
    paths: list[Path] = []
    for home in (RECEIPTS_DIR, LOCAL_RECEIPTS_DIR):
        base = root / home if month is None else root / home / month
        if base.is_dir():
            paths.extend(sorted(base.rglob("*.md")))
    out: list[dict] = []
    for path in paths:
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
    """path -> sha of the commit that added it, or for a local receipt (never tracked) of the
    commit whose `Completion-Receipt:` trailer names its receipt id; None when none did. One git
    spawn per kind of path present."""
    result: dict[str, str | None] = {p: None for p in rel_paths}
    local = {Path(p).stem: p for p in rel_paths if p.startswith(LOCAL_RECEIPTS_DIR + "/")}
    tracked = [p for p in rel_paths if p not in local.values()]
    if tracked:
        _added_commits(worktree_root, tracked, result)
    if local:
        for receipt_id, sha in _trailer_commits(worktree_root, set(local)).items():
            result[local[receipt_id]] = sha
    return result


def _trailer_commits(worktree_root: Path, receipt_ids: set[str]) -> dict[str, str]:
    """receipt id -> sha of the newest commit whose `Completion-Receipt:` trailer names it."""
    proc = run_git(
        [
            "log", f"--grep=^{RECEIPT_TRAILER}: ",
            f"--format={_HEADER}%H%n%(trailers:key={RECEIPT_TRAILER},valueonly=true)",
        ],
        cwd=str(worktree_root),
        timeout=_GIT_TIMEOUT_SECS,
    )
    found: dict[str, str] = {}
    if proc.returncode != 0:
        return found
    sha = None
    for line in proc.stdout.splitlines():
        if line.startswith(_HEADER):
            sha = line[len(_HEADER):]
        elif sha and line.strip() in receipt_ids and line.strip() not in found:
            found[line.strip()] = sha
    return found


def _added_commits(worktree_root: Path, rel_paths: list[str], result: dict) -> None:
    """Fill `result[path]` with the sha of the commit that added it."""
    proc = run_git(
        [
            "-c", "core.quotepath=off", "log", "--diff-filter=A", "--name-only",
            f"--format={_HEADER}%H", "--", *rel_paths,
        ],
        cwd=str(worktree_root),
        timeout=_GIT_TIMEOUT_SECS,
    )
    if proc.returncode != 0:
        return
    sha = None
    for line in proc.stdout.splitlines():
        if line.startswith(_HEADER):
            sha = line[len(_HEADER):]
        elif line and line in result and result[line] is None:
            result[line] = sha
