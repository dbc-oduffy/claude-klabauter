"""coordinator_core.hooks.nudge_multiwave_workflow — PreToolUse(Agent|Workflow)
advisory op.

Ported from DoE-claude `coordinator/hooks/scripts/nudge-multiwave-workflow.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C9.

Offer-shape (never blocks): always returns `allow_advisory()` on the nudge
path, `no_advisory()` otherwise.

Branches on tool_name (byte-faithful port of the source's decision tree):
  - "Workflow" -> records a session sentinel (workflow-launched); silent.
    Once a Workflow has launched this session, the nudge never fires again
    (Agent branch condition 4).
  - "Agent"    -> runs the nudge logic (conditions 1-6 below); fires at most
    once per session (condition 5), only on the Nth write-capable
    EM-originated Agent dispatch within a rolling W-second window
    (condition 6).
  - anything else -> silent.

Burst threshold: N defaults to 4, W defaults to 30 seconds, both
overridable via env (read from `params["env"]`, never `os.environ` — see
ADAPTATION below).

Conditions (order preserved exactly from source):
  1. `COORDINATOR_OVERRIDE_MULTIWAVE_WORKFLOW == "1"` -> never fire.
  2. subagent-originated dispatch (`agent_id` present) -> never nudge.
  3. `subagent_type` must match the write-capable roster (*executor*
     substring, or an exact review-integrator/enricher name,
     case-insensitive).
  4. No Workflow launched this session (workflow-launched sentinel absent).
  5. Fires at most once per session (multiwave-workflow-nudged sentinel).
  6. Burst threshold: rolling-window dispatch count (post-prune, INCLUDING
     the current dispatch just appended) must be >= THRESHOLD.

ADAPTATION (two sites): (1) the source's raw-substring `'"agent_id"' in raw`
check (a byte-fidelity artifact of the bash oracle this hook itself replaced,
scanning the whole stdin text rather than a parsed key) becomes
`params.get("agent_id")` truthiness — this op never sees raw bytes, only the
already-parsed payload dict `warm/hook_http.py :: payload_from_event`
builds, matching the payload-only-key-lookup convention every other op in
this package already uses (e.g. `nudge_autonomous_askuserquestion`).
(2) `os.environ` reads for the override flag and the two thresholds are
replaced with `params["env"]` reads — the resident engine serves ~50
concurrent sessions and its own process environment belongs to none of
them.

`_git_root()`'s in-process-walk-then-subprocess-fallback is replaced with
`coordinator_core.git.repo_root.show_toplevel` (zero-spawn only, subprocess
fallback dropped) — the same adaptation already made at W4-C4 for the
sibling worktree-strip module.

Graceful degradation — REQUIRED: any failure to resolve the git root,
common dir, or session sentinel/log falls through to `no_advisory()`. A
filesystem hiccup must never brick an Agent/Workflow dispatch.

Spec backlink: coordinator/docs/wiki/coordinator-tripwires.md
§ NUDGE-MULTIWAVE-WORKFLOW (DoE-claude);
docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Mapping

from coordinator_core._hook_envelope import allow_advisory, no_advisory, payload_of
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.session_hub import (
    ensure_session_dir,
    session_id_is_real,
)
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#workflow-offer-nudge"
)

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,}$")

_EXACT_WRITE_CAPABLE = {
    "review-integrator",
    "coordinator:review-integrator",
    "enricher",
    "coordinator:enricher",
}


def _is_write_capable(subagent_type_lc: str) -> bool:
    if "executor" in subagent_type_lc:
        return True
    return subagent_type_lc in _EXACT_WRITE_CAPABLE


def _env_value(env: object, key: str) -> "str | None":
    if not isinstance(env, Mapping):
        return None
    value = env.get(key)
    return value if isinstance(value, str) else None


def _read_int_lines(path: Path) -> "list[int]":
    out: "list[int]" = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(int(line))
            except ValueError:
                continue  # malformed timestamp line; skip it
    except Exception:
        pass  # unreadable/absent dispatch log is the cold-start case
    return out


def _compose_workflow_offer(in_window_count: int, env: object = None) -> str:
    prose = (
        f"[workflow offer] {in_window_count} hand-dispatched executors in a "
        "row - a Workflow survives compaction, encodes wave gates. Ad-hoc "
        "is fine; your call."
    )
    return render(compose(prose, anchor=_WIKI_ANCHOR), env=env)


@register_op("hooks.nudge_multiwave_workflow")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Agent|Workflow) op: offer a Workflow after a burst of
    hand-dispatched write-capable executors."""
    # Normalize the two params shapes
    # both engine doors and the cold chain send (see block_worktree_tool).
    params = payload_of(params)
    tool_name = params.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return no_advisory()

    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()
    if not _SESSION_ID_RE.match(session_id):
        return no_advisory()
    if not session_id_is_real(session_id):
        return no_advisory()

    try:
        git_root = show_toplevel()
    except Exception:
        git_root = None
    if not git_root:
        return no_advisory()

    common_dir = resolve_git_common_dir(git_root)
    if not common_dir:
        return no_advisory()

    session_dir = Path(common_dir) / "coordinator-sessions" / session_id

    if tool_name == "Workflow":
        try:
            if ensure_session_dir(session_dir, session_id):
                (session_dir / "workflow-launched").touch()
        except Exception:
            pass  # best-effort marker; must never block the advisory below
        return no_advisory()

    if tool_name != "Agent":
        return no_advisory()

    env = params.get("env")

    # Condition 1: explicit override.
    if _env_value(env, "COORDINATOR_OVERRIDE_MULTIWAVE_WORKFLOW") == "1":
        return no_advisory()

    # Condition 2: subagent-originated dispatch -> never nudge.
    if params.get("agent_id"):
        return no_advisory()

    # Condition 3: write-capable subagent_type only.
    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return no_advisory()
    subagent_type = tool_input.get("subagent_type")
    if not isinstance(subagent_type, str) or not subagent_type:
        return no_advisory()

    subagent_type_lc = subagent_type.lower()
    if not _is_write_capable(subagent_type_lc):
        return no_advisory()

    # Condition 4: no Workflow launched this session.
    if (session_dir / "workflow-launched").is_file():
        return no_advisory()

    # Condition 5: fire at most once per session.
    nudged_sentinel = session_dir / "multiwave-workflow-nudged"
    if nudged_sentinel.is_file():
        return no_advisory()

    # Condition 6: burst threshold.
    try:
        threshold = int(_env_value(env, "COORDINATOR_MULTIWAVE_NUDGE_THRESHOLD") or "4")
    except ValueError:
        threshold = 4
    try:
        window_secs = int(
            _env_value(env, "COORDINATOR_MULTIWAVE_NUDGE_WINDOW_SECS") or "30"
        )
    except ValueError:
        window_secs = 30
    dispatch_log = session_dir / "multiwave-dispatch-log"

    try:
        ensure_session_dir(session_dir, session_id)
    except Exception:
        pass  # best-effort dir creation; a later write below simply no-ops if absent

    now = int(time.time())
    try:
        with dispatch_log.open("a", encoding="utf-8") as fh:
            fh.write(f"{now}\n")
    except Exception:
        pass  # best-effort dispatch-log append; must never block the nudge below

    cutoff = now - window_secs
    lines = _read_int_lines(dispatch_log)
    pruned = [n for n in lines if n >= cutoff]
    tmp_path = dispatch_log.with_name(f"{dispatch_log.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text("".join(f"{n}\n" for n in pruned), encoding="utf-8", newline="\n")
        os.replace(tmp_path, dispatch_log)
    except Exception:
        try:
            tmp_path.unlink()
        except Exception:
            pass  # best-effort tmp-file cleanup after a failed replace

    in_window_count = len(pruned)

    if in_window_count < threshold:
        return no_advisory()

    try:
        nudged_sentinel.touch()
    except Exception:
        pass  # best-effort marker; must never block the advisory below

    message = _compose_workflow_offer(in_window_count, env)
    return allow_advisory("PreToolUse", message)
