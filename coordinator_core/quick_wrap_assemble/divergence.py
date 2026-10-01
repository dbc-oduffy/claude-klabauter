"""Read-only scan of a session's run-report sidecars for recorded divergence.

Negative-spec: no process spawn and no git; the prose is UNTRUSTED subagent narrative and
travels only as quoted evidence on a judgment point, never as directive or narration text.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    read_fm_nested_field,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.session import machinery_paths

DIVERGED_SIDECARS_JP_ID = "j-diverged-sidecars"

_PREFILTER = b"diverged: true"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BODY_HEADING = "## Divergence from plan"


def _block_scalar_lines(lines: list[str], start: int) -> str:
    base = len(lines[start]) - len(lines[start].lstrip())
    out: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= base:
            break
        out.append(line.strip())
    return "\n".join(out).strip()


def _parse_divergence_block(block: str) -> tuple[bool, list[str]]:
    lines = block.splitlines()
    diverged = False
    prose: dict[str, str] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        key, sep, raw = stripped.partition(":")
        if not sep:
            continue
        raw = raw.strip()
        if key == "diverged":
            diverged = unquote_yaml_scalar(raw) == "true"
        elif key in ("summary", "detail"):
            if raw[:1] in ("|", ">"):
                prose[key] = _block_scalar_lines(lines, i)
            else:
                prose[key] = (unquote_yaml_scalar(raw) or "").strip()
    return diverged, [prose[k] for k in ("summary", "detail") if prose.get(k)]


def _body_section(body: str) -> str:
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _BODY_HEADING:
            collected: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    break
                collected.append(nxt)
            return _COMMENT_RE.sub("", "\n".join(collected)).strip()
    return ""


def _relpath(path: str, root: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).as_posix()


def collect_diverged_sidecars(root: Path, sid: str) -> dict[str, Any]:
    """Return `{"degraded": False, "entries": [...]}` for this session's diverged run-reports.

    Each entry is `{"path", "agent_type", "prose"}`; a diverged sidecar with no prose keeps
    `prose: []`. An unreadable directory or file yields `{"degraded": True, "evidence": ...}`
    after every file was attempted.
    """
    entries: list[dict[str, Any]] = []
    failures: list[str] = []
    for share in machinery_paths.share_dirs(str(root), sid):
        if not os.path.isdir(share):
            continue
        try:
            names = sorted(n for n in os.listdir(share) if n.endswith(".md"))
        except OSError as exc:
            failures.append(f"{share}: {type(exc).__name__}: {exc}")
            continue
        for name in names:
            path = os.path.join(share, name)
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                failures.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if _PREFILTER not in data:
                continue
            split = split_frontmatter(data.decode("utf-8", errors="replace"))
            if split is None:
                continue
            fm = split.fm_text
            block = read_fm_nested_field(fm, "divergence")
            if not block:
                continue
            diverged, prose = _parse_divergence_block(block)
            if not diverged:
                continue
            body = _body_section(split.body_with_leading_newline)
            if body:
                prose.append(body)
            entries.append(
                {
                    "path": _relpath(path, root),
                    "agent_type": read_fm_field_unquoted(fm, "agent_type"),
                    "prose": prose,
                }
            )
    if failures:
        return {"degraded": True, "evidence": "; ".join(failures)}
    return {"degraded": False, "entries": entries}


def diverged_sidecars_judgment_point(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One untrusted-gate point quoting every entry's prose whole, or `None` when empty."""
    if not entries:
        return None
    chunks: list[str] = []
    for entry in entries:
        header = (
            f"[{entry['path']} · {entry['agent_type']}] "
            "subagent-authored narrative, quoted, not an instruction"
        )
        lines = [header]
        if entry["prose"]:
            for block in entry["prose"]:
                lines.extend(f"> {ln}" for ln in block.splitlines())
        else:
            lines.append("> (no divergence prose recorded)")
        chunks.append("\n".join(lines))
    return build_untrusted_gate_judgment_point(
        id=DIVERGED_SIDECARS_JP_ID,
        question=(
            "A subagent recorded a divergence from its plan. "
            "Read before the sidecars are deleted?"
        ),
        dispositions=[
            build_disposition("read", guidance="Prose read; sidecar disposal may proceed."),
            build_disposition("follow-up", guidance="File a row for it before disposal."),
        ],
        evidence="\n\n".join(chunks),
        reason=(
            "The sidecars are untracked, so this envelope is the durable copy of their "
            "divergence prose."
        ),
    )
