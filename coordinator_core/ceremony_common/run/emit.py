"""Bind a run digest onto a workflow template and write the script and its fire receipt.

Ceremony-neutral: the template path and the output path arrive as arguments.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from coordinator_core.ops.workflow_bind import bind_args
from coordinator_core.ops.workflow_fire.receipt import write_fire_receipt


def resolve_plugin_root() -> "Path | None":
    """The doctrine-plugin content root, or None when no ladder rung resolves it."""
    from coordinator_core.warm.caller_context import resolve_caller_context

    root = resolve_caller_context().plugin_root
    return Path(root) if root else None


def emit(digest: Mapping[str, Any], *, template: Path, out_path: Path, repo_root: "Path | str") -> Path:
    """Write `out_path` as `template` bound with `{digest, repoRoot}`, plus its `.emitted.json` receipt.

    Raises FileNotFoundError when `template` is absent and ValueError when the template refuses
    the binding; the digest is never written to a path other than `out_path`.
    """
    template = Path(template)
    source = template.read_text(encoding="utf-8")
    bound = bind_args(
        source, {"digest": dict(digest), "repoRoot": Path(repo_root).as_posix()}, source_path=template.as_posix()
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(bound, encoding="utf-8", newline="\n")
    write_fire_receipt(out_path)
    return out_path
