"""`approval_body_sha` is byte-identical to its pre-clearance-exclusion form for every real plan
whose gated rows carry no engine-written clearance value, so existing stamps keep validating."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.frontmatter.primitives import (
    APPROVED_BODY_OK,
    approval_body_sha,
    check_approved_body,
    frontmatter_body_text,
    git_blob_sha1,
    stamp_approved_body_sha,
)

_REPO = Path(__file__).resolve().parents[3]


_OLD_SPINE_KEY_INDENT_RE = re.compile(r'^([ \t]*-[ \t]+)id:')
_OLD_DISP_RE = re.compile(r'^[ \t]*disposition:[ \t]*(open|coded)[ \t]*$')
_OLD_DISP_REF_RE = re.compile(r'^[ \t]*disposition_ref:')
_OLD_ANY_DISP_RE = re.compile(r'^[ \t]*disposition:')


def _frozen_old_approval_body_sha(file_text: str) -> Optional[str]:
    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

    body = frontmatter_body_text(file_text)
    located = locate_fenced_block(body)
    if located.status != LocateStatus.LOCATED or located.span is None:
        return git_blob_sha1(body)
    start, end = located.span
    lines = body[start:end].splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    key_indent = None
    for idx, line in enumerate(lines):
        m = _OLD_SPINE_KEY_INDENT_RE.match(line)
        if m is None:
            continue
        if key_indent is None:
            key_indent = len(m.group(1)) - len(m.group(1).lstrip(' \t'))
        if len(m.group(1)) - len(m.group(1).lstrip(' \t')) != key_indent:
            continue
        spans.append([idx, len(lines), line[m.end():].strip().strip('\'"')])
    for i in range(len(spans) - 1):
        spans[i][1] = spans[i + 1][0]
    if not spans:
        return git_blob_sha1(body)

    rows: list[tuple[str, bool, list[str]]] = []
    for s_idx, e_idx, row_id in spans:
        row_lines = lines[s_idx:e_idx]
        content_indent = _OLD_SPINE_KEY_INDENT_RE.match(row_lines[0]).end() - len('id:')
        is_do = True
        kept: list[str] = []
        for n, line in enumerate(row_lines):
            text = line.rstrip('\r\n')
            at_key = n == 0 or (len(text) - len(text.lstrip(' \t'))) == content_indent
            if at_key and n > 0:
                if _OLD_DISP_REF_RE.match(text) or _OLD_DISP_RE.match(text):
                    continue
                if _OLD_ANY_DISP_RE.match(text):
                    is_do = False
            kept.append(line)
        while len(kept) > 1 and not kept[-1].strip():
            kept.pop()
        if kept and not kept[-1].endswith(('\n', '\r')):
            kept[-1] += '\n'
        rows.append((row_id, is_do, kept))

    do_sorted = iter(sorted((r for r in rows if r[1]), key=lambda r: r[0]))
    out = lines[: spans[0][0]]
    for row in rows:
        out.extend((next(do_sorted) if row[1] else row)[2])
    normalized = body[:start] + ''.join(out).rstrip('\n') + body[end:]
    return git_blob_sha1(normalized)


def _gated_plans() -> list[Path]:
    found = []
    for path in sorted((_REPO / "docs" / "plans").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "external_gate:" in text and "```yaml plan-tasks" in text and not re.search(
            r"^\s+(?:-\s+)?(?:cleared: true|cleared_evidence:|closure_evidence:)", text, re.M
        ):
            found.append(path)
        if len(found) >= 12:
            break
    return found


def test_there_are_real_gated_plans_to_compare():
    assert _gated_plans()


@pytest.mark.parametrize("path", _gated_plans(), ids=lambda p: p.name)
def test_unchanged_for_real_plans_with_unstamped_gates(path):
    text = path.read_text(encoding="utf-8")
    assert approval_body_sha(text) == _frozen_old_approval_body_sha(text)
    stamped = stamp_approved_body_sha(text)
    assert check_approved_body(stamped)[0] == APPROVED_BODY_OK
    assert _frozen_old_approval_body_sha(stamped) == approval_body_sha(stamped)
