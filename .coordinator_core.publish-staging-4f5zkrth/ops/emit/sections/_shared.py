"""Shared constants and helpers for the emit section porters.

Contains symbols used by more than one section porter to prevent silent drift on the
shared ReviewTrail quarantine contract. All three sources of truth (the bash oracle, the
review_trail section porter, and the rollups section porter) must stay in sync; centralising
them here means a single edit propagates automatically.

Exports:
  normalize_frontmatter   — shared ``frontmatter`` shape guard (list/non-dict → {}).
  run_git                 — public git subprocess helper (coordinator_roots + branch).
  human_axis_vendored     — the C9 activation switch for the human_* wire fields.
  _VERDICT_MAP            — verdict case-normalisation dict (review_trail + rollups).
  _TIME_SEG_RE            — filename timestamp-segment matcher (review_trail + rollups).
  _validate_review_trail_file — shared quarantine filter returning (record_dict, reason).

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b
Spec backlink (human_axis_vendored): docs/plans/2026-08-19-the-tracker-names-an-owner.md § C9
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.machine_resolver import registry_get

# C9 activation switch: whether the downstream consumer (opticon) has re-vendored a
# cockpit-contract carrying the human_* axis. Default OFF (unresolved key -> False) —
# see this function's own docstring for why a flag is owed at all before this key
# reaches a `.strict()` wire.
_HUMAN_AXIS_VENDORED_REGISTRY_KEY = "cockpit.human_axis_vendored"
_TRUTHY_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})


def human_axis_vendored() -> bool:
    """Whether the machine-local registry declares the human_* wire axis activated.

    Single source of truth for the C9 activation switch every ``human_*``-populating
    section (``handoffs.py``, ``trackers.py``) gates on. Reads the dotted registry key
    ``cockpit.human_axis_vendored`` via ``machine_resolver.registry_get`` — the same
    direct-tomllib reader every other registry consumer in this package binds to (see
    that function's own "Public promotion" docstring note); never the ``machine-local``
    CLI.

    Default is OFF: an unresolved key (``registry_get`` returns ``None``) or any value
    outside the small truthy set below resolves to ``False``. This is deliberate — the
    flag exists precisely so the human_* axis stays off an opticon `.strict()` wire
    until they confirm, in writing, that they have re-vendored a contract that names it
    (docs/plans/2026-08-19-the-tracker-names-an-owner.md § C9's "WHY A SWITCH" note).
    Flipping it on the reasoning that they "will have vendored by then" is exactly the
    failure mode this default forecloses.

    Flip condition: opticon confirms in writing they have re-vendored at or above the
    bumped CONTRACT_VERSION. Flipping is a one-line ``registry.local.toml`` edit
    (``machine-local set cockpit.human_axis_vendored true``), not a code change.
    """
    value = registry_get(_HUMAN_AXIS_VENDORED_REGISTRY_KEY)
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY_FLAG_VALUES


def normalize_frontmatter(rec: object) -> dict:
    """Return a record's ``frontmatter`` as a dict, or ``{}`` when it is absent or
    non-dict-shaped.

    Guards the emit section porters against a list-shaped ``frontmatter`` — the shape
    query-records.js produces when it mis-parses a ``body: |`` block whose scalar contains
    markdown bullet lines. ``rec.get("frontmatter") or {}`` is NOT sufficient: a non-empty
    list is truthy and slips through, then ``fm.get(...)`` raises AttributeError and aborts
    the whole cockpit emit (envelope.build has no per-section try/except). Normalizing to
    ``{}`` routes the record to the section's malformed/quarantine path instead of crashing.
    """
    if not isinstance(rec, dict):
        return {}
    fm = rec.get("frontmatter")
    return fm if isinstance(fm, dict) else {}


def run_git(repo_root: Path, *args: str) -> Optional[str]:
    """Run ``git -C <repo_root> <args>`` and return stripped stdout, or None on failure.

    Public git helper for section porters. Defined here so section modules import a public
    symbol rather than crossing into context._run_git (private by convention). Implementation
    is identical to context._run_git.
    """
    try:
        from coordinator_core.win_portability import no_console_creationflags

        out = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, ValueError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# Verdict case-normalization map (bash:685-695). Keys are the raw strings that map to a
# schema-valid verdict; any raw verdict not present here → quarantine (no mapping).
# Shared between review_trail.py and rollups.py — extend here when adding a new verdict.
_VERDICT_MAP = {
    "ok": "ok",
    "OK": "ok",
    "warn": "warn",
    "WARN": "warn",
    "blocked": "blocked",
    "BLOCKED": "blocked",
    "waived": "waived",
    "WAIVED": "waived",
}

# Filename time-segment matcher (bash:721): ``^(\d{6,})(?:-(.+))?$`` against the post-date rest.
# Shared between review_trail.py and rollups.py.
_TIME_SEG_RE = re.compile(r"^(\d{6,})(?:-(.+))?$")


def _validate_review_trail_file(
    filepath: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Validate one review-trail JSON file through the Section-3 quarantine filter.

    Returns ``(record_dict, None)`` on success; ``record_dict`` contains:
        reviewed_at (ISO-8601), sha_range, reviewer, verdict (normalised),
        diff_loc (int), workstream (Optional[str]).
    Returns ``(None, reason_string)`` on any quarantine condition.

    Shared between review_trail.collect and rollups._review_trail_facts so the quarantine
    rules remain in sync when the verdict set or filename format changes.

    Parity with bash SECTION 3 heredoc :697-811 / :707-745 (timestamp) / :748-760 (parse).
    """
    # Parse reviewed_at from filename (bash:707-745).
    bn = os.path.basename(filepath)
    bn_stem = bn[:-5] if bn.endswith(".json") else bn
    rt_date = bn_stem[:10] if len(bn_stem) >= 10 else "1970-01-01"
    rest = bn_stem[11:] if len(bn_stem) > 11 else ""

    m = _TIME_SEG_RE.match(rest)
    if m:
        time_digits = m.group(1)
        hh = time_digits[0:2] if len(time_digits) >= 2 else "00"
        mm = time_digits[2:4] if len(time_digits) >= 4 else "00"
        ss = time_digits[4:6] if len(time_digits) >= 6 else "00"
        # Reject decoded segments that are not a legal clock time (bash:735).
        if int(hh) > 23 or int(mm) > 59 or int(ss) > 59:
            return None, (
                f"filename timestamp segment '{time_digits}' does not encode a "
                f"valid HHMMSS time (decoded {hh}:{mm}:{ss})"
            )
        reviewed_at = f"{rt_date}T{hh}:{mm}:{ss}Z"
    else:
        reviewed_at = f"{rt_date}T00:00:00Z"

    # Read and parse the JSON body (bash:748-760).
    try:
        with open(filepath, encoding="utf-8", errors="replace") as fh:
            body = json.loads(fh.read())
    except Exception as e:  # noqa: BLE001 — parity with bash `except Exception`
        return None, f"JSON parse error: {e}"

    if not isinstance(body, dict):
        return None, "body is not a JSON object"

    sha_range = body.get("sha_range")
    reviewer = body.get("reviewer")
    raw_verdict = body.get("verdict")
    diff_loc = body.get("diff_loc", 0)

    if not isinstance(sha_range, str) or not sha_range:
        return None, "missing sha_range"
    if not isinstance(reviewer, str) or not reviewer:
        return None, "missing reviewer"
    if raw_verdict is None:
        return None, "missing verdict"
    verdict = _VERDICT_MAP.get(str(raw_verdict))
    if verdict is None:
        return None, f"invalid verdict '{raw_verdict}' (not in ok|warn|blocked|waived)"

    try:
        diff_loc_int = int(diff_loc) if diff_loc is not None else 0
    except (TypeError, ValueError):
        diff_loc_int = 0

    return {
        "reviewed_at": reviewed_at,
        "sha_range": sha_range,
        "reviewer": reviewer,
        "verdict": verdict,
        "diff_loc": diff_loc_int,
        "workstream": body.get("workstream", None),
    }, None
