"""External-original read gate: a row citing a claude.ai artifact or design board is met only when
verify names the original itself in `originals_read`; a capture on disk is not the original."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit.design_conformance import _BOARD_LINE

_ARTIFACT = re.compile(r"(?:https?://)?claude\.ai/artifact/[A-Za-z0-9_-]+")
_TRAILING = ".,;:)]>\"'"


def normalise(url: str) -> str:
    """Fold scheme, case and trailing slash so two spellings of one original compare equal."""
    s = url.strip().rstrip(_TRAILING + "/")
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    return s.lower()


def cited_originals(text: str) -> list[str]:
    """Distinct external originals cited in `text` (artifact URLs, then the `Board:` URL), in order."""
    found: list[str] = []
    seen: set[str] = set()
    cands = [m.group(0) for m in _ARTIFACT.finditer(text or "")]
    cands += [m.group(1).rstrip(_TRAILING) for m in _BOARD_LINE.finditer(text or "")]
    for cand in cands:
        key = normalise(cand)
        if key and key not in seen:
            seen.add(key)
            found.append(cand)
    return found


def _parse(stamp: object) -> Optional[datetime]:
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        moment = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def capture_age_s(item: Mapping) -> Optional[int]:
    """Seconds the original was edited after the capture (`original_last_edited_at` minus
    `capture_harvested_at`); None when either stamp is missing or unparseable."""
    captured = _parse(item.get("capture_harvested_at"))
    edited = _parse(item.get("original_last_edited_at"))
    if captured is None or edited is None:
        return None
    return max(0, int((edited - captured).total_seconds()))


def _read_set(item: Mapping) -> set[str]:
    raw = item.get("originals_read")
    names = [raw] if isinstance(raw, str) else [n for n in raw or [] if isinstance(n, str)]
    return {normalise(n) for n in names}


def read_refusal(row_id: str, row_text: str, item: Mapping) -> Optional[dict]:
    """None when every original cited in `row_text` is named in `item["originals_read"]`; else
    the BLOCKED record for the first unread one, with capture age when knowable."""
    read = _read_set(item)
    for url in cited_originals(row_text):
        if normalise(url) in read:
            continue
        age = capture_age_s(item)
        age_text = "capture age unknowable" if age is None else f"capture age {age}s"
        return {
            "status": "BLOCKED",
            "reason": (
                f"BLOCKED: unreachable-original {url}: row {row_id}: verify evidence shows no read "
                f"of the original; {age_text}"
            ),
            "capture_age_s": age,
        }
    return None
