"""Read-contract adapter for example-retrieval-repo's `changelog_resolve` and `doc_outline` forms.

The forms are looked up in-process on example-retrieval-repo's read-contract module; no subprocess is spawned.
A form that is absent, an import that fails, or a call that raises yields `fallback=True` plus a gap
`{form, reason}` for the caller to surface to example-retrieval-repo, and the caller then uses
`changelog_walk`'s parse. Never raises on a missing or broken form.

Assumed form shapes (unconfirmed upstream until example-retrieval-repo serves them):
  changelog_resolve(entry={"title", "day", "commits"}) -> iterable of {"path": str, "outline": [str] | str}
  doc_outline(paths=[str]) -> iterable of {"path": str, "outline": [str] | str}
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any

from coordinator_core.backlog.record_schema import FlaggedChange

__all__ = ["RagRead", "READ_CONTRACT_MODULE", "changelog_resolve", "doc_outline"]

READ_CONTRACT_MODULE = "example_retrieval_repo_core.read_contract"

Loader = Callable[[], Any]


@dataclass(frozen=True)
class RagRead:
    """Result of one read-contract call. `fallback` means the caller must parse the changelog files
    itself; `gaps` are `{form, reason}` records to surface to example-retrieval-repo; `outlines` maps a path
    to its heading outline; `change` is the input FlaggedChange with rag-resolved paths merged in."""

    fallback: bool
    gaps: tuple[dict[str, str], ...] = ()
    outlines: dict[str, tuple[str, ...]] | None = None
    change: FlaggedChange | None = None


def _default_loader() -> ModuleType:
    return importlib.import_module(READ_CONTRACT_MODULE)


def _gap(form: str, reason: str) -> RagRead:
    return RagRead(fallback=True, gaps=({"form": form, "reason": reason},))


def _call(form: str, loader: Loader | None, **kwargs: Any) -> tuple[list[Any] | None, RagRead | None]:
    """(rows, None) on success, (None, fallback RagRead) when the form is missing or unreachable."""
    try:
        module = (loader or _default_loader)()
    except ImportError as exc:
        return None, _gap(form, f"read contract not importable: {exc}")
    except Exception as exc:  # unreachable contract is a fallback, never a crash
        return None, _gap(form, f"read contract unreachable: {type(exc).__name__}: {exc}")
    fn = getattr(module, form, None)
    if not callable(fn):
        return None, _gap(form, "form not served by the read contract")
    try:
        rows = fn(**kwargs)
        return list(rows or []), None
    except Exception as exc:
        return None, _gap(form, f"form call failed: {type(exc).__name__}: {exc}")


def _outline(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(line for line in value.splitlines() if line.strip())
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value)
    return ()


def _split(rows: list[Any]) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    paths: list[str] = []
    outlines: dict[str, tuple[str, ...]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        path = row["path"]
        if path not in outlines:
            paths.append(path)
        outlines[path] = _outline(row.get("outline"))
    return paths, outlines


def changelog_resolve(change: FlaggedChange, *, loader: Loader | None = None) -> RagRead:
    """Map one flagged changelog entry to the artifacts it touches, with outlines. The returned
    `change` carries the rag paths merged after its own cited_paths."""
    rows, failed = _call("changelog_resolve", loader,
                         entry={"title": change.title, "day": change.day, "commits": list(change.commits)})
    if failed is not None:
        return replace(failed, change=change)
    paths, outlines = _split(rows or [])
    merged = tuple(dict.fromkeys((*change.cited_paths, *paths)))
    return RagRead(fallback=False, outlines=outlines, change=replace(change, cited_paths=merged))


def doc_outline(paths: Iterable[str], *, loader: Loader | None = None) -> RagRead:
    """Outlines for a list of repo-relative paths."""
    wanted = list(paths)
    rows, failed = _call("doc_outline", loader, paths=wanted)
    if failed is not None:
        return failed
    _, outlines = _split(rows or [])
    return RagRead(fallback=False, outlines=outlines)
