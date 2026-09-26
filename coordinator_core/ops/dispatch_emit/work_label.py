
from __future__ import annotations

WORK_LABEL_PREFIX = "work:"


def build_work_label(row_id: str) -> str:
    return f"{WORK_LABEL_PREFIX}{row_id}"


def parse_work_label(label: str) -> str | None:
    if not label.startswith(WORK_LABEL_PREFIX):
        return None
    row_id = label[len(WORK_LABEL_PREFIX) :]
    return row_id or None
