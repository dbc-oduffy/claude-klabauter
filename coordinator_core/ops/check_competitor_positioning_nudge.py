"""
coordinator_core.ops.check_competitor_positioning_nudge — absent-OR-empty-triggered
competitive-positioning offer.

Purpose: shared by /workweek-start Step 4.5 and /workweek-complete Step 4j so the
trigger logic (and its decline-memory) lives in exactly one place instead of two
byte-identical copies drifting independently.

Trigger: state/strategic/self-description.yaml is absent, OR present with an empty
competitors[] array. Suppressed (design-as-offers decline-memory) when the PM has
declined within the last _COOLDOWN_DAYS days — an unconditional offer that
resurfaces unchanged twice a week reads as a nag by week 3-4, not an offer.

Port of: check-competitor-positioning-nudge.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: coordinator/DoE-claude:pln-per-repo-competitor-peer-self--f0b04e

Negative-spec (fidelity to the bash oracle — do NOT "fix" these mid-port):
    - A malformed self-description.yaml (unparseable YAML) is NOT caught — it
      propagates as an uncaught exception, non-zero exit, and a traceback on
      stderr, exactly as the bash oracle's inline `python3 -c` block does when
      run under `set -euo pipefail`. This is advisory-tool debt inherited
      verbatim, not new breakage.
    - A malformed decline-marker timestamp fails safe to "not cooling" (nudge
      resumes) — mirrors the bash `except ValueError: print(0)` branch.
    - `--record-decline` always exits 0, unconditionally, even if the directory
      write somehow fails to be observed downstream — mirrors the bash oracle
      (no error handling around `mkdir -p`/`date -u >`).
"""

from __future__ import annotations

# Generator-provenance declaration: _record_decline() writes only
# state/strategic/.positioning-nudge-declined-at, a decline-memory marker
# that is not tracked by git (untracked working state, never committed).
GENERATES = []

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.session.declared_writes import declare_write

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML is a project dependency; defensive only
    yaml = None  # type: ignore[assignment]

_SSD_YAML_REL = os.path.join("state", "strategic", "self-description.yaml")
_DECLINE_MARKER_REL = os.path.join("state", "strategic", ".positioning-nudge-declined-at")
_COOLDOWN_DAYS = 28

_NUDGE_TEXT = (
    "Want me to set your repo's peers/competitors/aspirational-targets? Makes "
    "deliverable planning easier — run the strategic self-description refresh "
    "(Skill(coordinator:strategic-self-description-refresh)), which scaffolds "
    "state/strategic/self-description.yaml if it doesn't exist yet. (Skip freely — "
    f"say no and I won't ask again for {_COOLDOWN_DAYS} days.)"
)


def _record_decline(repo_root: Path) -> None:
    marker = repo_root / _DECLINE_MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker.write_text(ts + "\n", encoding="utf-8")

    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(marker)


def _still_cooling(marker: Path) -> bool:
    """Fail-safe: absent OR malformed marker → not cooling (nudge resumes)."""
    raw = marker.read_text(encoding="utf-8").strip()
    try:
        declined = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"skip: _still_cooling: declined = datetime.strptime(raw, \"%Y-%m-%dT%H:%M:%SZ\").replace(tzinfo failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    now = datetime.now(timezone.utc)
    return (now - declined).days < _COOLDOWN_DAYS


def _competitors_empty(ssd_yaml: Path) -> bool:
    """Raises on unparseable YAML — deliberate parity with the bash oracle (see
    module negative-spec)."""
    with open(ssd_yaml, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    competitors = doc.get("competitors") or []
    return len(competitors) == 0


def check(repo_root: Optional[str] = None) -> str:
    """Return the nudge text, or "" if suppressed/not-triggered.

    repo_root: injected repo root for test isolation; defaults to cwd (mirrors
    the bash oracle's cwd-relative paths — it never resolves a git root).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    ssd_yaml = root / _SSD_YAML_REL
    decline_marker = root / _DECLINE_MARKER_REL

    if yaml is None:
        return ""

    if decline_marker.is_file() and _still_cooling(decline_marker):
        return ""

    if not ssd_yaml.is_file():
        nudge = True
    else:
        nudge = _competitors_empty(ssd_yaml)

    return _NUDGE_TEXT if nudge else ""


def main(argv: List[str]) -> int:
    """CLI entry: `--record-decline` writes the marker; otherwise prints the
    nudge (or nothing) and returns 0 — mirrors the bash oracle's never-block
    advisory-tool contract. An unparseable self-description.yaml propagates
    as an uncaught exception (see module negative-spec), matching the bash
    oracle's `set -euo pipefail` failure mode rather than returning non-zero
    cleanly."""
    repo_root = Path.cwd()

    if argv and argv[0] == "--record-decline":
        _record_decline(repo_root)
        return 0

    text = check(str(repo_root))
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
