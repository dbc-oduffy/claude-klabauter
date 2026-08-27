"""
coordinator_core.frontmatter.body_blocks

Shared fenced-block locator for plan-body YAML blocks (e.g. the `## Tasks`
` ```yaml plan-tasks` spine). Parity target: DoE-claude
`coordinator/bin/coordinator-harvest-deferrals:317-372` (`_locate_tasks_block`).

DoE's reference returns only the block body string (or None on any failure,
collapsing "absent" and "malformed" into one outcome). This port diverges in
two ways, both deliberate:

  1. It returns a typed `LocateResult` distinguishing `located` / `absent`
     (zero fenced blocks matching the info-string, post-comment-blanking) /
     `malformed` (more than one fenced block, or a heading with no fence
     located inside its section). Callers (e.g. need-1's `add-task`)
     legitimately need to create the block when absent, but must fail-loud
     when malformed — collapsing both to `None` would erase that distinction.
  2. It returns the located block's `(start, end)` character span in the
     source, in addition to the body text. This is net-new: DoE's
     `_locate_tasks_block` has no span concept because it never splices back
     into the source. The span is pinned to `match.span(1)` — the fence-BODY
     span only, exclusive of the ` ```yaml plan-tasks\\n` opener and the
     `\\n``` ` closer — so a caller can replace `source[start:end]` with
     re-serialized YAML while leaving the fence markers themselves intact.

PUBLIC CROSS-REPO SEAM (2026-07-29) — `locate_fenced_block` and
`LocateStatus` are imported BY NAME from outside this repo. DoE-claude's
write-time guard `coordinator/hooks/scripts/validate-frontmatter-schema.py`
imports both to validate a plan's task-spine rows at authoring time (the
gate that closes plan-tasks.schema.json's closed enums —
change_kind/disposition/queue_scope — which nothing enforced before).

Treat these two names, and `LocateResult`'s field names, as an external
contract: additive changes only. Renaming, relocating, or changing the
return shape does NOT fail their build — their import is wrapped in a
bare `except Exception` that writes a stderr line and sets the validator
to `None`, deliberately, so their hook never blocks a write on our infra.
The failure mode is therefore SILENT INERTNESS: their authoring gate
stops running and every plan still commits green. Nobody gets a red test.

That fail-open is their call and the right one for a write-time hook; the
obligation it creates is ours. Give claude-central-em a heads-up before
touching either name — same standing arrangement as
`coordinator_core.contract.cockpit_schema.emit_schema` (see CLAUDE.md
§ Architecture), and the same reason: a sibling's capability depends on a
symbol whose disappearance we would not otherwise notice.

Negative-spec: do NOT scan the document for any generic YAML fence. The
locate rule is heading -> fence containment for one exact info-string;
matching any ```yaml fence would mis-locate on plans carrying unrelated
fenced YAML.

HTML-comment blanking (silent-data-loss fix, ported from the DoE reference):
before any matching, `source` is scanned via a comment-blanked COPY
(`scan_text`, every `<!--...-->` span replaced by an equal-length run of
spaces via `re.DOTALL`) — never against `source` directly. `coordinator-doc-new`
scaffolds every new plan with an authoring HTML comment under `## Tasks` that
embeds a literal ```` ```yaml plan-tasks ```` token as documentation.
Unblanked, that comment counts as a second fence (false MALFORMED) and as
non-blank content between the heading and the real fence (false MALFORMED
under the old adjacency rule). Because blanking is length-preserving, every
offset computed against `scan_text` is valid against the ORIGINAL `source` —
`body` is sliced from `source`, not `scan_text`, so a caller splicing
`source[start:end]` round-trips exactly. A plan with two REAL (non-comment)
fenced blocks still counts as 2 fences post-blanking and is still MALFORMED
(see `multiple-fenced-blocks.md`) — blanking narrows false positives, it does
not weaken the genuine-duplicate guard.

Containment, not adjacency (fix ported from the DoE reference): the fence
must live INSIDE the `## <heading>` section — bounded at the next
`^##\\s+\\S` heading, or end of document — not merely on blank lines directly
below the heading line. Real reviewed plans routinely carry load-bearing
prose (a pinned-interface paragraph, a wave map) between the heading and the
fence; the old "only blank lines permitted in between" rule silently
MALFORMED every such plan. The adjacency check was never load-bearing for
disambiguation — exactly-one-fence-in-the-whole-document is already enforced
before position is examined — so containment is a pure widening, not a
weakening: a genuinely misplaced fence (in a later, different section) is
still MALFORMED.

Spec backlinks:
  coordinator/bin/coordinator-harvest-deferrals (DoE-claude, lines 317-372)
"""
from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple, Pattern


DEFAULT_HEADING = "Tasks"
DEFAULT_INFO_STRING = "yaml plan-tasks"

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)


class LocateStatus(str, Enum):
    """Discriminates the outcome of locating a fenced block.

    Negative-spec: `ABSENT` (zero matching fenced blocks anywhere in the
    document, post-comment-blanking) and `MALFORMED` (more than one matching
    fenced block, or a heading with no matching fence located inside its
    section) are intentionally distinct statuses, not collapsed into one
    falsy/None outcome — a caller creating the block on first use needs to
    tell "nothing here yet" apart from "something here is broken."
    """

    LOCATED = "located"
    ABSENT = "absent"
    MALFORMED = "malformed"


class LocateResult(NamedTuple):
    """Result of locating a fenced block under a heading.

    Negative-spec: `span` is the fence-BODY span (`match.span(1)`), NOT the
    whole-fenced-block span — it excludes the opener line (e.g.
    ```` ```yaml plan-tasks\\n ````) and the closer (`\\n``` `). A caller
    splicing new content back into the source replaces `source[start:end]`
    and the surrounding fence markers survive untouched. `body` and `span`
    are populated only when `status is LocateStatus.LOCATED`; both are
    `None` for `ABSENT`/`MALFORMED`.
    """

    status: LocateStatus
    body: str | None
    span: tuple[int, int] | None


def _compile_heading_re(heading: str) -> Pattern[str]:
    return re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)


def _compile_fence_re(info_string: str) -> Pattern[str]:
    return re.compile(rf"```{re.escape(info_string)}\n(.*?)\n```", re.DOTALL)


def locate_fenced_block(
    source: str,
    *,
    heading: str = DEFAULT_HEADING,
    info_string: str = DEFAULT_INFO_STRING,
) -> LocateResult:
    """Locate the fenced block with the given info-string contained within
    the first `## <heading>` section in `source`.

    Parity: DoE `_locate_tasks_block` (`coordinator-harvest-deferrals:317-372`).
    Exactly one fenced block with the given info-string must exist ANYWHERE
    in the document (post-comment-blanking, see module docstring), and it
    must live INSIDE the heading's section — bounded at the next
    `^##\\s+\\S` heading, or end of document — for the result to be
    `LOCATED`. Zero such blocks -> `ABSENT`. More than one such block, a
    missing heading, or a heading whose section contains no matching fence
    -> `MALFORMED`.
    """
    fence_re = _compile_fence_re(info_string)
    heading_re = _compile_heading_re(heading)

    scan_text = _HTML_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), source)

    all_fences = fence_re.findall(scan_text)
    if len(all_fences) == 0:
        return LocateResult(status=LocateStatus.ABSENT, body=None, span=None)
    if len(all_fences) > 1:
        return LocateResult(status=LocateStatus.MALFORMED, body=None, span=None)

    heading_match = heading_re.search(scan_text)
    if heading_match is None:
        return LocateResult(status=LocateStatus.MALFORMED, body=None, span=None)

    after_heading = scan_text[heading_match.end():]

    next_heading = _NEXT_HEADING_RE.search(after_heading)
    section_text = after_heading[: next_heading.start()] if next_heading else after_heading

    fence_match = fence_re.search(section_text)
    if fence_match is None:
        return LocateResult(status=LocateStatus.MALFORMED, body=None, span=None)

    # Offsets computed against scan_text are identical to offsets against
    # source (comment blanking is length-preserving), so re-slice the
    # ORIGINAL source with the same span to return the un-blanked body.
    body_start, body_end = fence_match.span(1)
    offset = heading_match.end()
    span = (offset + body_start, offset + body_end)
    return LocateResult(status=LocateStatus.LOCATED, body=source[span[0]:span[1]], span=span)
