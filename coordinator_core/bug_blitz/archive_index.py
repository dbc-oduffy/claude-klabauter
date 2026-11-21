"""Worktree-local identities published before a bug archive rename."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.locked_write import contended_lock_wait_secs, held_lock

ARCHIVE_DIR = "archive/bug-backlog"


def _index_path(root: Path, month: str) -> Path:
    return root / ".coordinator-local" / "bug-blitz-archive-index-v1" / f"{month}.json"


def read_month(root: Path, month: str) -> dict:
    try:
        rows = json.loads(_index_path(root, month).read_bytes())
    except (OSError, ValueError):
        return {}
    return rows if isinstance(rows, dict) else {}


def index_row(src: Path, row: dict) -> list:
    stamp = src.stat()
    title, surface, tags = row.get("title"), row.get("surface"), row.get("tags")
    identity = (
        [title, surface]
        if isinstance(title, str) and isinstance(surface, str)
        and isinstance(tags, (list, str)) and "tf" in tags
        else None
    )
    return [stamp.st_mtime_ns, stamp.st_size, identity]


def publish_month(root: Path, month: str, rows: dict) -> None:
    path = _index_path(root, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A crash before rename leaves an inert index entry: readers also require the destination.
    with held_lock(path, timeout=contended_lock_wait_secs(), holder_label="TF archive index"):
        updated = read_month(root, month)
        updated.update(rows)
        atomic_write_bytes(path, json.dumps(updated, separators=(",", ":")).encode())


def backfill(root: Path) -> int:
    """Index existing rows outside submit; a row edited later can be backfilled again."""
    count = 0
    for month in sorted((root / ARCHIVE_DIR).glob("*/")):
        rows = {}
        for path in month.glob("*.yaml"):
            try:
                raw = yaml.load(path.read_bytes(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
                rows[path.name] = index_row(path, raw if isinstance(raw, dict) else {})
            except (OSError, ValueError, yaml.YAMLError):
                continue
        if rows:
            publish_month(root, month.name, rows)
        count += len(rows)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill local TF archive identities before submitting a bug blitz")
    parser.add_argument("--repo", type=Path, required=True)
    print(f"Indexed {backfill(parser.parse_args().repo.resolve())} archived rows")
