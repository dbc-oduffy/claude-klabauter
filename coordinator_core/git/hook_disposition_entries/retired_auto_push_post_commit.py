"""The retired `coordinator-auto-push` post-commit hook: whole-file `remove` for
the generated fresh and older exec bodies, `excise-block` for the block the
generator once appended to a foreign hook.

Identification is exact: a body that merely mentions the retired helper, or
whose exec tail was hand-edited away, is not matched.
"""

from __future__ import annotations

import re
from typing import Optional

from coordinator_core.git.hook_dispositions import HookDisposition, Match

ENTRY_ID = "retired-auto-push-post-commit"

_RETIRED_HEADER = "coordinator auto-push (crash insurance)"
_START_MARKER = f"# === {_RETIRED_HEADER} ==="
_END_MARKER = f"# === END {_RETIRED_HEADER} ==="

_FRESH_MARKER = "# coordinator coordinator-auto-push hook — installed by git_hook_install."
_FRESH_TAIL = 'exec "$_PY" "$SCRIPT" "$@"'

_OLDER_EXEC = re.compile(
    r'^exec\s+(?:ba)?sh\s+"?(?:\$HOME|~)/\.claude/plugins/coordinator-claude/\S*coordinator-auto-push(?:\.py)?"?'
    r'(?:\s+"?\$@"?)?\s*$'
)


def _is_older_exec_body(text: str) -> bool:
    """Shebang, comments, and a lone `exec bash <marketplace>/coordinator-auto-push`."""
    code = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return len(code) == 1 and _OLDER_EXEC.match(code[0]) is not None


def _has_line(text: str, exact_line: str) -> bool:
    return any(line.strip() == exact_line for line in text.splitlines())


def _block_span(text: str) -> Optional[Match]:
    """Character span of the marker-delimited block, whole lines inclusive.
    None unless exactly one start and one later end marker exist."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _START_MARKER]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == _END_MARKER]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    return Match(sum(map(len, lines[: starts[0]])), sum(map(len, lines[: ends[0] + 1])))


def _identify_whole_body(text: str) -> Optional[Match]:
    if _block_span(text) is not None:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if _has_line(text, _FRESH_MARKER) and lines[-1].strip() == _FRESH_TAIL:
        return Match(0, len(text))
    if _is_older_exec_body(text):
        return Match(0, len(text))
    return None


ENTRY = HookDisposition(
    id=ENTRY_ID,
    hook_name="post-commit",
    action="remove",
    identify=_identify_whole_body,
)

BLOCK_ENTRY = HookDisposition(
    id=f"{ENTRY_ID}-block",
    hook_name="post-commit",
    action="excise-block",
    identify=_block_span,
)

# One disposition carries one action, so the whole-body and appended-block
# shapes are two entries; `load_entries` must register both.
ENTRIES = (ENTRY, BLOCK_ENTRY)
