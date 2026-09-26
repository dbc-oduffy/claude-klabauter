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
    bi = content.find(begin_marker)
    if bi == -1:
        return None

    ei = content.find(end_marker, bi + len(begin_marker))
    if ei == -1:
        return None

    begin_line_start = bi
    while begin_line_start > 0 and content[begin_line_start - 1] != "\n":
        begin_line_start -= 1
    begin_line_end = bi + len(begin_marker)
    if begin_line_end < len(content) and content[begin_line_end] == "\r":
        begin_line_end += 1
    if begin_line_end < len(content) and content[begin_line_end] == "\n":
        begin_line_end += 1

    end_line_start = ei
    while end_line_start > 0 and content[end_line_start - 1] != "\n":
        end_line_start -= 1
    end_line_end = ei + len(end_marker)
    if end_line_end < len(content) and content[end_line_end] == "\r":
        end_line_end += 1
    if end_line_end < len(content) and content[end_line_end] == "\n":
        end_line_end += 1

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
    pos = _find_markers(content, begin_marker, end_marker)
    if pos is None:
        return None

    head = content[: pos["beginEnd"]]
    tail = content[pos["endStart"] :]

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
    replaced = replace_block(content, begin_marker, end_marker, new_block_content)
    if replaced is not None:
        return replaced

    body = new_block_content
    if len(body) > 0 and not body.endswith("\n"):
        body += "\n"
    block = begin_marker + "\n" + body + end_marker + "\n"

    if insert_at == "start":
        return block + content
    sep = "\n" if len(content) > 0 and not content.endswith("\n") else ""
    return content + sep + block
