"""
coordinator_core.backlog_grind_assemble.readers_dogfood — C3e reader: dogfood's
loop-controller surface. Exposes `collect(cadence) -> ReaderResult`, self-gated
to `cadence == "dogfood"` — backlog-grind's five cadence values are the five
mirror-surface names themselves (see `directives.py`'s module docstring and
`test_backlog_grind_assemble.py::_CADENCES`), not orient_assemble's
session/day/week severity knob.

Purpose, per docs/plans/2026-07-26-backlog-grind-computed-frontage.md, chunk
C3e: `/dogfood` (DoE-claude `coordinator/skills/dogfood/SKILL.md`) is a
loop-controller, structurally closer to a state machine than a triage
pipeline — its six gate types are ALL judgment and stay in the skill surface
(D-1's own gate-topology corroboration cites dogfood as "highest gate
density, 6 types"). This reader's job is scoped to the one signal SKILL.md
itself already calls mechanical: the § Per-Iteration Mode Artifact
`pass-<N>-modes.json` failure-mode tuple count ("EM no longer needs to
judge subjectively — the JSON is the signal"), plus its paired
stuck-detection check (same tuple set unchanged from the prior pass, § Loop
Exit / § Switch-Gears Protocol). Both are pure functions of on-disk
flight-recorder state; this module computes the STATE (threshold crossed /
stuck / neither), never the DECISION — proposing switch-gears is
EM-proposed, PM-confirmed per SKILL.md § Switch-Gears Protocol, and this
reader never emits that recommendation.

Session discovery: scans `tasks/` for `dogfood-<target>-<date>/`
flight-recorder directories (SKILL.md § Flight Recorder Directory) and reads
the most-recently-modified one — SKILL.md's own single-committer framing
(`--narrow`: "EM commits each fix directly"; `--broad`/`--shakedown`: "The EM
serializes commits at wave gates") describes one active loop at a time, so
"most recent" is the live session, never a cross-session concatenation.

Spec backlink: DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C3e; DoE-claude `coordinator/skills/dogfood/SKILL.md` § Per-Iteration
Mode Artifact / § Switch-Gears Protocol / § Flight Recorder Directory
(read-only source for this chunk — not edited here; C11 owns that file).

Negative-spec:
    - Does NOT read any of the three backlog queues (bug-backlog,
      debt-backlog, improvement-queue) — dogfood has no queue surface, so
      AC6 ("every queue read routes through `queue_family.load_family_records`")
      is satisfied by this file containing no queue read at all, not by an
      unrouted one.
    - Does NOT call the glob method on a Path object anywhere — AC6's own
      grep pattern matches that call shape regardless of what it scans, so
      even a non-queue `tasks/`-directory glob would trip it. Session/
      pass-file discovery below uses `iterdir()` + a filename regex instead.
    - Does NOT decide switch-gears, propose which disposition to take, or
      attach a `recommendation` — SKILL.md § Switch-Gears Protocol keeps
      that EM-proposed/PM-confirmed judgment call in the skill body; this
      reader surfaces the mechanical trigger fact only, with
      `recommendation=None` (`build_judgment_point`'s required-but-nullable
      first argument).
    - Does NOT compute the skip-ratio threshold (SKILL.md § Cone
      Classification: "if skip-ratio exceeds 40% ... or >2 consecutive
      skips"). The flight recorder has no on-disk denominator for total
      observations — only `surface-and-skip.md`'s skip entries themselves —
      so fabricating a ratio against an unmodeled denominator would be
      inventing state the disk doesn't carry, the exact failure mode this
      plan's C3e brief warns against ("never invent a path or an API").
      Left as skill-side judgment, unchanged by this chunk.
    - Does NOT reshape another reader's `ReaderResult`; returns only its own.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.orient_assemble.reader_result import ReaderResult

__all__ = ["collect"]

#: This file lives at coordinator_core/backlog_grind_assemble/readers_dogfood.py
#: — parents[2] is the makima repo root, mirroring
#: orient_assemble/readers_health_reaper.py's identical depth calculation.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASKS_DIR = _REPO_ROOT / "tasks"

_SESSION_DIR_PREFIX = "dogfood-"
_PASS_MODES_RE = re.compile(r"^pass-(\d+)-modes\.json$")

#: SKILL.md § Per-Iteration Mode Artifact: "≥3-4 distinct {component,
#: error-class} tuples in a single pass -> switch-gears is warranted."
#: Pinned at the floor of that range (3) so the mechanical signal fires at
#: the earliest point the skill's own prose licenses it — any judgment
#: about where exactly in the 3-4 band to act stays in the skill body, not
#: this reader.
_FAILURE_MODE_SHIFT_THRESHOLD = 3


def _find_active_session_dir() -> Optional[Path]:
    """Most-recently-modified `tasks/dogfood-<target>-<date>/` directory, or
    `None` if `tasks/` doesn't exist or carries no dogfood session dir."""
    if not _TASKS_DIR.is_dir():
        return None
    candidates = [
        entry
        for entry in _TASKS_DIR.iterdir()
        if entry.is_dir() and entry.name.startswith(_SESSION_DIR_PREFIX)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _pass_tuple_set(pass_path: Path) -> frozenset:
    """Parse one `pass-<N>-modes.json` file into its distinct
    `(component, error-class)` tuple set. A malformed/unreadable file reads
    as an empty set rather than raising — a corrupt flight-recorder file is
    evidence for a human to look at, not a reason for this read-only reader
    to crash `brief()`."""
    try:
        raw = json.loads(pass_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(raw, list):
        return frozenset()
    tuples: set = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        component = entry.get("component")
        error_class = entry.get("error-class")
        if isinstance(component, str) and isinstance(error_class, str):
            tuples.add((component, error_class))
    return frozenset(tuples)


def _numbered_pass_files(session_dir: Path) -> list:
    """`(N, path)` pairs for every `pass-<N>-modes.json` in `session_dir`,
    sorted ascending by N. Uses `iterdir()` + a filename regex rather than
    a Path glob call — see the module negative-spec."""
    numbered: list = []
    for entry in session_dir.iterdir():
        if not entry.is_file():
            continue
        match = _PASS_MODES_RE.match(entry.name)
        if match:
            numbered.append((int(match.group(1)), entry))
    numbered.sort(key=lambda pair: pair[0])
    return numbered


def _mechanical_switch_gears_signal(session_dir: Path) -> ReaderResult:
    """SKILL.md § Per-Iteration Mode Artifact's mechanical switch-gears
    signal: the latest pass's distinct-tuple count crossing
    `_FAILURE_MODE_SHIFT_THRESHOLD`, or the latest pass's tuple set being
    unchanged from the prior pass (stuck-detection, § Switch-Gears
    Protocol "Stuck-detection"). Either condition surfaces ONE trusted
    judgment point naming the mechanical fact — `recommendation=None`,
    since proposing switch-gears itself stays EM-proposed/PM-confirmed
    judgment per SKILL.md § Switch-Gears Protocol, never this reader's
    call."""
    passes = _numbered_pass_files(session_dir)
    if not passes:
        return ReaderResult()

    latest_n, latest_path = passes[-1]
    latest_tuples = _pass_tuple_set(latest_path)
    threshold_crossed = len(latest_tuples) >= _FAILURE_MODE_SHIFT_THRESHOLD

    stuck = False
    prior_n = None
    if len(passes) >= 2:
        prior_n, prior_path = passes[-2]
        prior_tuples = _pass_tuple_set(prior_path)
        stuck = bool(latest_tuples) and latest_tuples == prior_tuples

    if not threshold_crossed and not stuck:
        return ReaderResult()

    reasons = []
    if threshold_crossed:
        reasons.append(
            f"pass-{latest_n}-modes.json carries {len(latest_tuples)} distinct "
            f"{{component, error-class}} tuples (threshold "
            f"{_FAILURE_MODE_SHIFT_THRESHOLD})"
        )
    if stuck:
        reasons.append(
            f"pass-{latest_n}-modes.json's tuple set is unchanged from "
            f"pass-{prior_n}-modes.json — the loop is stuck on the same "
            "failure mode(s)"
        )

    jp = build_judgment_point(
        None,
        id="j-dogfood-mechanical-switch-gears-signal",
        question=(
            "Mechanical switch-gears signal fired (see evidence) — propose "
            "switch-gears to the PM, or continue the fix-through loop?"
        ),
        dispositions=[
            build_disposition("propose_switch_gears", resolves=[]),
            build_disposition("continue_loop", resolves=[]),
        ],
        evidence=f"session={session_dir.name} | " + "; ".join(reasons),
        reason="recommendation-forbidden",
    )
    return ReaderResult(judgment_points=[jp])


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    """Compute dogfood's loop-controller signal for `cadence`. Self-gates:
    non-empty only when `cadence == "dogfood"` — the seam (C3) calls every
    reader unconditionally for every cadence and trusts each to self-gate.
    Backlog-grind's `cadence` is one string per mirror surface
    (`bug-blitz`/`mise-en-place`/`bug-sweep`/`debt-triage`/`dogfood`), not
    orient_assemble's session/day/week severity knob — see `directives.py`'s
    module docstring.

    `run_id` names which run of the ASKING surface is asking. This reader
    has no per-run record family to resolve it against, so it accepts the
    parameter and ignores it: the seam threads it to all five readers
    uniformly for every cadence (`__init__.py`'s negative-spec against a
    per-surface branch), and self-gating on it is each reader's own job,
    exactly as self-gating on `cadence` is.
    """
    if cadence != "dogfood":
        return ReaderResult()

    session_dir = _find_active_session_dir()
    if session_dir is None:
        return ReaderResult()

    return _mechanical_switch_gears_signal(session_dir)
