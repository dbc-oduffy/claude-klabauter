"""EM brief for the warp --ask arm: load, bound and render one clause every composed prompt prefixes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from coordinator_core.ops.workflow_scaffold import _js_string_literal

EM_BRIEF_VAR = "_EM_BRIEF"
EM_BRIEF_BYTE_CAP = 16 * 1024
_HEADER = "EM brief for this run — binding context; this prompt's task governs"


class EmBriefRefused(ValueError):
    """The brief inputs cannot be admitted; the message names the offending flag or path."""


@dataclass(frozen=True)
class EmBrief:
    """``source`` is ``"inline"`` or the repo-relative brief-file path; ``context`` is repo-relative."""

    text: str
    source: str
    context: tuple[str, ...]


def _rel(repo_root: Path, raw: str, flag: str) -> tuple[Path, str]:
    root = repo_root.resolve()
    # A Windows-typed backslash path names the same file on every host.
    p = Path(raw.replace("\\", "/"))
    resolved = (p if p.is_absolute() else root / p).resolve()
    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        raise EmBriefRefused(f"{flag} {raw}: outside the repo root") from None
    return resolved, rel


def load_em_brief(
    repo_root: Path,
    *,
    brief: str | None,
    brief_file: str | None,
    context: Sequence[str] = (),
) -> EmBrief | None:
    """None when brief, brief_file and context are all empty; EmBriefRefused otherwise on bad input."""
    if brief and brief_file:
        raise EmBriefRefused("--brief and --brief-file are mutually exclusive")
    ctx_paths = tuple(c for c in context if c)
    if not brief and not brief_file and not ctx_paths:
        return None

    if brief_file:
        path, source = _rel(repo_root, brief_file, "--brief-file")
        if not path.is_file():
            raise EmBriefRefused(f"--brief-file {brief_file}: no such file")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise EmBriefRefused(f"--brief-file {brief_file}: unreadable ({exc.__class__.__name__})") from None
        if not text.strip():
            raise EmBriefRefused(f"--brief-file {brief_file}: empty")
    else:
        text, source = brief or "", "inline"

    if len(text.encode("utf-8")) > EM_BRIEF_BYTE_CAP:
        raise EmBriefRefused(f"brief text exceeds {EM_BRIEF_BYTE_CAP} bytes")

    ctx_rel: list[str] = []
    for raw in ctx_paths:
        path, rel = _rel(repo_root, raw, "--context")
        if not path.is_file():
            raise EmBriefRefused(f"--context {raw}: no such file")
        ctx_rel.append(rel)
    return EmBrief(text=text, source=source, context=tuple(ctx_rel))


def check_em_brief_paths(repo_root: Path, *, brief_file: str | None, context: Sequence[str] = ()) -> None:
    """Refuse a --brief-file or --context path that is outside the repo or absent: stats only, no read."""
    named = [("--brief-file", brief_file)] if brief_file else []
    for flag, raw in named + [("--context", c) for c in context if c]:
        path, _ = _rel(repo_root, raw, flag)
        if not path.is_file():
            raise EmBriefRefused(f"{flag} {raw}: no such file")


def _clause(b: EmBrief) -> str:
    parts = [_HEADER, b.text.strip()]
    if b.context:
        parts.append("Read each context file first: " + ", ".join(b.context))
    return "\n".join(parts) + "\n\n"


def brief_decl_js(b: EmBrief) -> str:
    return f"const {EM_BRIEF_VAR} = {_js_string_literal(_clause(b))};"


def receipt_fields(b: EmBrief) -> dict:
    return {
        "source": b.source,
        "sha256": hashlib.sha256(b.text.encode("utf-8")).hexdigest(),
        "context": list(b.context),
    }
