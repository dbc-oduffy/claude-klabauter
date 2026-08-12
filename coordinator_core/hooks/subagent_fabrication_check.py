"""
coordinator_core.hooks.subagent_fabrication_check — pull/poll fabricated-report detector.

Purpose: Answer "did this dispatched subagent's report describe edits it never
made" — the incident this op exists to catch is real, not hypothetical: a
`coordinator:review-integrator` returned a fully well-formed report (a triage
table marking two findings applied, a named new test function, named extracted
helpers, `57 passed` where 56 was the prior count, and quoted break/restore
evidence) after 16 tool calls, with `git status --porcelain` on its target empty
— byte-identical to HEAD. The existing `ZERO-TOOL-USE-DETECT` tripwire
(hooks.subagent_zero_tool_use) keys on a raw tool-call count of zero and
structurally CANNOT fire on a 16-tool-call agent. This op answers a narrower,
harder question over the SAME transcript shape that op already reads.
Incident write-up: state/lessons/2026-08-11-an-integrator-reported-break-restore-eviden-6b21f7e40c93.yaml

Sibling of hooks.subagent_arrival_check (same pull/poll posture, same two-ID-
namespace transcript resolution — `_resolve_subagent_transcript_path` and
`_is_path_safe_agent_id` are imported from that module rather than re-derived)
and of hooks.subagent_zero_tool_use (same per-line JSONL tool_use-block
counting discipline). Read both before touching this file.

THE PREDICATE — built on tool calls, not on report prose. Do NOT parse the
subagent's free-text report anywhere in this op. "Claims to have applied
edits" is not reliably extractable from prose, and a regex over report text
produces false positives forever; the robust signal is structural, over the
subagent's OWN transcript:

    1. Count file-MUTATING tool calls (`Edit`, `Write`, `NotebookEdit`,
       `MultiEdit`) in the subagent's transcript.
    2. Count `Bash` tool calls separately. A Bash call is opaque — an agent
       legitimately edits via a `python3 -` heredoc or `sed`, so a
       Bash-using agent with zero Edit/Write calls is NOT evidence of
       fabrication. This is the single most important false-positive guard
       here; do not fold Bash into the mutating count or drop this branch.
    3. Determine whether the declared target path(s) actually changed, via
       a LIVE `git status --porcelain -- <path>` against the current working
       tree (see "No per-agent start snapshot exists" below for why this is
       a live check, not a true dispatch-time-vs-now diff).

Fires ONLY on the conjunction: mutating_tool_call_count == 0 AND
bash_tool_call_count == 0 AND target_changed is False (verified, not
unknown). All three, unconditionally. Any one of them absent, or the git
probe itself unresolvable, yields "no_signal" — never "fabrication_suspected".
This is deliberately narrow: it catches the incident above (that agent's
claimed edits, had they happened, would have been Edit calls) while staying
silent on every legitimate working shape, including a Bash-only editor and an
agent whose target genuinely changed.

No per-agent start snapshot exists at this hook seam. The brief for this op
asked for a dispatch-time-vs-completion-time diff of the target path(s); no
such snapshot is captured anywhere in this tree today — `track_touched_files`
records WHICH paths were touched by Edit/Write/NotebookEdit/MultiEdit calls
(not a content hash, and only for those four tool names — a Bash-authored
edit records nothing there, see that module's NAMED LIMIT/DR-258), and
`_ensure_session_dir` stamps `head_at_start` once per SESSION, not per agent
or per file. Rather than invent a snapshot store this op has no mandate to
own, this op checks the CURRENT working tree against `HEAD` at the moment
it's called — the same thing a human runs by hand
(`git status --porcelain <path>`) — which is the exact honest signal
available and is sufficient for the conjunction above: if a target is clean
against HEAD AND no mutating/Bash tool call fired, nothing this agent could
have legitimately done is unaccounted for. A target that WAS dirty before
this agent's dispatch and stayed dirty in the same way would false-negative
(report "no_signal" when it should fire) — accepted, because "prefer missing
a fabrication to inventing one" is the standing constraint, and a caller
wanting the stronger dispatch-time-vs-now diff must supply its own baseline
snapshot; this op does not have one to compare against.

Negative-spec:
    Does NOT parse or reason about the subagent's report text — see THE
    PREDICATE above.

    Does NOT write anything — pure read/compute, same posture as
    hooks.subagent_arrival_check and hooks.subagent_zero_tool_use_resolve.

    Does NOT retry, wait, or poll on a timer — a single point-in-time read
    (transcript + live git status). The caller re-invokes this op itself for
    a fresher read.

    Does NOT alter, gate, or interact with ZERO-TOOL-USE-DETECT or any other
    existing tripwire — an independent predicate over the same transcript
    shape, nothing shared but the read path.

    Does NOT block a dispatch and NEVER raises into a hook — every
    unresolvable/unreadable/malformed input path below resolves to verdict
    "no_signal" (fail-open), mirroring hooks.subagent_arrival_check's
    fail-toward-"unknown" discipline exactly: an absent transcript, an
    unreadable transcript, a git-status probe that errors or times out, or
    missing agent_id/transcript_path/target_paths/repo_root all fail open.

Also ships `verify_target_clean(repo_root, target_paths)` — the cheap,
directly-callable EM-side check named in the brief: the mechanised form of
"`git status --porcelain <target>` plus a one-line read". Callable without a
hook firing, either as a plain Python import or via
`python -m coordinator_core.hooks.subagent_fabrication_check <repo_root>
<target_path> [<target_path>...]`.

Spec backlink: state/sizings/2026-08-11-catch-a-fabricated-agent-report-mechanic.yaml
Spec backlink: state/lessons/2026-08-11-an-integrator-reported-break-restore-eviden-6b21f7e40c93.yaml
"""

from __future__ import annotations

import json
import os
import subprocess

from coordinator_core.hooks._payload import field
from coordinator_core.hooks.subagent_arrival_check import (
    _is_path_safe_agent_id,
    _resolve_subagent_transcript_path,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.win_portability import no_console_creationflags

# File-mutating tool names — Bash is deliberately NOT in this set (see the
# module docstring's false-positive guard #2).
_MUTATING_TOOL_NAMES = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

_GIT_STATUS_TIMEOUT_SECONDS = 10


def _count_calls_by_name(transcript_path: str) -> dict[str, int] | None:
    """Return {tool_name: call_count} over every tool_use block in the transcript, or
    None on an absent/unreadable transcript.

    Mirrors hooks.subagent_zero_tool_use._count_tool_use_blocks's per-line
    tolerance (a malformed line, or one whose message.content is absent/not a
    list, contributes nothing and does not abort the count) but buckets by
    tool NAME rather than returning a bare total — this op needs the
    Edit/Write/NotebookEdit/MultiEdit vs Bash split, not one number.

    A present-but-empty-of-tool_use transcript is a verified {} (no keys),
    not None — the file itself was readable; there is nothing ambiguous
    about a genuinely toolless run. None is reserved for "the file could
    not be read at all".
    """
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    counts: dict[str, int] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            name = block.get("name")
            if isinstance(name, str):
                counts[name] = counts.get(name, 0) + 1
    return counts


def _git_porcelain_for_path(worktree_root: str, path: str) -> str | None:
    """Return `git status --porcelain -- <path>` stdout, or None on any failure.

    None (never an exception) on: git not on PATH, non-zero exit, timeout, or
    any other OSError — callers treat None as "could not verify", which fails
    the whole check open (see _targets_changed).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", path],
            cwd=worktree_root,
            capture_output=True,
            text=True,
            timeout=_GIT_STATUS_TIMEOUT_SECONDS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _targets_changed(worktree_root: str, target_paths: list[str]) -> bool | None:
    """True iff ANY target path shows dirty/untracked status right now.

    None (never False-by-default) if the git-status probe could not be
    completed for even ONE target — a probe failure must never be silently
    read as "clean", which is the direction that would let this detector
    fire a false fabrication signal. False only when every target was
    successfully probed and every probe came back empty.
    """
    if not target_paths:
        return None
    for path in target_paths:
        porcelain = _git_porcelain_for_path(worktree_root, path)
        if porcelain is None:
            return None
        if porcelain.strip():
            return True
    return False


def verify_target_clean(repo_root: str, target_paths: list[str]) -> str:
    """One-line, directly-callable EM-side verdict: are target_paths dirty vs HEAD?

    The mechanised form of "git status --porcelain <target>" — the two-second
    check the incident this op exists to catch never got run. Callable
    without any hook firing, from a plain Python import or via this module's
    `python -m` entrypoint (see bottom of file).
    """
    try:
        worktree_root = str(main_worktree_root(git_common_dir(repo_root)))
    except RuntimeError:
        worktree_root = str(repo_root)

    changed = _targets_changed(worktree_root, target_paths)
    if changed is None:
        return (
            f"UNKNOWN — could not run `git status --porcelain` for one or more of "
            f"{target_paths} under {worktree_root}"
        )
    if changed:
        return (
            f"DIRTY — at least one of {target_paths} differs from HEAD "
            "(a real change is on disk)"
        )
    return (
        f"CLEAN — {target_paths} byte-identical to HEAD; if the agent's report "
        "claimed edits here, verify the report before accepting it"
    )


def _envelope(
    verdict: str,
    agent_id: str,
    subagent_transcript_path: str,
    reason: str,
    *,
    mutating_tool_call_count: int | None = None,
    bash_tool_call_count: int | None = None,
    target_changed: bool | None = None,
) -> dict:
    return {
        "verdict": verdict,
        "agent_id": agent_id,
        "subagent_transcript_path": subagent_transcript_path,
        "mutating_tool_call_count": mutating_tool_call_count,
        "bash_tool_call_count": bash_tool_call_count,
        "target_changed": target_changed,
        "reason": reason,
    }


@register_op("hooks.subagent_fabrication_check")
async def _handler(params: dict, repo_root=None) -> dict:
    """Compute-only op: verdict "fabrication_suspected" | "no_signal" for one agent_id.

    Inputs (flat scalar, extracted via _payload.field(); "" treated as absent):
        transcript_path — the PARENT session's transcript path (as carried on
            PostToolUse); used ONLY to derive the subagent transcript path,
            identical resolution to hooks.subagent_arrival_check (both ID
            namespaces — bare hex and EM-side canonical form).
        agent_id — the subagent's id, in either namespace (see
            hooks.subagent_arrival_check's TWO ID NAMESPACES note).
        target_paths — comma-separated list of the path(s) the agent's brief
            declared as its edit target(s), worktree-relative or absolute.

    repo_root — the common_dir this op is invoked with (op_scopes "common_dir"
        classification); used only to resolve the worktree root for the live
        `git status --porcelain` probe.

    Returns the pinned {"verdict", "agent_id", "subagent_transcript_path",
    "mutating_tool_call_count", "bash_tool_call_count", "target_changed",
    "reason"} shape directly — never raises; every unresolved input or failed
    probe resolves to verdict "no_signal", never "fabrication_suspected".
    """
    import asyncio

    transcript_path = field(params, "transcript_path")
    agent_id = field(params, "agent_id")
    target_paths_raw = field(params, "target_paths")

    if not agent_id:
        return _envelope("no_signal", "", "", "no agent_id supplied — nothing to check")
    if not transcript_path:
        return _envelope(
            "no_signal", agent_id, "",
            "no transcript_path supplied — cannot derive the subagent transcript path",
        )
    if not target_paths_raw:
        return _envelope(
            "no_signal", agent_id, "",
            "no target_paths supplied — cannot verify the working tree against a report",
        )
    if not repo_root:
        return _envelope(
            "no_signal", agent_id, "",
            "no repo_root supplied — cannot resolve the worktree root for git status",
        )
    if not _is_path_safe_agent_id(agent_id):
        return _envelope(
            "no_signal", agent_id, "",
            "agent_id carries a path separator or a '..' segment — it would steer the "
            "transcript derivation outside the subagents directory, so no read is attempted",
        )

    target_paths = [p.strip() for p in target_paths_raw.split(",") if p.strip()]
    if not target_paths:
        return _envelope(
            "no_signal", agent_id, "",
            "target_paths resolved to an empty list after parsing — nothing to verify",
        )

    subagent_transcript_path = await asyncio.to_thread(
        _resolve_subagent_transcript_path, transcript_path, agent_id
    )

    counts = await asyncio.to_thread(_count_calls_by_name, subagent_transcript_path)
    if counts is None:
        return _envelope(
            "no_signal", agent_id, subagent_transcript_path,
            f"subagent transcript at {subagent_transcript_path} is absent or unreadable "
            "— no signal (fail-open, never fabrication_suspected on an unread transcript)",
        )

    mutating_count = sum(counts.get(name, 0) for name in _MUTATING_TOOL_NAMES)
    bash_count = counts.get("Bash", 0)

    try:
        worktree_root = str(main_worktree_root(git_common_dir(repo_root)))
    except RuntimeError:
        worktree_root = str(repo_root)

    target_changed = await asyncio.to_thread(_targets_changed, worktree_root, target_paths)
    if target_changed is None:
        return _envelope(
            "no_signal", agent_id, subagent_transcript_path,
            "could not determine working-tree state for one or more target paths "
            "(git status --porcelain failed or timed out) — no signal, fail-open",
            mutating_tool_call_count=mutating_count,
            bash_tool_call_count=bash_count,
        )

    if mutating_count == 0 and bash_count == 0 and target_changed is False:
        return _envelope(
            "fabrication_suspected", agent_id, subagent_transcript_path,
            f"agent {agent_id} made 0 Edit/Write/NotebookEdit/MultiEdit calls, 0 Bash "
            f"calls, and target path(s) {target_paths} show no working-tree change vs "
            "HEAD — verify this agent's report against disk before accepting it",
            mutating_tool_call_count=mutating_count,
            bash_tool_call_count=bash_count,
            target_changed=target_changed,
        )

    return _envelope(
        "no_signal", agent_id, subagent_transcript_path,
        f"at least one of: {mutating_count} mutating tool call(s), {bash_count} Bash "
        f"call(s), or a working-tree change was observed for {target_paths} — no "
        "fabrication signal",
        mutating_tool_call_count=mutating_count,
        bash_tool_call_count=bash_count,
        target_changed=target_changed,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(
            "usage: python -m coordinator_core.hooks.subagent_fabrication_check "
            "<repo_root> <target_path> [<target_path>...]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(verify_target_clean(sys.argv[1], sys.argv[2:]))
