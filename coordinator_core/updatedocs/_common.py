"""
coordinator_core.updatedocs._common — plumbing shared by all four detectors.

Purpose: the one exception shape and the one bounded frontmatter-head-reading
strategy common to `readme_index`, `directory_md`, `plan_prune`, and
`memo_prune`. Pulled out because all four raise a byte-identical
missing-target exception (only the message text differed) and three of the
four independently needed to read a file's leading bytes to find its
frontmatter block — one of them (`readme_index`) had re-derived
`split_frontmatter` by hand rather than importing the sibling that already
bounded the read.

No writes, no `GateResult` — same negative spec as every module in this
package; the gate layer (`coordinator_core.ops.updatedocs_gates`) is the only
place `UpdatedocsTargetMissing` becomes `GateVerdict.UNAVAILABLE`.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa
"""
from __future__ import annotations

import re
from pathlib import Path


class UpdatedocsTargetMissing(Exception):
    """Raised when a required target path does not exist or cannot be read.

    Carries `missing_path` so the gate layer can report exactly which path
    was absent, and can convert this into `GateVerdict.UNAVAILABLE` rather
    than a bare exception bubbling up or a silently CLEAN empty result --
    "we looked at nothing" and "we found nothing" must reach the caller as
    different states. Shared by all four updatedocs detectors; no handler in
    the gate layer branches on which one raised it, so one class is all four
    ever needed.
    """

    def __init__(self, missing_path: Path) -> None:
        self.missing_path = missing_path
        super().__init__(
            f"updatedocs: required path not found or unreadable: {missing_path}"
        )


# Bytes read from the head of each file when looking for its frontmatter
# block, and the growth ceiling applied when the closing `---` hasn't
# appeared yet (a long HTML-comment preamble can push the real frontmatter
# past the first chunk -- growing avoids misreading a genuine status as
# absent, which would wrongly inflate `indeterminate`).
_HEAD_READ_BYTES = 8192
_HEAD_READ_MAX_BYTES = 65536

# A frontmatter delimiter line, standalone on its own line -- distinct from
# an incidental run of dashes inside a markdown table/rule that can appear
# well before the real closing delimiter and falsely look like "found it".
_FM_DELIMITER_LINE = re.compile(rb"(?m)^---[ \t]*\r?$")


def read_head(p: Path) -> str:
    """Read the bounded head of `p`, growing once (to `_HEAD_READ_MAX_BYTES`)
    if a closing frontmatter delimiter hasn't appeared within the first
    `_HEAD_READ_BYTES`. Never raises -- returns "" on any OSError.
    """
    try:
        with p.open("rb") as fh:
            raw = fh.read(_HEAD_READ_BYTES)
            if len(_FM_DELIMITER_LINE.findall(raw)) < 2 and len(raw) == _HEAD_READ_BYTES:
                fh.seek(0)
                raw = fh.read(_HEAD_READ_MAX_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")
