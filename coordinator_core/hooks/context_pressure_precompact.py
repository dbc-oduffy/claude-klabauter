"""coordinator_core.hooks.context_pressure_precompact — PreCompact sentinel +
state-serialization bookkeeping op.

Port of: context-pressure-precompact.sh (DoE d39ab164, 2026-07-16) (W4b, recipe § 2.6).

Landing convention confirmed by `08-claude-klabauter-landing-contract.md § 1`: one file
per hook op under `coordinator_core/hooks/`, `snake_case.py` named after the
op (not the bash script), registered via `@register_op("hooks.<name>")` at
import time, using `_envelope.py`'s shape builders. This module follows the
`track_touched_files.py` shape most closely (both are async bookkeeping ops
whose product is an on-disk write side-effect, not an advisory) — see that
file for the sibling pattern this one mirrors.

Called TWO ways (both in-process, no subprocess, no bash). hooks.json dials
the op over the http door (item 2); the DoE stub (item 1) is no longer wired:
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

`run()` returns the compaction STEERING text: the DoE stub prints it, and
the harness (2.1.295) takes a PreCompact hook's trimmed plain stdout as
`newCustomInstructions`, appended after any args the PM typed to `/compact` —
additive, never a replacement. The same text is echoed to the PM as the hook's
completion line, so it is held to `_STEERING_CHAR_CAP`. `_handler` keeps
returning `no_advisory()`: the op door has no stdout. State is bridged to
context via
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

"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core._hook_envelope import payload_of
from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_@-]+$")

GENERATES: list = []

_TOTAL_LINE_CAP = 100


def _extract_ids(raw: str) -> Tuple[str, str, str]:
    """`(session_id, transcript_path, custom_instructions)`, each `""` when absent."""
    try:
        data = json.loads(raw)
    except Exception:
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    return tuple(str(data.get(k) or "") for k in
                 ("session_id", "transcript_path", "custom_instructions"))


def _write_sentinel(tmpdir: str, session_id: str, transcript_path: str) -> None:
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
                continue
            if not isinstance(data, dict):
                continue
            subj = data.get("subject")
            stat = data.get("status")
            entries.append(f"- {subj} [{stat}]")
        lines.extend(entries[:20])
    else:
        lines.append("(no task list for this session)")
    return lines


_LOG_COUNT = 3
_ABBREV_LEN = 10
_UNSTAGED_NOT_LISTED = "(unstaged list not computed; run git status)"
_STAGED_CAP = 10


def _recent_commits(repo: Path, head: str) -> List[str]:
    from itertools import islice

    from coordinator_core.git import commit_walk
    from coordinator_core.git.git_dir import resolve_git_common_dir

    out: List[str] = []
    common = resolve_git_common_dir(repo)
    for sha, meta in islice(commit_walk.walk(common, head), _LOG_COUNT):
        subject = meta["message"].splitlines()[0].strip() if meta["message"] else ""
        out.append(f"{sha[:_ABBREV_LEN]} {subject}".rstrip())
    return out


def _staged_paths(repo: Path) -> List[str]:
    """Repo-relative paths whose index `(mode, sha)` differs from HEAD's tree.
    Staged DELETIONS (`git rm --cached`) are not listed (see
    `push_failure_verdict._inprocess_staged_unstaged`). Raises on a
    corrupt/conflicted index.
    """
    from coordinator_core.git.git_state import head_blobs, read_index

    index = read_index(repo)
    paths = sorted(index)
    if not paths:
        return []
    head_map = head_blobs(repo, paths)
    return [p for p in paths if head_map.get(p) != (index[p].mode, index[p].sha)]


def _build_git_section(cwd: Optional[str]) -> List[str]:
    from coordinator_core.git import repo_root as _repo_root_seam
    from coordinator_core.git.git_state import head_branch, head_sha

    lines = ["", "## Git State"]
    root = _repo_root_seam.show_toplevel(cwd)
    branch = None
    head = None
    if root:
        try:
            head = head_sha(root)
            branch = head_branch(root) if head else None
        except (OSError, ValueError):
            branch = None
    if not branch:
        lines.append("(not a git repository)")
        return lines
    repo = Path(root)
    lines.append(f"Branch: {branch}")
    lines.append("Recent commits:")
    try:
        lines.extend(_recent_commits(repo, head))
    except (OSError, ValueError):
        pass
    lines.append("")
    lines.append("Modified files:")
    lines.append(_UNSTAGED_NOT_LISTED)
    try:
        staged = _staged_paths(repo)
    except (OSError, ValueError):
        staged = []
    if staged:
        lines.append("Staged files:")
        lines.extend(staged[:_STAGED_CAP])
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


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _render_resume_call(run: dict) -> str:
    """Render the exact `Workflow({...})` call an EM pastes to resume a run
    that a `/compact` killed mid-flight. `scriptPath`/`args` may be `None`
    (the capture side -- postuse_advisory_dispatch.py's
    `_capture_script_path_and_args` -- is best-effort, not pinned to a
    confirmed harness field), in which case only `resumeFromRunId` is named
    and the reader is told the rest is unknown rather than shown a
    fabricated value.

    Every interpolated value is rendered through `json.dumps`, not an f-string
    quote -- a raw f-string (the prior shape for `scriptPath`) lets an
    embedded quote close the string early and splice arbitrary text into the
    snapshot. `run_id` is control-character-checked despite already carrying
    quotes via json.dumps: a newline inside it still breaks the ONE-LINE
    assumption the state-snapshot renderer makes elsewhere in this module.
    """
    run_id = run.get("run_id")
    script_path = run.get("scriptPath")
    args = run.get("args")
    if not isinstance(run_id, str) or not run_id or _CONTROL_CHAR_RE.search(run_id):
        return ""
    if (
        not isinstance(script_path, str)
        or not script_path
        or _CONTROL_CHAR_RE.search(script_path)
    ):
        return (
            "Workflow({scriptPath: <unknown -- capture missed it>, "
            f"resumeFromRunId: {json.dumps(run_id)}}})"
        )
    parts = [f"scriptPath: {json.dumps(script_path)}"]
    if isinstance(args, (dict, list)) and args:
        try:
            parts.append("args: " + json.dumps(args))
        except Exception:
            pass
    parts.append(f"resumeFromRunId: {json.dumps(run_id)}")
    return "Workflow({" + ", ".join(parts) + "})"


def _build_workflow_runs_section(tmpdir: str, session_id: str) -> List[str]:
    lines = ["", "## Active Workflow Runs"]
    if not session_id:
        lines.append("(none)")
        return lines
    pattern = os.path.join(tmpdir, f"workflow-run-{session_id}-*.json")
    try:
        matches = sorted(_glob.glob(pattern))
    except Exception:
        matches = []
    rendered: List[str] = []
    for path in matches[:10]:
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        resume_call = _render_resume_call(record)
        if not resume_call:
            continue
        rendered.append(f"- resume: {resume_call}")
    lines.extend(rendered if rendered else ["(none)"])
    return lines


_STEERING_CHAR_CAP = 1500
_CARRY_FORWARD_CAP = 5
_CARRY_FORWARD_ENTRY_CAP = 200

_STEERING_RULES = (
    "Compaction steering (coordinator):\n"
    "- Lead with forward state: open work, next actions, pending PM decisions, "
    "in-flight agents and workflow runs (with ids), uncommitted or unpushed changes.\n"
    "- Finished, verified work: one line each. Drop tool-output narration."
)


def _journal_paths(session_id: str, cwd: str) -> Tuple[Optional[Path], Optional[Path]]:
    """`(baton.json, pm_turns.jsonl)` for the session, each `None` when absent."""
    from coordinator_core.session_baton import store

    sdir = store.baton_dir(session_id, cwd)
    if sdir is None or not sdir.is_dir():
        return None, None
    baton = sdir / store.BATON_FILENAME
    turns = sdir / "pm_turns.jsonl"
    return (baton if baton.is_file() else None,
            turns if turns.is_file() else None)


def _one_line(text: str, cap: int) -> str:
    flat = " ".join(_CONTROL_CHAR_RE.sub(" ", text).split())
    return flat if len(flat) <= cap else flat[: cap - 1] + "…"


def build_steering(session_id: str, cwd: str, typed: str = "") -> str:
    """The summarizer instructions for this compaction. Never raises: any
    journal read failure degrades to the static rules alone."""
    lines = [_STEERING_RULES]
    if typed.strip():
        lines[0] = lines[0].replace(
            "(coordinator):", "(coordinator; the PM's instructions above take precedence):"
        )
    try:
        baton, turns = _journal_paths(session_id, cwd)
    except Exception:
        baton, turns = None, None
    if turns is not None:
        try:
            count = turns.read_bytes().count(b"\n")
        except OSError:
            count = 0
        lines.append(
            f"- PM verbatims ({count} turns) are saved at {turns} — cite that path "
            "and the turn numbers; do not restate or paraphrase them."
        )
    if baton is not None:
        lines.append(f"- Session work journal: {baton} — point to it, do not copy it.")
        try:
            record = json.loads(baton.read_text(encoding="utf-8"))
            notes = record.get("carry_forward") if isinstance(record, dict) else None
        except (OSError, ValueError):
            notes = None
        if isinstance(notes, list):
            kept = [_one_line(n, _CARRY_FORWARD_ENTRY_CAP)
                    for n in notes[-_CARRY_FORWARD_CAP:] if isinstance(n, str) and n.strip()]
            if kept:
                lines.append("- Keep these carry-forward notes verbatim:")
                lines.extend(f"  - {n}" for n in kept)
    text = "\n".join(lines)
    return text if len(text) <= _STEERING_CHAR_CAP else text[: _STEERING_CHAR_CAP - 1] + "…"


def _write_state_snapshot(tmpdir: str, session_id: str) -> None:
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
        lines.extend(_build_workflow_runs_section(tmpdir, session_id))

        capped = lines[:_TOTAL_LINE_CAP]
        state_path = os.path.join(tmpdir, f"compaction-state-{session_id}.md")
        atomic_write_bytes(state_path, ("\n".join(capped) + "\n").encode("utf-8"))
    except Exception:
        pass


def run(raw_stdin: str) -> str:
    """Entry point for the DoE Shape-P1 stub — direct in-process call, no
    register_op/dispatch_message round-trip (mirrors
    `write_guards.engine.evaluate_payload_json`'s call shape).

    Args:
        raw_stdin: the raw PreCompact hook JSON payload (already drained from
            stdin by the caller — this function does no I/O of its own on
            stdin; the DoE stub owns the read, matching
            `preuse-write-dispatch.py`'s ownership split).

    Returns the steering text for the stub to print — always non-empty, the
    static rules alone when the session or its journal is unreadable.

    Never raises. Writes no files when session_id is absent OR fails the
    charset validation — both are the SAME no-op path, matching the bash
    oracle's `[[ -z "$SESSION_ID" ]]` early-exit reusing the traversal-guard's
    `exit 0` shape.
    """
    try:
        session_id, transcript_path, typed = _extract_ids(raw_stdin)

        if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
            return _STEERING_RULES

        import tempfile

        tmpdir = tempfile.gettempdir()

        _write_sentinel(tmpdir, session_id, transcript_path)
        _write_state_snapshot(tmpdir, session_id)
        return build_steering(session_id, os.getcwd(), typed)
    except Exception:
        return _STEERING_RULES


@register_op("hooks.context_pressure_precompact")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    session_id = field(params, "session_id")
    transcript_path = field(params, "transcript_path")
    raw = json.dumps({"session_id": session_id, "transcript_path": transcript_path})
    run(raw)
    return no_advisory()
