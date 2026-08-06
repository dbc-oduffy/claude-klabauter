"""sentinel_blocks — byte-parity port of coordinator/bin/lib/sentinel-blocks.js.

Extract and replace sentinel-delimited blocks in markdown content.

Consumed via coordinator/bin/lib/sentinel-blocks-cli.js, which `require()`s
the sentinel-blocks.js module this file ports. The .js module is NOT invoked
directly as an executable (no shebang, no main-execution path) — it is a
sourced library consumed via require() by sentinel-blocks-cli.js, so this
port has no example-doctrine-repo-side trampoline; the .js file and its CLI wrapper are left
untouched.

Spec backlink: archive/specs/2026-05-01-portable-ideas-from-obsidian-research.md
§W2 (Sentinel-Block Primitives)
Port recipe pattern: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-normalize-snippet.md (byte-parity port discipline)

Exports:
    extract_block(content, begin_marker, end_marker) -> dict | None
        {"block": str, "before": str, "after": str}
    replace_block(content, begin_marker, end_marker, new_block_content) -> str | None
    insert_or_replace_block(content, begin_marker, end_marker, new_block_content,
                             insert_at="end") -> str

All ops use plain string index lookups — no regex — so markers with special
characters work safely. Markers are treated as exact substrings. Typical
form: <!-- BEGIN x --> / <!-- END x -->.

Negative-spec: this module is a PURE string->string/dict transform (no I/O,
no globals, no side effects) — do not add filesystem/env reads here,
matching the original .js's own purity discipline.
"""
from __future__ import annotations

from typing import Optional, TypedDict


class _MarkerPositions(TypedDict):
    beginStart: int
    beginEnd: int
    endStart: int
    endEnd: int


class BlockResult(TypedDict):
    block: str
    before: str
    after: str


def _find_markers(
    content: str, begin_marker: str, end_marker: str
) -> Optional[_MarkerPositions]:
    """Find begin/end marker positions, handling both "marker on its own line"
    and inline cases.

    Returns a dict of byte offsets into `content`, or None if either marker
    is absent.
    """
    bi = content.find(begin_marker)
    if bi == -1:
        return None

    ei = content.find(end_marker, bi + len(begin_marker))
    if ei == -1:
        return None

    # Determine line extents for begin marker
    begin_line_start = bi
    while begin_line_start > 0 and content[begin_line_start - 1] != "\n":
        begin_line_start -= 1
    begin_line_end = bi + len(begin_marker)
    # Consume trailing newline (including \r\n)
    if begin_line_end < len(content) and content[begin_line_end] == "\r":
        begin_line_end += 1
    if begin_line_end < len(content) and content[begin_line_end] == "\n":
        begin_line_end += 1

    # Determine line extents for end marker
    end_line_start = ei
    while end_line_start > 0 and content[end_line_start - 1] != "\n":
        end_line_start -= 1
    end_line_end = ei + len(end_marker)
    if end_line_end < len(content) and content[end_line_end] == "\r":
        end_line_end += 1
    if end_line_end < len(content) and content[end_line_end] == "\n":
        end_line_end += 1

    # Only use line extents if the text before the marker on its line is
    # whitespace-only. If there's non-whitespace before the marker, treat as
    # inline — use raw positions.
    text_before_begin = content[begin_line_start:bi]
    text_before_end = content[end_line_start:ei]

    begin_is_own_line = text_before_begin.strip() == ""
    end_is_own_line = text_before_end.strip() == ""

    return {
        "beginStart": begin_line_start if begin_is_own_line else bi,
        "beginEnd": begin_line_end if begin_is_own_line else bi + len(begin_marker),
        "endStart": end_line_start if end_is_own_line else ei,
        "endEnd": end_line_end if end_is_own_line else ei + len(end_marker),
    }


def extract_block(
    content: str, begin_marker: str, end_marker: str
) -> Optional[BlockResult]:
    """Extract the content between begin_marker and end_marker.

    Returns {"block": ..., "before": ..., "after": ...} where:
        block  — the text between the two markers (not including marker
                 lines themselves)
        before — the text before (and including) the begin marker line
        after  — the text from (and including) the end marker line to end
                 of file

    Returns None if either marker is not found.
    """
    pos = _find_markers(content, begin_marker, end_marker)
    if pos is None:
        return None

    before = content[: pos["beginEnd"]]
    block = content[pos["beginEnd"] : pos["endStart"]]
    after = content[pos["endStart"] :]

    return {"block": block, "before": before, "after": after}


def replace_block(
    content: str, begin_marker: str, end_marker: str, new_block_content: str
) -> Optional[str]:
    """Replace the block content between begin_marker and end_marker with
    new_block_content.

    Preserves the marker lines themselves. new_block_content is placed
    verbatim between them; a trailing newline is added before the end
    marker if new_block_content doesn't end with one.

    Returns the updated string, or None if either marker is missing.
    """
    pos = _find_markers(content, begin_marker, end_marker)
    if pos is None:
        return None

    # Reconstruct: everything up to (and including) begin marker line, then
    # new content, then end marker line to end of file.
    head = content[: pos["beginEnd"]]
    tail = content[pos["endStart"] :]

    # Ensure new_block_content ends with newline so end marker starts on its
    # own line
    body = new_block_content
    if len(body) > 0 and not body.endswith("\n"):
        body += "\n"

    return head + body + tail


def insert_or_replace_block(
    content: str,
    begin_marker: str,
    end_marker: str,
    new_block_content: str,
    insert_at: str = "end",
) -> str:
    """Like replace_block, but if the markers don't exist, insert them.

    insert_at: "end" (default) appends the block at the end of content.
               "start" prepends at the beginning.

    Returns the updated string (never None).
    """
    replaced = replace_block(content, begin_marker, end_marker, new_block_content)
    if replaced is not None:
        return replaced

    # Markers missing — insert them
    body = new_block_content
    if len(body) > 0 and not body.endswith("\n"):
        body += "\n"
    block = begin_marker + "\n" + body + end_marker + "\n"

    if insert_at == "start":
        return block + content
    # "end" — ensure there's a newline separator
    sep = "\n" if len(content) > 0 and not content.endswith("\n") else ""
    return content + sep + block
