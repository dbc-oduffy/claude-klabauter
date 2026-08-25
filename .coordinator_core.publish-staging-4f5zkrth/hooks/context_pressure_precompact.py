"""STAGED FOR RELOCATION — final destination:
    X:/project-makima/coordinator_core/hooks/context_pressure_precompact.py
(a confined general-purpose subagent cannot write outside the DoE-claude repo
per subagent-sandbox-policy.yaml `confined:` — this file is staged here for
the EM to `git mv`/copy into the makima working tree and commit there.)

coordinator_core.hooks.context_pressure_precompact — PreCompact sentinel +
state-serialization bookkeeping op.

Port of: context-pressure-precompact.sh (DoE d39ab164, 2026-07-16) (W4b, recipe § 2.6).

Landing convention confirmed by `08-makima-landing-contract.md § 1`: one file
per hook op under `coordinator_core/hooks/`, `snake_case.py` named after the
op (not the bash script), registered via `@register_op("hooks.<name>")` at
import time, using `_envelope.py`'s shape builders. This module follows the
`session_heartbeat.py` shape most closely (both are async bookkeeping ops
whose product is an on-disk write side-effect, not an advisory) — see that
file for the sibling pattern this one mirrors.

Called TWO ways (both in-process, no subprocess, no bash):
  1. Directly by the DoE Shape-P1 stub
     (`coordinator/hooks/scripts/context-pressure-precompact.py`), which
     drains stdin itself and calls `run(raw_stdin)` — this mirrors
     `preuse-write-dispatch.py` -> `write_guards.engine.evaluate_payload_json`,
     the cheapest-possible shape per the landing contract's Option (a): one
     python3 process spawn, zero bash, zero JSON-RPC envelope round-trip.
  2. Via `@register_op("hooks.context_pressure_precompact")` /
     `dispatch_message`, for parity with the other 11 `coordinator_core.hooks`
     ops and any future MCP/IPC-routed caller — `_handler` is a thin adapter
     over the same `run()` core.

Output is IGNORED by Claude Code for PreCompact events (stdout is not
surfaced to the model) — `_handler` therefore always returns `no_advisory()`,
matching `session_heartbeat.py`'s "the product is the write side-effect"
contract. State is bridged to context via
`coordinator_core.hooks.postuse_advisory_dispatch` (PostToolUse), which
ALREADY consumes the two files this module writes:

    {tempdir}/compaction-occurred-{session_id}     — sentinel (triggers advisory)
    {tempdir}/compaction-state-{session_id}.md     — state snapshot (read by advisory)

The sentinel write is critical; the state write is best-effort. State-file
failure must NOT prevent sentinel creation. `run()` must NEVER raise — the
DoE stub calls it inside its own fail-open `try/except` for defense-in-depth,
but `run()` also fully contains its own errors so behavior matches the
legacy bash's unconditional `exit 0`.

Windows-portability divergence from the bash oracle (intentional, per
W4a-sessionstart-recipe.md § 2.6): both files are written under
`tempfile.gettempdir()`, NOT a hardcoded `/tmp`. This is required, not
optional — `coordinator_core.hooks.postuse_advisory_dispatch` (the existing
CONSUMER of these files, `postuse_advisory_dispatch.py:151`, Review:
code-reviewer B-F3) already reads from `tempfile.gettempdir()`; a producer
that wrote to a literal `/tmp` would silently desync from its own consumer on
native Windows, where `/tmp` does not resolve to the same path
`gettempdir()` returns. The legacy bash producer's hardcoded `/tmp` was
already Windows-lossy in the same way the reviewed consumer fixed — this
port carries that fix forward rather than reproducing the bug.

Security-load-bearing (highest-severity parity requirement in this hook):
`session_id` MUST match `^[A-Za-z0-9_@-]+$` via `re.fullmatch` before any
path is constructed from it. An id containing `/` or `..` is a
path-traversal vector into the temp dir and MUST be treated as absent
(silent no-op) — never partially processed, never erroring loudly.

Intentional divergence from the bash oracle (same class as the guard-
settings-integrity / ue-knowledge-distrust ports in this cohort): the bash
oracle has a jq-present / jq-absent fork with a hand-rolled sed/grep
fallback for JSON field extraction when `jq` is unavailable. Python has
native `json` unconditionally, so that fork collapses to one code path here
— a flagged, intentional parity divergence, not a silent drop.

No confirmed Python-native equivalent of the bash `coordinator_state_root`
seam exists yet (W4a-sessionstart-recipe.md § 6 item 4 — open risk, NOT
silently invented here). `_resolve_state_root()` falls back to the same
default the bash oracle itself falls back to when its seam is unavailable:
`${GIT_ROOT}/state`.

Spec backlink: X:/DoE-claude/scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md § 2.6
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field

# Canonical session_id charset — anchored full-match, NOT re.search. A widened
# charset (e.g. forgetting to anchor, or allowing '/') reopens a path-traversal
# write into the temp dir. Do not relax this pattern.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_@-]+$")

#: Generator-provenance declaration: _write_sentinel/_write_state_snapshot
#: write compaction-occurred-<sid> / compaction-state-<sid>.md under
#: tempfile.gettempdir() — outside the repo tree, never a tracked artifact.
GENERATES: list = []

# Overall output cap on the state-snapshot file — mirrors the bash oracle's
# trailing `| head -100`. This is a TOTAL cap across all sections combined,
# applied last, on top of each section's own per-section cap below.
_TOTAL_LINE_CAP = 100


def _run_git(args: List[str], cwd: Optional[str] = None) -> str:
    """Run a git subcommand, returning stripped stdout or "" on any failure.

    Mirrors the bash oracle's `git ... 2>/dev/null || true` pattern — never
    raises, never surfaces stderr.
    """
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            # House value (`bash_guards.dispatch_checks._run_git`), not a local
            # choice. The 2026-08-05 hot-path hardening incident that produced
            # `test_hot_path_subprocess_timeouts.py` was a Windows box degraded by
            # exactly this shape -- a `timeout=10` sitting on a hook path against a
            # house value of 2.0. A git read that has not answered in 2s is a defect
            # to find, not a wait to extend, and this call already degrades to "" on
            # expiry rather than propagating.
            timeout=2.0,
            **no_console_creationflags(),
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _extract_ids(raw: str) -> Tuple[str, str]:
    """Extract (session_id, transcript_path) from the raw PreCompact JSON payload.

    Native `json` — no jq-present/jq-absent fork (see module docstring). Any
    parse failure or missing field returns "" for that field, matching the
    bash oracle's `// empty` jq fallback semantics.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    session_id = data.get("session_id") or ""
    transcript_path = data.get("transcript_path") or ""
    return str(session_id), str(transcript_path)


def _write_sentinel(tmpdir: str, session_id: str, transcript_path: str) -> None:
    """Write the critical-path sentinel file.

    Content is the pre-compaction transcript byte size (bare integer) or an
    empty string when the transcript is missing/unreadable — consumed by
    `coordinator_core.hooks.postuse_advisory_dispatch` to suppress
    false-positive emissions (its `line.isdigit()` check tolerates both).
    """
    pre_size = ""
    if transcript_path and os.path.isfile(transcript_path):
        try:
            pre_size = str(os.path.getsize(transcript_path))
        except OSError:
            pre_size = ""

    sentinel_path = os.path.join(tmpdir, f"compaction-occurred-{session_id}")
    try:
        with open(sentinel_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{pre_size}\n")
    except OSError:
        sys.stderr.write(
            "[precompact] sentinel write failed; advisory may be missed\n"
        )


def _build_tasks_section(session_id: str) -> List[str]:
    lines = ["## Tasks"]
    home = Path(os.environ.get("HOME") or str(Path.home()))
    task_dir = home / ".claude" / "tasks" / session_id
    if task_dir.is_dir():
        entries: List[str] = []
        for f in sorted(task_dir.glob("*.json")):
            try:
                with f.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue  # skip an unreadable/malformed task file; the rest of the tasks section still renders
            if not isinstance(data, dict):
                continue
            subj = data.get("subject")
            stat = data.get("status")
            entries.append(f"- {subj} [{stat}]")
        lines.extend(entries[:20])
    else:
        lines.append("(no task list for this session)")
    return lines


def _build_git_section(cwd: Optional[str]) -> List[str]:
    lines = ["", "## Git State"]
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if branch:
        lines.append(f"Branch: {branch}")
        lines.append("Recent commits:")
        log = _run_git(["log", "--oneline", "-3"], cwd=cwd)
        if log:
            lines.extend(log.splitlines())
        lines.append("")
        lines.append("Modified files:")
        modified = _run_git(["--no-optional-locks", "diff", "--name-only"], cwd=cwd)
        if modified:
            lines.extend(modified.splitlines()[:20])
        staged = _run_git(["diff", "--staged", "--name-only"], cwd=cwd)
        if staged:
            lines.append("Staged files:")
            lines.extend(staged.splitlines()[:10])
    else:
        lines.append("(not a git repository)")
    return lines


def _resolve_state_root(git_root: str) -> str:
    """Resolve the per-repo state root (see module docstring — open risk item 4).

    Preserves the bash oracle's literal-concatenation edge case verbatim: a
    non-git cwd yields GIT_ROOT="" and therefore a state root of "/state" —
    not a valid state root on any real machine, but harmless: the subsequent
    glob simply finds nothing and falls through to "(none)".
    """
    return f"{git_root}/state"


def _build_active_plans_section(git_root: str) -> List[str]:
    lines = ["", "## Active Plans"]
    todo_glob = f"{git_root}/tasks/*/todo.md"
    try:
        matches = sorted(_glob.glob(todo_glob))
    except Exception:
        matches = []
    lines.extend(matches[:10] if matches else ["(none)"])
    return lines


def _build_handoffs_section(state_root: str) -> List[str]:
    lines = ["", "## Handoffs"]
    handoffs_glob = f"{state_root}/handoffs/*.md"
    try:
        matches = sorted(_glob.glob(handoffs_glob))
    except Exception:
        matches = []
    lines.extend(matches[:5] if matches else ["(none)"])
    return lines


def _write_state_snapshot(tmpdir: str, session_id: str) -> None:
    """Write the best-effort state-snapshot file.

    Wrapped so ANY failure here never propagates — the sentinel (already
    written by `_write_sentinel`) is the critical path; this is pure
    best-effort context enrichment. Mirrors the bash oracle's
    `( ... ) 2>/dev/null || true` subshell isolation.

    Repo root comes from `coordinator_core.git.repo_root.show_toplevel`, which
    resolves by parent walk instead of a spawn. Its None-on-failure result is
    normalized to "" here so `_resolve_state_root`'s documented non-git edge
    case (a literal "/state" that simply globs to nothing) is preserved
    byte-for-byte rather than becoming "None/state".
    """
    try:
        from coordinator_core.git import repo_root as _repo_root_seam

        cwd = os.getcwd()
        lines: List[str] = []
        lines.extend(_build_tasks_section(session_id))
        lines.extend(_build_git_section(cwd))

        git_root = _repo_root_seam.show_toplevel(cwd) or ""
        state_root = _resolve_state_root(git_root)

        lines.extend(_build_active_plans_section(git_root))
        lines.extend(_build_handoffs_section(state_root))

        capped = lines[:_TOTAL_LINE_CAP]
        state_path = os.path.join(tmpdir, f"compaction-state-{session_id}.md")
        with open(state_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(capped))
            fh.write("\n")
    except Exception:
        # Best-effort — never let a state-snapshot failure surface. Sentinel
        # write already completed; that is the contract this hook must keep.
        pass


def run(raw_stdin: str) -> None:
    """Entry point for the DoE Shape-P1 stub — direct in-process call, no
    register_op/dispatch_message round-trip (mirrors
    `write_guards.engine.evaluate_payload_json`'s call shape).

    Args:
        raw_stdin: the raw PreCompact hook JSON payload (already drained from
            stdin by the caller — this function does no I/O of its own on
            stdin; the DoE stub owns the read, matching
            `preuse-write-dispatch.py`'s ownership split).

    Never raises. Fail-open silently (no files written) when session_id is
    absent OR fails the charset validation — both are the SAME no-op path,
    matching the bash oracle's `[[ -z "$SESSION_ID" ]]` early-exit reusing
    the traversal-guard's `exit 0` shape.
    """
    try:
        session_id, transcript_path = _extract_ids(raw_stdin)

        if not session_id:
            return  # fail-open: no session_id, can't write sentinel

        if not _SESSION_ID_RE.fullmatch(session_id):
            # Security: reject ids containing path-traversal characters
            # (notably '/' and '..') before any path construction. Treat as
            # absent — same behavior as the missing-session_id path above.
            return

        import tempfile

        tmpdir = tempfile.gettempdir()

        _write_sentinel(tmpdir, session_id, transcript_path)
        _write_state_snapshot(tmpdir, session_id)
    except Exception:
        # Defense-in-depth — run() must never raise into the DoE stub's
        # fail-open wrapper. Every internal step already contains its own
        # errors; this is a final backstop, not the primary control.
        pass


@register_op("hooks.context_pressure_precompact")
def _handler(params: dict, repo_root=None) -> dict:
    """IPC/dispatch_message adapter over `run()` — flat-scalar input, thin
    JSON-reconstruction shim so both call shapes (direct `run(raw_json)` and
    dispatch-routed `_handler(params_dict)`) execute identical logic.

    Always returns `no_advisory()` — the product is the on-disk write
    side-effect (mirrors `session_heartbeat.py`'s "never blocks" contract);
    PreCompact output is ignored by Claude Code regardless.
    """
    session_id = field(params, "session_id")
    transcript_path = field(params, "transcript_path")
    raw = json.dumps({"session_id": session_id, "transcript_path": transcript_path})
    run(raw)
    return no_advisory()
