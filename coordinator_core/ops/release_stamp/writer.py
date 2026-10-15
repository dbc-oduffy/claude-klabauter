"""Stamps one pending-release completion entry as released, under the entry's lock."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.release_stamp.model import (
    ENTRY_ROOT,
    PENDING,
    RELEASED,
    STAMP_FIELDS,
    ReleaseTag,
    StampError,
    frontmatter_value,
)

_FENCE = "---"


def _apply_fields(text: str, fields: dict[str, str]) -> str:
    """Replace each key of `fields` on its own line, inserting the absent ones before the
    closing fence in STAMP_FIELDS order; every other byte passes through.

    Raises ValueError on fewer than two fence lines.
    """
    out: list[str] = []
    fences = 0
    seen: set[str] = set()
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if bare == _FENCE:
            fences += 1
            if fences == 2:
                out.extend(f"{k}: {fields[k]}\n" for k in STAMP_FIELDS if k not in seen)
            out.append(line)
            continue
        if fences == 1:
            key = bare.split(":", 1)[0]
            if key in fields:
                out.append(f"{key}: {fields[key]}\n")
                seen.add(key)
                continue
        out.append(line)
    if fences < 2:
        raise ValueError("malformed frontmatter")
    return "".join(out)


def stamp_entry(
    worktree_root: Path, rel_path: str, tag: ReleaseTag
) -> tuple[dict | None, str | None]:
    """Stamp `rel_path` released at `tag` if its fresh status is exactly `pending-release`.

    Returns `(record, None)` on a stamp, `(None, reason)` on a skip. Raises
    StampError('bad-params') for a path outside archive/completed. Declares the written
    path only when a stamp lands.
    """
    from coordinator_core.locked_write import LockTimeout, locked_rmw
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.session.declared_writes import declare_write

    root = Path(worktree_root)
    target = contained_path(root / rel_path, [root / ENTRY_ROOT])
    if target is None:
        raise StampError("bad-params", f"{rel_path} is outside {ENTRY_ROOT}")

    record = tag.record(rel_path)
    fields = {"status": RELEASED} | {k: v for k, v in record.items() if k != "path"}
    state: dict[str, str | None] = {"skip": None}

    def mutate(old: str) -> str:
        status = frontmatter_value(old, "status")
        if status != PENDING:
            state["skip"] = f"{rel_path}: status is {status}"
            return old
        try:
            return _apply_fields(old, fields)
        except ValueError:
            state["skip"] = f"{rel_path}: malformed frontmatter"
            return old

    try:
        locked_rmw(target, mutate, repo_root=root)
    except FileNotFoundError as exc:
        return None, f"{rel_path}: unreadable ({exc})"
    except LockTimeout as exc:
        return None, f"{rel_path}: timed out waiting for file lock ({exc})"

    if state["skip"] is not None:
        return None, state["skip"]
    declare_write(rel_path)
    return record, None
