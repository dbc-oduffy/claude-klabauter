"""coordinator_core.bounded_slug -- the one bounded filename-slug function.

Purpose: a title or goal becomes a filename basename at several sites; an
unbounded slug of a long title exceeds NAME_MAX (255 bytes) and the write fails.
Width is measured in UTF-8 bytes, because str.isalnum admits non-ASCII letters
and a character cap alone does not bound the encoded basename.

Negative-spec: does not compose ids (see artifact_id_slug) and does not add the
date prefix or extension; the caller budgets those out of NAME_MAX.
"""

from __future__ import annotations

NAME_MAX = 255


def bounded_slug(text: str, max_width: int) -> str:
    """Lower-case dash slug of `text`, at most `max_width` UTF-8 bytes.

    Cuts at a word boundary when the cap lands mid-word and one exists; never
    leads or trails with a dash; returns "untitled" for an empty result.
    """
    out: list[str] = []
    prev_dash = False
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    if len(slug.encode("utf-8")) > max_width:
        kept: list[str] = []
        used = 0
        for ch in slug:
            used += len(ch.encode("utf-8"))
            if used > max_width:
                break
            kept.append(ch)
        cut = "".join(kept)
        if "-" in cut and len(cut) < len(slug) and slug[len(cut)] != "-":
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-")
    return slug or "untitled"
