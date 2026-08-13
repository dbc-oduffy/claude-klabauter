"""
coordinator_core.ops.workday_surface_auto_push_failure_stats — JSON-RPC
"workday.surface_auto_push_failure_stats" operation.

Purpose: read-only aggregation over `.git/push-failures.log` for the
/workday-start orientation surface (fence: coordinator-claude commands/workday-start.md:954,
which computed TOTAL/RECENT_24H/LAST_LINE via wc/awk/date/tail). The bash
oracle's 24h window used GNU `date -d` with a BSD `date -v-1d` fallback —
exactly the dual-syntax shell fragility the bash-kill campaign targets.
Settlement B8 (docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md
§ B8, RATIFIES) closes it natively: window boundary =
`datetime.now(timezone.utc) - timedelta(hours=24)` (CC-2 wall-clock), each
line's own bracketed UTC timestamp parsed via `strptime` — no GNU/BSD date
fork anywhere.

Log-line shape (writer: coordinator_core/hooks/auto_push.py `_log_failure`):

    [YYYY-MM-DDTHH:MM:SSZ] PUSH FAILED on <branch> (<route>/<class> after N) :: ...

Malformed-line rule (settlement B8, EXPLICIT — a named acceptance criterion):
a line whose timestamp fails to parse counts toward `total`, is EXCLUDED from
`recent_24h`, and NEVER raises — the log is written by a hook under failure
conditions; robustness beats strictness.

Absent log → `{total: 0, recent_24h: 0, last_line: null}` — zeros, NOT an
error. Absence of failures is a valid healthy state; this log is created
lazily on first failure (asymmetric with poll_scratch_dir's
missing-dir-is-a-premise-failure rule BY DESIGN, per the settlement). The
absence carve-out covers the log file only; a `repo_root` that does not
exist on disk is a caller premise failure and fails loud (CC-7).

Idempotency (AC7, DEC-7 note): INHERENT — pure read, zero writes, zero
subprocess spawns; identical inputs return identical stats (modulo the
wall-clock window boundary, which is the op's contract, not accreted state).
The double-invocation test asserts two back-to-back calls return identical
results and leave the log untouched.

Keying: the log path is `resolve_git_common_dir(repo_root) / "push-failures.log"`,
matching the `auto_push.py` writer's own keying (`resolve_git_common_dir(repo_root)`,
2026-08-01) byte-for-byte — reader and writer resolve the same file by
construction, in every topology (plain clone, linked worktree,
`--separate-git-dir`, submodule), since both apply the identical resolver to
the same `repo_root` input. Scope `show_top`: the caller passes `repo_root`
explicitly as a required param and this handler uses that value only (the
ipc-injected `repo_root` kwarg is accepted but unused) — see the handler's
own docstring below. Registration on the three shared surfaces
(`_EAGER_OP_MODULES`, `_OP_KEY_SCOPE`, `_registry_map.py`) lands in the
EM-serial registration pass per CC-3; this module carries only its own
`register_op`.

Contract: params {repo_root: str} -> {total: int, recent_24h: int, last_line: str|null}
Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § B8
Parent plan:   docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § DEC-2

Negative-spec:
    - NO subprocess spawns of any kind — the oracle's wc/awk/date/tail
      pipeline collapses entirely to in-process Python; there is no git
      invocation here either (the path join off repo_root IS the writer's
      keying, no discovery needed).
    - Does NOT validate line content beyond the timestamp — the aggregation
      is count-shaped, not parse-shaped; PUSH-FAILED payload fields are
      opaque here.
    - Never raises on log CONTENT (malformed-line rule above); raises only
      on a missing/non-directory repo_root (premise failure).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ipc import register_op

# Matches the auto_push.py writer's bracketed-UTC-stamp line prefix:
# strftime("%Y-%m-%dT%H:%M:%SZ") inside literal square brackets.
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")

_TS_FMT = "%Y-%m-%dT%H:%M:%S"


class PushFailureStatsError(RuntimeError):
    """Structured failure for workday.surface_auto_push_failure_stats (CC-7).

    Raised only on a caller premise failure (missing/non-directory repo_root
    or a missing required param) — never on log content or log absence.
    """


def surface_auto_push_failure_stats(repo_root: str) -> dict:
    """Aggregate `.git/push-failures.log` stats (settlement B8).

    Returns {total, recent_24h, last_line} per the manifest contract:
    total     — every log line (malformed included);
    recent_24h — lines whose bracketed UTC timestamp parses AND falls within
                 the trailing 24h window (CC-2 wall-clock boundary);
    last_line  — the log's final line verbatim, or None when the log is
                 absent or empty.
    """
    root = Path(repo_root)
    if not root.is_dir():
        raise PushFailureStatsError(
            "workday.surface_auto_push_failure_stats: repo_root "
            f"{str(root)!r} does not exist or is not a directory"
        )

    log_path = resolve_git_common_dir(root) / "push-failures.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        # Absent log = healthy (created lazily on first failure) — zeros,
        # not an error, per settlement B8.
        return {"total": 0, "recent_24h": 0, "last_line": None}

    lines = text.splitlines()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    recent_24h = 0
    for line in lines:
        match = _TS_RE.match(line)
        if match is None:
            # Malformed-line rule: counts toward total (it is in `lines`),
            # excluded from recent_24h, never raises.
            continue
        try:
            stamp = datetime.strptime(match.group(1), _TS_FMT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            # Regex-shaped but calendar-invalid (e.g. month 13) — same
            # malformed-line treatment.
            continue
        if stamp >= cutoff:
            recent_24h += 1

    return {
        "total": len(lines),
        "recent_24h": recent_24h,
        "last_line": lines[-1] if lines else None,
    }


@register_op("workday.surface_auto_push_failure_stats")
def _surface_auto_push_failure_stats(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'workday.surface_auto_push_failure_stats' handler — sync.

    Params: repo_root (str, required) — the caller's repo working-tree root,
    per the manifest contract. The explicit param is authoritative (the
    ipc-injected `repo_root` kwarg is unused); `surface_auto_push_failure_stats`
    resolves the git COMMON dir from it via `resolve_git_common_dir`, matching
    the writer's own keying in every topology.
    """
    param_root = params.get("repo_root") or ""
    if not param_root:
        raise PushFailureStatsError(
            "workday.surface_auto_push_failure_stats: required param "
            "'repo_root' is missing or empty"
        )
    return surface_auto_push_failure_stats(param_root)
