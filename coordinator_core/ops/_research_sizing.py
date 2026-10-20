"""Drop a sizing's `research:` block under the file lock, schema-validated.

A sizing that still carries a non-empty research block reads as a research sizing to the ask gate and
the engine-ask router; removing the block (rather than stamping it closed) satisfies every reader
unchanged and needs no change to the DoE-vendored sizing schema.

Negative-spec: spawns no process; never adds a key to the sizing; never rewrites a sizing that has no
research block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_SIZING_SCHEMA = Path(__file__).resolve().parents[1] / "frontmatter" / "schemas" / "sizing-object.schema.json"


class ResearchSizingRefused(Exception):
    """str(exc) is the one-line reason the drop was refused."""


def _resolve(root: Path, sizing_rel: str) -> Path:
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.session.record_homes import home_dir

    found = contained_path(Path(root) / sizing_rel, [Path(home_dir(str(root), "sizings"))])
    if found is not None and found.is_file():
        return found
    # The review stamp moves a sizing into the archive; the archived record is edited in place.
    from coordinator_core.ops._sizing_citation import resolve_sizing_citation

    archived = resolve_sizing_citation(root, sizing_rel)
    if archived is not None and archived.is_file():
        contained = contained_path(archived, [Path(root) / "archive" / "sizings"])
        if contained is not None:
            return contained
    raise ResearchSizingRefused(
        f"sizing {sizing_rel!r} is not a file under the sizings home or the sizings archive"
    )


def _error_keys(text: str) -> set[tuple[str, str]]:
    import yaml

    from coordinator_core.frontmatter.schema_validate import validate_frontmatter

    errors = validate_frontmatter(yaml.safe_load(text) or {}, _SIZING_SCHEMA)
    return {(str(e.get("field", "?")), str(e.get("error", "?"))) for e in errors}


def drop_research_block(root: Path, sizing_rel: str) -> Optional[bytes]:
    """Remove the sizing's `research:` key. Returns the pre-drop bytes when a block was removed, None
    when there was none (nothing written). Refuses with ResearchSizingRefused on a path outside the
    sizings home and archive, a missing file, a lock timeout, or a schema error the removal introduces
    (an error already present before the removal does not refuse). CRLF line endings are preserved."""
    from coordinator_core.frontmatter.primitives import remove_fm_nested_field
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    root = Path(root)
    target = _resolve(root, sizing_rel)
    out: dict[str, bytes] = {}

    def mutate(old: str) -> str:
        new = remove_fm_nested_field(old, "research")
        if new == old:
            return old
        introduced = _error_keys(new) - _error_keys(old)
        if introduced:
            raise MutateAbort(
                "dropping the research block introduces schema errors: "
                + "; ".join(f"{f}: {e}" for f, e in sorted(introduced))
            )
        raw = target.read_bytes()
        out["before"] = raw
        return new.replace("\n", "\r\n") if b"\r\n" in raw else new

    try:
        locked_rmw(target, mutate, repo_root=root)
    except MutateAbort as exc:
        raise ResearchSizingRefused(str(exc.args[0]) if exc.args else "research drop refused") from exc
    except LockTimeout as exc:
        raise ResearchSizingRefused(f"timed out waiting for the lock on {sizing_rel}: {exc}") from exc
    except OSError as exc:
        raise ResearchSizingRefused(f"cannot edit {sizing_rel}: {exc}") from exc
    return out.get("before")
