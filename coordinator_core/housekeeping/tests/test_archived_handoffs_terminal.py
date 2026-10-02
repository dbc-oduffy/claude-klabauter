"""Every archived handoff's `deployment_state` is terminal, except a frozen shrink-only ledger.

A handoff moved to `archive/handoffs/` while still `in_flight` or `ready_to_fire` makes the
archive claim a state the record contradicts. Records without a `deployment_state` are not
judged. The ledger names the violators that predate this test; an entry that stops violating
must leave the ledger, and nothing new may join it.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

_REPO_ROOT = Path(__file__).resolve().parents[3]

_FRONTMATTER_RE = re.compile(r"\A(?:\s*<!--.*?-->\s*)*---\n(.*?)\n---", re.S)
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")

_LEDGER = frozenset({
    "archive/handoffs/2026-07/2026-07-02_230103_roadmap-pcore-03.md",
    "archive/handoffs/2026-07/2026-07-04_220004_roadmap-strang-04.md",
    "archive/handoffs/2026-07/2026-07-04_220009_roadmap-strang-09.md",
    "archive/handoffs/2026-07/2026-07-23-realized-by-stamping-seam-stamp-at-reali.md",
    "archive/handoffs/2026-08/2026-08-27-the-discriminators-that-already-exist-reach-their-rows.md",
})


def _deployment_state(text: str) -> str | None:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    for line in match.group(1).splitlines():
        if line.startswith("deployment_state:"):
            value = _TRAILING_COMMENT_RE.sub("", line.split(":", 1)[1]).strip()
            return value.strip("'\"") or None
    return None


def _violators(root: Path) -> set[str]:
    out: set[str] = set()
    for path in sorted((root / "archive" / "handoffs").rglob("*.md")):
        state = _deployment_state(path.read_text(encoding="utf-8", errors="replace"))
        if state is not None and state not in HANDOFF_TERMINAL_DEPLOYMENT:
            out.add(path.relative_to(root).as_posix())
    return out


def test_no_archived_handoff_is_non_terminal_outside_the_ledger():
    new = _violators(_REPO_ROOT) - _LEDGER
    assert not new, f"archived handoff(s) with a non-terminal deployment_state: {sorted(new)}"


def test_ledger_only_shrinks():
    stale = _LEDGER - _violators(_REPO_ROOT)
    assert not stale, f"ledger entries that no longer violate -- remove them: {sorted(stale)}"


def test_planted_in_flight_record_is_caught(tmp_path):
    archive = tmp_path / "archive" / "handoffs" / "2026-10"
    archive.mkdir(parents=True)
    (archive / "bad.md").write_text(
        "---\ndeployment_state: in_flight  # still going\n---\nbody\n", encoding="utf-8"
    )
    (archive / "ok.md").write_text("---\ndeployment_state: shipped\n---\nbody\n", encoding="utf-8")
    (archive / "none.md").write_text("---\nstatus: consumed\n---\nbody\n", encoding="utf-8")
    assert _violators(tmp_path) == {"archive/handoffs/2026-10/bad.md"}
