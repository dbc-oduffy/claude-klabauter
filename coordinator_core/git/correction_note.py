"""Correction-note contract for commits whose subject was damaged and amended by `git notes`.

A note is machine-readable when its first non-blank line is `Corrected-Subject: <subject>`;
anything after is free-form. Stdlib only, no I/O.
"""

from __future__ import annotations

from typing import Optional

SENTINEL = "Corrected-Subject:"


def format_note(subject: str, body: str = "") -> str:
    """Render a note: the sentinel line carrying `subject`, then an optional free-form body."""
    note = f"{SENTINEL} {subject}"
    return f"{note}\n{body}" if body else note


def parse_note(note: str) -> Optional[str]:
    """Return the corrected subject, or None when the first non-blank line lacks the sentinel."""
    for line in (note or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith(SENTINEL):
            return None
        subject = line[len(SENTINEL):].strip()
        return subject or None
    return None
