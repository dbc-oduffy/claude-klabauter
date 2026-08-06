"""
coordinator_core.orient_assemble.readers_clean_ops — C2a reader port: EM
environment drift, addon/doctor health, cross-repo memo surfacing,
Example-retrieval-repo staleness, and agent-worktree sweep.

Purpose: import each reader's READ-ONLY compute AS-IS (no extraction, no
rewrite — these five are already clean, in-process-importable modules per
`docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md` § Reader-
in-process port scoping, chunk C2a) and translate the result into the
shared decision-object shape: every would-be mutation becomes a
`directives[]` entry (plain dicts, mirroring `coordinator_core.
pickup_assemble`'s directive shape — no separate shipped directive
constructor exists), every open human branch becomes a `judgment_points[]`
entry built via the shipped `contract/decision_object/judgment.py`
constructors (`build_judgment_point`/`build_disposition`).

Cadence tunes severity/depth only (day = --red-and-stale; session/week =
--red-only, over the SAME addon-health reader call) — never a branch into
a different reader call per cadence (Approach § "Cadence is a parameter,
not three code paths").

Spec backlink: docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md, chunk C2a

Negative-spec:
    - Does NOT call any mutating entrypoint of the ported readers — no
      `--reap`, no sentinel writes, no memo status flips, no git fetch.
      Every reader call below is the read-only/report-only path of its
      source module (`agent_worktree_sweep.classify_worktree` classifies
      without calling `_remove_worktree`/`_delete_branch_best_effort`/
      `_cherry_pick_with_env`; addon-health's `_run` never writes a
      sentinel; the memo surfacer never flips `status:`).
    - Does NOT re-implement `resolution/facade.py`'s config resolution —
      none of these five readers need it (they resolve their own roots via
      env/settings-home, matching the un-ported CLI shape).
    - Does NOT wire these results into `brief()`. `__init__.py`'s cadence
      dispatch is shared write-surface across C2a-C2d (the plan's own
      "same package — serial, write-overlap" note); this chunk lands
      alongside concurrently-dispatched sibling reader ports, so wiring
      `collect()` into `brief()` is left to a follow-up integration pass
      rather than risked as a concurrent edit to the shared file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.ops.check_em_environment import (
    _latest_model,
    _resolve_effort,
    _resolve_transcript,
)
from coordinator_core.orient_assemble.reader_result import (
    ReaderResult,
    cap_judgment_points,
    truncate_external_text,
)
from coordinator_core.plugin_health.scan import _run as _scan_addon_health_run
from coordinator_core.ops.workday_start_cross_repo_memo_surface import (
    _list_qualifying_lines,
    _resolve_inbox_dir,
)
from coordinator_core.ops.check_rag_state import check_rag_state
from coordinator_core.ops.agent_worktree_sweep import (
    _active_branch as _wt_active_branch,
    _is_agent_worktree,
    _list_worktrees,
    _repo_root as _wt_repo_root,
    classify_worktree,
)

#: addon-health severity modes this family's `collect()` cadence-maps onto —
#: mirrors the CLI's own two live modes (`--check-sentinel-presence` is a
#: fresh-install bootstrap probe, not a per-cadence orient concern, and is
#: intentionally not wired here).
#:
#: Default-direction convention (Review: code-reviewer — Finding 6, nit): this
#: map's own `collect()` call-site default (`.get(cadence, "--red-only")`)
#: and `_MEMO_SURFACE_MODE_BY_CADENCE`'s call-site default
#: (`.get(cadence, "surface")`) below both fail an UNRECOGNIZED cadence
#: toward the MORE-VERBOSE mode, never toward suppression — "show more" is
#: the safe failure direction (never a silent KeyError, never a silent
#: under-report), and it is deliberate parallelism between the two maps, not
#: a coincidence. A future map added alongside these two should default the
#: same direction.
_ADDON_HEALTH_MODE_BY_CADENCE = {
    "day": "--red-and-stale",
    "session": "--red-only",
    "week": "--red-only",
}

#: Memo-surface behaviour by cadence — mirrors `_ADDON_HEALTH_MODE_BY_CADENCE`'s
#: cadence→behaviour map shape (Approach § "cadence-parameterized reader
#: behaviour", not a second, parallel dispatch mechanism). `~/.claude/CLAUDE.md`
#: ruling (2026-07-30): "The cross-repo memo inbox doesn't move without
#: deliberate Claude+human action. Depth is not a backlog and waiting memos
#: are not overdue work — don't report the count." At `session` cadence this
#: family emits ZERO judgment points and NO depth count anywhere — not a
#: summary line, not an "N pending" aggregate. `day` (where `/workday-start`
#: Step 1.45's blitz escalation lives) and `week` keep surfacing memos,
#: capped per `_MEMO_JUDGMENT_POINT_CAP`.
_MEMO_SURFACE_MODE_BY_CADENCE = {
    "day": "surface",
    "session": "suppress",
    "week": "surface",
}

#: Named cap on the memo-surface family's per-item judgment-point list — see
#: `reader_result.cap_judgment_points`. Unbounded per-inbound-memo lists were
#: the majority contributor (~91 of 148 JPs) to a 124KB `brief('session')`
#: payload before this cap existed.
_MEMO_JUDGMENT_POINT_CAP = 15


#: Per-character complement base for `_inverted_date` — one past `"9"`, the
#: highest code point an ISO-8601 date's digits or `"-"` separator can take.
#: Complementing each character against it reverses lexicographic order for
#: that field, letting a single ascending `sorted()` express "band ascending,
#: date descending" without a second sort pass or a `functools.cmp_to_key`.
_INVERTED_DATE_SENTINEL = ":"


def _inverted_date(created: str) -> str:
    """Return `created` transformed so ascending lexicographic order over the
    result is DESCENDING chronological order over the input — most-recent
    first. Empty/malformed input inverts to a value sorting after every real
    date, so a memo with no `created` field lands at the bottom of its band
    rather than spuriously at the top."""
    return "".join(chr(ord(_INVERTED_DATE_SENTINEL) - ord(ch)) for ch in created)


def _memo_cap_sort_key(line: str) -> tuple[str, str]:
    """Sort key for capping: band first, then recency-descending within a
    band, so `cap_judgment_points` withholds the least-urgent memos rather
    than an arbitrary tail (Review: code-reviewer — Finding 5: a cap over an
    order with no priority signal can silently hide the most urgent item
    behind a routine one).

    `line` is `_qualify_memo`'s pipe-joined `"<band_rank>|<created>|<sender>|
    <title>|<kind>"` shape — index 0 is `band_rank` (`"0"` action-required,
    `"1"` fyi), index 1 is `created`. Band ASCENDING is load-bearing and
    must not be collapsed into a recency-only key: an fyi memo created today
    outranking an action-required memo from last week is exactly the
    silent-withholding this cap ordering exists to prevent. Recency is the
    tiebreak WITHIN a band, inverted via `_INVERTED_DATE_SENTINEL` so one
    ascending `sorted()` expresses both directions.

    Falls back to the raw line as band with an empty date when it doesn't
    split into at least two `|`-parts (e.g. a fixture string in a test) so
    sorting never raises on malformed input."""
    parts = line.split("|", 4)
    if len(parts) < 2:
        return (line, "")
    band, created = parts[0], parts[1]
    return (band, _inverted_date(created))


def _read_em_environment() -> ReaderResult:
    """EM effort/model drift — a human branch (pin effort / switch model),
    never a mutation this module performs itself."""
    judgment_points: list[dict[str, Any]] = []

    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    user_claude = Path(home) / ".claude" if home else Path(".claude")
    proj = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or os.getcwd())

    effort, effort_source = _resolve_effort(proj, user_claude)
    effort_warn = ""
    if not effort:
        effort_warn = "unpinned"
    elif effort != "medium":
        effort_warn = effort

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    transcript = _resolve_transcript("", user_claude, session_id)
    current_model = _latest_model(Path(transcript)) if transcript else ""
    model_warn = current_model if current_model and "opus" not in current_model else ""

    if effort_warn:
        judgment_points.append(
            build_judgment_point(
                {
                    "disposition": "pin_effort_medium",
                    "rationale": "medium is the cost-calibrated default for EM work",
                },
                id="j-em-env-effort",
                question=f"EM effort is {effort_warn!r}, not 'medium' — pin it?",
                dispositions=[
                    build_disposition("pin_effort_medium"),
                    build_disposition("leave_as_is"),
                ],
                evidence=f"effort={effort_warn!r} source={effort_source}",
                reason=(
                    "unpinned/non-medium effort silently inflates cost as "
                    "Anthropic's default drifts upward"
                ),
            )
        )
    if model_warn:
        judgment_points.append(
            build_judgment_point(
                {
                    "disposition": "switch_to_opus",
                    "rationale": "transcript shows a non-Opus model for EM work",
                },
                id="j-em-env-model",
                question=f"Transcript shows model {model_warn!r}, not Opus — switch?",
                dispositions=[
                    build_disposition("switch_to_opus"),
                    build_disposition("leave_as_is"),
                ],
                evidence=f"model={model_warn!r}",
                reason="EM work is expected to run on Opus",
            )
        )
    return ReaderResult(judgment_points=judgment_points)


def _read_addon_health(mode: str) -> ReaderResult:
    """Addon/doctor health scan — every RED/AMBER/stale/absent/missing-hook
    line names a would-be remediation action (`/{plugin}:doctor`), so each
    becomes a directive, never a judgment point (no open human question)."""
    directives: list[dict[str, Any]] = []
    lines, _exit_code = _scan_addon_health_run(mode)
    for idx, line in enumerate(lines):
        directives.append(
            {
                "id": f"d-addon-health-{idx + 1}",
                "cli": "scan-addon-health",
                "args": [mode],
                "depends_on": None,
                "already_satisfied": False,
                "detail": line,
            }
        )
    return ReaderResult(directives=directives)


def _read_memo_surface(mode: str) -> ReaderResult:
    """Inbound cross-repo memo staleness — an open human branch (Accept /
    Decline / Surface-to-PM), never silently auto-resolved.

    `mode="suppress"` (session cadence) emits ZERO judgment points and NO
    depth count anywhere — per the 2026-07-30 CLAUDE.md ruling, memo-inbox
    depth is not backlog and is never reported as a count, not even as one
    summarizing judgment point in place of the per-memo ones. `mode="surface"`
    (day/week cadence) emits the existing per-memo judgment points, capped
    via `cap_judgment_points`.
    """
    if mode == "suppress":
        return ReaderResult()

    inbox_dir = _resolve_inbox_dir()
    if not os.path.isdir(inbox_dir):
        return ReaderResult()

    judgment_points: list[dict[str, Any]] = []
    for idx, line in enumerate(
        sorted(_list_qualifying_lines(inbox_dir), key=_memo_cap_sort_key)
    ):
        line = truncate_external_text(line)
        judgment_points.append(
            build_judgment_point(
                None,
                id=f"j-memo-{idx + 1}",
                question=f"Inbound cross-repo memo pending action: {line}",
                dispositions=[
                    build_disposition("accept"),
                    build_disposition("decline"),
                    build_disposition("surface_to_pm"),
                ],
                evidence=(
                    f"{line} | reason: an inbound memo ask is never silently "
                    "queued — the exits are Accept / Decline / Surface-to-PM"
                ),
                reason="recommendation-forbidden",
            )
        )
    judgment_points = cap_judgment_points(
        judgment_points,
        cap=_MEMO_JUDGMENT_POINT_CAP,
        overflow_id="j-overflow-memo",
        item_label="inbound cross-repo memos",
        list_command="workday-start-cross-repo-memo-surface",
    )
    return ReaderResult(judgment_points=judgment_points)


def _read_rag_staleness() -> ReaderResult:
    """example-retrieval-repo staleness token — stale/unknown implies a repomap
    regeneration is due; a directive, not an open branch (the gating rule
    is deterministic per `repomap-rag-gating.md`)."""
    token, _exit_code = check_rag_state()
    if token not in ("stale", "unknown"):
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-rag-staleness-regen",
                "cli": "generate-repomap",
                "args": [],
                "depends_on": None,
                "already_satisfied": False,
                "detail": (
                    f"example-retrieval-repo state={token!r} — regenerate repomap. "
                    "`generate-repomap` regenerates the repomap only; it is "
                    "not a substitute for the full `/update-docs` skill."
                ),
            }
        ]
    )


def _read_worktree_sweep() -> ReaderResult:
    """Agent-worktree classification (no `--reap`): reapable states become
    directives naming the existing `agent-worktree-sweep --reap` CLI;
    non-benign dirty worktrees become a judgment point (PM must handle,
    never auto-reaped)."""
    repo_root_str = _wt_repo_root()
    if not repo_root_str:
        return ReaderResult()
    repo_root = Path(repo_root_str)
    compare_ref = _wt_active_branch(repo_root)
    if not compare_ref:
        return ReaderResult()

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    worktrees = [w for w in _list_worktrees(repo_root) if _is_agent_worktree(w.path)]
    for idx, wt in enumerate(worktrees):
        classification = classify_worktree(wt.path, compare_ref)
        if classification.state in ("empty-clean", "dirty-benign", "commits-clean"):
            directives.append(
                {
                    "id": f"d-worktree-reap-{idx + 1}",
                    "cli": "agent-worktree-sweep",
                    "args": ["--reap"],
                    "depends_on": None,
                    "already_satisfied": False,
                    "detail": f"{wt.path} state={classification.state}",
                }
            )
        else:
            judgment_points.append(
                build_judgment_point(
                    None,
                    id=f"j-worktree-dirty-{idx + 1}",
                    question=(
                        f"Agent worktree {wt.path} has non-benign dirty "
                        "state — how to handle?"
                    ),
                    dispositions=[
                        build_disposition("pm_reviews_manually"),
                        build_disposition("leave_for_now"),
                    ],
                    evidence=(
                        f"path={wt.path} state={classification.state} "
                        f"dirty_count={classification.dirty_count} | "
                        "reason: dirty worktrees outside the benign allowlist "
                        "are never auto-reaped"
                    ),
                    reason="recommendation-forbidden",
                )
            )
    return ReaderResult(directives=directives, judgment_points=judgment_points)


def collect(cadence: str) -> ReaderResult:
    """Compute this reader family's directives/judgment_points for `cadence`.

    Cadence tunes two independent knobs — the addon-health severity mode
    (day = red-and-stale; session/week = red-only) and the memo-surface
    mode (session = suppress; day/week = surface, capped) — the same five
    reader calls run for every cadence; nothing here branches into a
    different reader per cadence.
    """
    addon_mode = _ADDON_HEALTH_MODE_BY_CADENCE.get(cadence, "--red-only")
    memo_mode = _MEMO_SURFACE_MODE_BY_CADENCE.get(cadence, "surface")

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in (
        _read_em_environment(),
        _read_addon_health(addon_mode),
        _read_memo_surface(memo_mode),
        _read_rag_staleness(),
        _read_worktree_sweep(),
    ):
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
