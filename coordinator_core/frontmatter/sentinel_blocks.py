"""
coordinator_core.frontmatter.sentinel_blocks

Extract and replace sentinel-delimited blocks in markdown content.

Spec backlink: example-doctrine-repo `coordinator/bin/lib/sentinel-blocks.js` (144 LoC,
zero dependencies). Also traces to archive/specs/2026-05-01-portable-ideas-
from-obsidian-research.md §W2 (Sentinel-Block Primitives), the original JS
module's own spec backlink.

Exports:
  extract_block(content, begin_marker, end_marker) -> ExtractResult | None
  replace_block(content, begin_marker, end_marker, new_block_content) -> str | None
  insert_or_replace_block(content, begin_marker, end_marker, new_block_content,
                           insert_at="end") -> str

All ops use plain string index lookups -- no regex -- so markers with special
characters work safely. Markers are treated as exact substrings. Typical form:
`<!-- BEGIN x -->` / `<!-- END x -->`.

Line-boundary handling: if a marker sits at the start of its line (possibly
after whitespace), the surrounding newlines are consumed so that the
extracted block / replacement is clean.

Negative-spec: this module does no I/O and holds no state -- pure string
manipulation, 1:1 with the JS original. Do not add a schema.js import here;
disk-verified (2026-07-16) the JS original has zero `require()` statements
despite an earlier plan-text claim that it depends on schema.js.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class _MarkerPositions(NamedTuple):
    """Byte offsets into `content` for the begin/end marker spans.

    begin_start -- index of the first character of the begin marker line (or
                   the marker itself, if inline)
    begin_end   -- index just after the end of the begin marker (including
                   its trailing newline if any)
    end_start   -- index of the first character of the end marker line (or
                   the marker itself, if inline)
    end_end     -- index just after the end of the end marker (including its
                   trailing newline if any)
    """

    begin_start: int
    begin_end: int
    end_start: int
    end_end: int


class ExtractResult(NamedTuple):
    """Result of `extract_block`.

    block  -- the text between the two markers (not including marker lines)
    before -- the text before (and including) the begin marker line
    after  -- the text from (and including) the end marker line to end of file
    """

    block: str
    before: str
    after: str


def _find_markers(
    content: str, begin_marker: str, end_marker: str
) -> Optional[_MarkerPositions]:
    """Find begin/end marker positions, handling both "marker on its own
    line" and inline cases. Returns None if either marker is not found.
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
    # inline -- use raw positions.
    text_before_begin = content[begin_line_start:bi]
    text_before_end = content[end_line_start:ei]

    begin_is_own_line = text_before_begin.strip() == ""
    end_is_own_line = text_before_end.strip() == ""

    return _MarkerPositions(
        begin_start=begin_line_start if begin_is_own_line else bi,
        begin_end=begin_line_end if begin_is_own_line else bi + len(begin_marker),
        end_start=end_line_start if end_is_own_line else ei,
        end_end=end_line_end if end_is_own_line else ei + len(end_marker),
    )


def extract_block(
    content: str, begin_marker: str, end_marker: str
) -> Optional[ExtractResult]:
    """Extract the content between begin_marker and end_marker.

    Returns None if either marker is not found.
    """
    pos = _find_markers(content, begin_marker, end_marker)
    if pos is None:
        return None

    before = content[: pos.begin_end]
    block = content[pos.begin_end : pos.end_start]
    after = content[pos.end_start :]

    return ExtractResult(block=block, before=before, after=after)


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
    head = content[: pos.begin_end]
    tail = content[pos.end_start :]

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

    # Markers missing -- insert them
    body = new_block_content
    if len(body) > 0 and not body.endswith("\n"):
        body += "\n"
    block = begin_marker + "\n" + body + end_marker + "\n"

    if insert_at == "start":
        return block + content

    # "end" -- ensure there's a newline separator
    sep = "\n" if len(content) > 0 and not content.endswith("\n") else ""
    return content + sep + block
