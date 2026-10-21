"""Delivery state of a plan a baton cites: status, un-coded dispatchable rows, last coded commit.

Pure reader, no subprocess, no write. A plan is delivered only when its status is terminal, its
spine parses, and no dispatchable row is still open.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit.spine_read import (
    NON_DISPATCHABLE_DISPOSITIONS,
    load_rows_memo,
    with_canonical_disposition,
)

# `landed` is non-terminal per plan.schema.json (spine rows still open): do not re-add it.
# `complete` stays as defensive tolerance.
PLAN_DONE = frozenset({"implemented", "complete"})

_STATUS_RE = re.compile(r"^status:[ \t]*(.*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class PlanDelivery(NamedTuple):
    """`delivered` is True only for a terminal status, a non-MALFORMED spine and no open row."""

    status: str | None
    located: bool
    open_rows: tuple[str, ...]
    ship_ref: str | None
    delivered: bool


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    cut = raw.find(" #")
    return (raw[:cut] if cut >= 0 else raw).strip()


def _status_of(text: str) -> str | None:
    split = split_frontmatter(text)
    if split is None:
        return None
    for line in split.fm_text.splitlines():
        m = _STATUS_RE.match(line)
        if m:
            return _scalar(m.group(1)) or None
    return None


def plan_delivery(text: str) -> PlanDelivery:
    """Delivery state of the plan whose full text is `text`.

    A row is open when its disposition is not in NON_DISPATCHABLE_DISPOSITIONS and `deferred`
    is not true. `ship_ref` is the 40-hex `disposition_ref` of the last coded row in spine order.
    """
    status = _status_of(text)
    result = load_rows_memo(text)
    open_rows: list[str] = []
    ship_ref: str | None = None
    if result.status is LocateStatus.LOCATED:
        for raw in map(with_canonical_disposition, result.rows):
            if not isinstance(raw, dict):
                continue
            disposition = raw.get("disposition")
            if disposition == "coded":
                ref = str(raw.get("disposition_ref") or "").strip().lower()
                if _SHA_RE.match(ref):
                    ship_ref = ref
            if disposition not in NON_DISPATCHABLE_DISPOSITIONS and raw.get("deferred") is not True:
                open_rows.append(str(raw.get("id")))
    delivered = (
        status in PLAN_DONE
        and result.status is not LocateStatus.MALFORMED
        and not open_rows
    )
    return PlanDelivery(status, True, tuple(open_rows), ship_ref, delivered)


class PlanDeliveryCache:
    """Reads each plan once: docs/plans/<name>, then archive/specs/<yyyy-mm>/<name> newest first."""

    def __init__(self, worktree_root: str) -> None:
        self.root = worktree_root
        self._memo: dict[str, PlanDelivery | None] = {}
        self._months: list[str] | None = None

    def _archive_months(self) -> list[str]:
        if self._months is None:
            base = os.path.join(self.root, "archive", "specs")
            try:
                names = sorted(os.listdir(base), reverse=True)
            except OSError:
                names = []
            self._months = [os.path.join(base, n) for n in names]
        return self._months

    @staticmethod
    def _read(path: str) -> str | None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            chunks = []
            while True:
                buf = os.read(fd, 1 << 20)
                if not buf:
                    break
                chunks.append(buf)
        finally:
            os.close(fd)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def get(self, plan_rel: str) -> PlanDelivery | None:
        """PlanDelivery for a docs/plans/<name> rel path; None when the plan is missing everywhere."""
        rel = plan_rel.replace("\\", "/")
        if rel in self._memo:
            return self._memo[rel]
        result: PlanDelivery | None = None
        parts = rel.split("/")
        if rel and not rel.startswith("/") and ".." not in parts and ":" not in parts[0]:
            text = self._read(os.path.join(self.root, *parts))
            if text is None and rel.startswith("docs/plans/") and len(parts) == 3:
                for month in self._archive_months():
                    text = self._read(os.path.join(month, parts[2]))
                    if text is not None:
                        break
            if text is not None:
                result = plan_delivery(text)
        self._memo[rel] = result
        return result
