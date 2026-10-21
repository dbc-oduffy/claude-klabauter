"""Builds and parses the `work:<row_id>` label used to tag a dispatch-emit row back to its originating row id.

A continuation executor (a fresh agent resuming a checkpointed row) is labelled `work:<row_id>#cont<n>`;
`parse_work_label` maps it back to the same row id.
"""

from __future__ import annotations

WORK_LABEL_PREFIX = "work:"
CONTINUATION_SEP = "#cont"


def build_work_label(row_id: str) -> str:
    return f"{WORK_LABEL_PREFIX}{row_id}"


def continuation_label_prefix(row_id: str) -> str:
    """The label of row `row_id`'s continuation executors up to the continuation number."""
    return f"{build_work_label(row_id)}{CONTINUATION_SEP}"


def continuation_label(row_id: str, n: int) -> str:
    """The label of the n-th (1-based) continuation executor of row `row_id`."""
    return f"{continuation_label_prefix(row_id)}{n}"


def parse_work_label(label: str) -> str | None:
    if not label.startswith(WORK_LABEL_PREFIX):
        return None
    row_id = label[len(WORK_LABEL_PREFIX) :]
    head, sep, tail = row_id.rpartition(CONTINUATION_SEP)
    if sep and tail.isdigit():
        row_id = head
    return row_id or None
