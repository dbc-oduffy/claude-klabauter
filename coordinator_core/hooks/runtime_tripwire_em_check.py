"""
coordinator_core.hooks.runtime_tripwire_em_check — PostToolUse(Agent) warm-door
counterpart of DoE-claude's `coordinator/hooks/scripts/
runtime-tripwire-em-check.py`, **PostToolUse(Agent) LEG ONLY**.

SCOPE FENCE (per this chunk's own dispatch brief): the source script is
registered on TWO events — `UserPromptSubmit` (Terminal, no later
reconciliation moment for that specific prompt's tripwire check — out of
scope, per `docs/reference/warm-hook-migration.md`'s own "Hot-path AND
Terminal" finding) and `PostToolUse(Agent)` (Reconstructable — a missed fire
is caught by the next tool round). This module implements the PostToolUse
leg ONLY. Do not extend it to serve UserPromptSubmit.

COVERAGE ESTABLISHED FIRST (per the dispatch brief's own instruction — "check
for a `@register_op` handler, not for a filename"), against the source
script's own `main()` at DoE-claude HEAD:

    main()'s five advisory legs, and what each one costs on a PostToolUse(Agent)
    fire specifically (never on a UserPromptSubmit fire, which this module does
    not implement):

      1. Subagent-detect early return (`agents_dir`/`em-session-id.txt` lookup,
         `_resolve_subagent_identity` fallback) — ALWAYS runs, gates everything
         below. No existing op covers this; PORTED (residue).
      2. `_check_push_failures` — ALWAYS runs (not event-gated). Rounds-trips
         the ALREADY-REGISTERED `git.push_failure_verdict` op
         (`coordinator_core.ops.push_failure_verdict`) for its five-state
         classification; the cursor-file bookkeeping and log-growth predicate
         around that call is NOT an existing op. PORTED (residue), calling the
         existing op's own `_handler` in-process rather than re-deriving the
         classification.
      3. `_check_hooks_json_staleness` — ALWAYS runs (not event-gated). Pure
         file-hash + cursor-file bookkeeping; no engine op involved at all.
         PORTED (residue) in full.
      4. `_check_zero_tool_use_surface` — gated `if hook_event !=
         "UserPromptSubmit": return None, None` as the FIRST statement in the
         function body (source script line ~1370). On a PostToolUse(Agent)
         fire this leg contributes nothing. NOT PORTED — dead weight on this
         leg specifically, confirmed by reading the gate, not inferred from
         the function's name.
      5. `_mint_session_baton` — gated `if hook_event != "UserPromptSubmit":
         return` as the FIRST statement in the function body (source script
         line ~1689). Same disposition as (4): NOT PORTED for this leg.

    Net: of the source script's 2,186 lines, this leg needs (1)+(2)+(3) —
    the subagent-detect gate, the push-failure log-growth predicate (which
    calls the ALREADY-BUILT `git.push_failure_verdict` op for its
    classification, not a re-derivation of it), and the hooks.json-staleness
    hash/cursor check. (4) and (5) are UserPromptSubmit-only and contribute
    zero behavior to this registration; porting them here would be exactly
    the "porting 2,186 lines that are already half-warm" waste the brief
    names.

Every input comes from `params["payload"]` — the shape `warm/hook_http.py ::
payload_from_event` builds from the fired event — never from `os.environ` or
this process's own `cwd`/session. The resident engine serves ~50 concurrent
sessions; its own process environment and cwd belong to none of them.
`session_id`/`agent_id`/`cwd` are read from the payload exactly as the
sibling `hooks.*` ops in this family do (`nudge_autonomous_askuserquestion`,
`watchdog_undischarged_next_move`, `plan_persistence_check`).

Zero-spawn throughout — no `git rev-parse`/`git rev-list` subprocess anywhere
in this module. `git_root` resolves via `coordinator_core.git.repo_root.
show_toplevel(payload["cwd"])` (a non-spawning parent walk); the source
script's own `_git_root()` walks its OWN process cwd first and falls back to
a `git rev-parse --show-toplevel` subprocess only because it is a
per-invocation script with no payload to read a cwd from — that fallback has
no analogue here (a resident engine's own cwd is nobody's session, exactly
the reasoning `docs/reference/warm-hook-migration.md`'s "Env vars an http
flip would silently drop" section names for the sibling
`plan-persistence-check.py` port). `_current_branch_cheap`'s raw `.git/HEAD`
read and `_check_hooks_json_staleness`'s single `sha256` file hash are
already zero-spawn in the source and are ported verbatim in that respect.

`_resolve_subagent_identity` here is the byte-identical 3-path port already
established in `coordinator_core.hooks.postuse_advisory_dispatch` (source
script's own docstring: "IDENTICAL to the one already ported into claude-klabauter's
postuse_advisory_dispatch.py::_resolve_subagent_identity") — this module
carries its own copy rather than importing that sibling module's private
name, matching how `postuse_advisory_dispatch` itself carries its own copy
rather than importing a shared one; see
`coordinator_core.write_guards._subagent_identity` for the DIFFERENT
4-path variant (adds an already-canonical `<name>@session-<short>` path) —
that module is a different port with different callers, not a drop-in here.

Git-common-dir resolution (`_resolve_git_common_dir` in the source) is
`coordinator_core.git.git_dir.resolve_git_common_dir` here — the shared,
already-ported, zero-spawn resolver every sibling `hooks.*` op in this
family already uses.

Returns `no_advisory()` (empty dict) whenever there is nothing to report;
otherwise `post_advisory(<text>)` — the same `hookSpecificOutput` /
`additionalContext` shape the source script's `_emit_advisory` printed to
stdout for a PostToolUse fire, joining the push-failure and hooks.json-stale
advisories with the source's own `"\n\n".join(...)` separator when both fire
on the same call.

Spec backlink: docs/plans/2026-08-31-six-hook-scripts-become-engine-ops.md
(chunk C6); docs/reference/warm-hook-migration.md (candidate-selection input,
PostToolUse Agent leg-2-of-2 row); DoE-claude `coordinator/hooks/scripts/
runtime-tripwire-em-check.py` (source).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.ipc import register_op
from coordinator_core.ops.push_failure_verdict import _handler as _push_failure_verdict_handler

# ---------------------------------------------------------------------------
# Charset guard — verbatim from the source script (`_ID_CHARSET_RE`).
# ---------------------------------------------------------------------------
_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9_@-]+$")

# AUTO-PUSH-MID-SESSION-DETECT — verbatim from the source script
# (`_PUSH_FAILED_LINE_RE`): matches only a genuine, exhausted-retry failure
# row, never every new line the log happens to grow by.
_PUSH_FAILED_LINE_RE = re.compile(r"\]\s*PUSH FAILED\b")

_PUSH_FAILURE_REFERENCE_LINE = (
    "Reference: docs/wiki/coordinator-tripwires/tripwire-registry/"
    "auto-push-mid-session-detector-auto-push-mid-session-detect.md"
)
_HOOKS_JSON_STALE_REFERENCE_LINE = (
    "Reference: docs/wiki/coordinator-tripwires/related.md"
)


# ---------------------------------------------------------------------------
# resolve_subagent_identity — byte-identical 3-path port, mirroring
# `coordinator_core.hooks.postuse_advisory_dispatch._resolve_subagent_identity`
# (which the source script's own docstring names as this file's twin). See
# module docstring for why this is its own copy, not a shared import.
# ---------------------------------------------------------------------------
def _resolve_subagent_identity(agent_id: str, session_id: str) -> str:
    """Translate a subagent-side agent_id to the canonical EM-side id.

    Three paths:
        (a) Bare hex  ^[a-f0-9]{12,}$  — unnamed agent fast path; return unchanged.
        (b) Named teammate  ^a(.+)-[a-f0-9]{16}$  — build "<name>@session-<short8>".
        (c) Anything else -> "" (fail-closed).
    """
    if re.match(r"^[a-f0-9]{12,}$", agent_id):
        return agent_id

    m = re.match(r"^a(.+)-[a-f0-9]{16}$", agent_id)
    if m:
        name = m.group(1)
        if len(session_id) < 8:
            return ""
        short = session_id[:8]
        return f"{name}@session-{short}"

    return ""


def _fail_open(fn, *args, default=None):
    """Run one detector, yielding `default` on any exception — mirrors the
    source script's own `_fail_open`: a bug in one leg must never take down
    another."""
    try:
        return fn(*args)
    except Exception:
        return default


def _ensure_cursor_dir(cursor_dir: str) -> bool:
    try:
        os.makedirs(cursor_dir, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# _current_branch_cheap — raw `.git/HEAD` read, zero-spawn, verbatim logic
# from the source script. Resolved via the worktree-PRIVATE gitdir
# (`resolve_git_common_dir`'s sibling private-dir logic is inlined here
# rather than imported, matching the source's own `_resolve_git_dir_no_
# commondir` — HEAD is per-worktree state and must NOT follow the commondir
# indirection).
# ---------------------------------------------------------------------------
def _resolve_git_dir_no_commondir(git_root: str) -> str:
    try:
        dot_git = os.path.join(git_root, ".git")
        if os.path.isdir(dot_git):
            return dot_git
        if os.path.isfile(dot_git):
            with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if not text.startswith("gitdir:"):
                return ""
            gitdir_value = text[len("gitdir:"):].strip()
            git_dir = (
                gitdir_value
                if os.path.isabs(gitdir_value)
                else os.path.normpath(os.path.join(git_root, gitdir_value))
            )
            if not os.path.isdir(git_dir):
                return ""
            return git_dir
        return ""
    except Exception:
        return ""


def _current_branch_cheap(git_root: str) -> str:
    try:
        git_dir = _resolve_git_dir_no_commondir(git_root)
        if not git_dir:
            return ""
        head_path = os.path.join(git_dir, "HEAD")
        with open(head_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        m = re.match(r"^ref:\s*refs/heads/(.+)$", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# push_failure_verdict — in-process call of the ALREADY-REGISTERED
# `git.push_failure_verdict` op's own handler (never a subprocess, never a
# `dispatch_from_hook`/JSON-RPC round-trip: caller and callee already share
# one process and one import, matching `plan_persistence_check.py`'s own
# direct-function-call precedent over its analogous op).
# ---------------------------------------------------------------------------
def _push_failure_verdict(git_root: str) -> "tuple[Optional[dict], Optional[str]]":
    """Returns `(result, degrade_reason)` — mirrors the source script's own
    contract. `degrade_reason` is `"malformed"` for any response that is not
    a dict with a recognised `verdict` and a dict `evidence`; this in-process
    call has no JSON-RPC envelope to raise a `-32602`/routing-contract error,
    so the source's `"contract"`/`"unreachable"` split collapses to a single
    `"malformed"` bucket for any unexpected shape or exception."""
    try:
        result = _push_failure_verdict_handler({}, repo_root=Path(git_root))
    except Exception:
        return None, "malformed"

    if not isinstance(result, dict):
        return None, "malformed"
    verdict = result.get("verdict")
    evidence = result.get("evidence")
    if verdict not in (
        "peer_staged",
        "half_applied_merge",
        "simple_lag",
        "resolved_since",
        "indeterminate",
    ):
        return None, "malformed"
    if not isinstance(evidence, dict):
        return None, "malformed"
    return result, None


def _render_push_failure_verdict(
    verdict_result: dict, n_new: int, branch: str, last_line: str
) -> Optional[str]:
    """Verbatim port of the source script's `_render_push_failure_verdict`."""
    verdict = verdict_result.get("verdict")
    evidence = verdict_result.get("evidence") or {}
    ref = _PUSH_FAILURE_REFERENCE_LINE

    header = (
        "{n} push failure(s) landed in .git/push-failures.log on `{branch}` "
        "since this session started:\n  {last}\n"
    ).format(n=n_new, branch=branch, last=last_line)

    if verdict == "resolved_since":
        return (
            "AUTO-PUSH mid-session note — " + header +
            "the branch is currently in sync with its upstream — the "
            "failure was real when written and has since been superseded "
            "(most likely a peer reconciled and pushed); nothing to push.\n"
            + ref
        )

    if verdict == "simple_lag":
        ahead = evidence.get("ahead")
        behind = evidence.get("behind")
        if behind:
            return (
                "AUTO-PUSH MID-SESSION FAILURE — " + header +
                f"clean index, {ahead} ahead / {behind} behind upstream — the "
                "branch has DIVERGED. Whether any of the unpushed commits are "
                "YOURS is not something this check knows: on a shared day-branch "
                "they are often entirely a peer's, in which case your own work is "
                "already pushed. Confirm before acting — `git merge-base "
                "--is-ancestor <your-sha> @{u}` per commit you care about. Do NOT "
                "retry a bare `git push`; it is what just failed, and it will keep "
                "failing while the branch is behind. A merge/rebase here rewrites "
                "or overwrites commits that may be a live peer's.\n" + ref
            )
        return (
            "AUTO-PUSH MID-SESSION FAILURE — " + header +
            f"clean index, {ahead} ahead / 0 behind upstream — the branch is "
            "strictly ahead, so `git push` should fast-forward cleanly. Whether "
            "the unpushed commits are yours or a peer's, pushing insures them "
            "all.\n" + ref
        )

    if verdict == "half_applied_merge":
        staged_incoming_overlap = evidence.get("staged_incoming_overlap")
        incoming_count = evidence.get("incoming_count")
        staged_unstaged_overlap = evidence.get("staged_unstaged_overlap") or 0
        return (
            "AUTO-PUSH MID-SESSION FAILURE — " + header +
            f"{staged_incoming_overlap} of {incoming_count} incoming files "
            f"staged, {staged_unstaged_overlap} overlap with local "
            "modifications — this reads as our own half-applied merge, not "
            "a peer's WIP: git reset (mixed), scoped-commit the blockers, "
            "re-merge.\n" + ref
        )

    if verdict == "peer_staged":
        staged_count = evidence.get("staged_count")
        incoming_count = evidence.get("incoming_count")
        staged_incoming_overlap = evidence.get("staged_incoming_overlap") or 0
        return (
            "AUTO-PUSH MID-SESSION FAILURE — " + header +
            f"{staged_count} file(s) staged, only {staged_incoming_overlap} "
            f"of {incoming_count} incoming file(s) overlap — reads as "
            "another session's work-in-progress, not our own merge: stand "
            "off, touch nothing.\n" + ref
        )

    if verdict == "indeterminate":
        upstream_resolved = evidence.get("upstream_resolved")
        incoming_count = evidence.get("incoming_count")
        if not upstream_resolved:
            why = (
                "no upstream tracking branch resolvable -- detached HEAD, "
                "unconfigured upstream, or a git failure"
            )
        elif incoming_count is None:
            why = (
                "files are staged but the incoming-commit diff could not "
                "be computed"
            )
        else:
            why = "no pathological signal to classify"
        return (
            "AUTO-PUSH MID-SESSION FAILURE — " + header +
            f"state is indeterminate ({why}) — naming the state and "
            "stopping here; do not run a recovery command on a guess, a "
            "fast route out of a stuck merge destroys work if the "
            "peer-staged reading is true.\n" + ref
        )

    return None  # unreachable -- `_push_failure_verdict` already validated `verdict`


def _check_push_failures(git_root: str, session_id: str) -> Optional[str]:
    """AUTO-PUSH-MID-SESSION-DETECT — mid-session surfacing of a *newly
    growing* `.git/push-failures.log`. Verbatim port of the source script's
    `_check_push_failures`, minus its `_unpushed_commit_count` subprocess
    fallback path: that fallback exists in the source purely to cover a
    `_push_failure_verdict` classifier that is unreachable/unimportable, a
    condition that cannot arise here — the classifier is the SAME
    in-process import as this module's own, not a separate cross-process
    round-trip that can go missing independently. A `"malformed"` degrade
    (any unexpected response shape) still falls through to the full,
    present-tense alarm text below, matching the source's own
    fail-toward-firing contract for a `None` result — it just never engages
    the git-subprocess fallback rung the source used to distinguish "engine
    unreachable" from "branch already in sync," because that distinction
    requires a spawn this module does not pay for on this leg's cadence.
    """
    common_dir = str(resolve_git_common_dir(git_root))
    if not common_dir:
        return None

    log_path = os.path.join(common_dir, "push-failures.log")
    try:
        log_size = os.path.getsize(log_path)
    except OSError:
        return None

    if not session_id or not _ID_CHARSET_RE.match(session_id):
        return None

    cursor_dir = os.path.join(common_dir, "coordinator-sessions", session_id)
    cursor_path = os.path.join(cursor_dir, "push-failures-cursor.txt")

    baseline = None
    if os.path.isfile(cursor_path):
        try:
            with open(cursor_path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            if text.isdigit():
                baseline = int(text)
        except Exception:
            baseline = None

    if baseline is None:
        if not _ensure_cursor_dir(cursor_dir):
            return None
        try:
            with open(cursor_path, "w", encoding="utf-8") as fh:
                fh.write(str(log_size))
        except Exception:
            pass
        return None

    if log_size <= baseline:
        return None

    new_lines: list = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(baseline)
            new_lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except Exception:
        pass

    try:
        with open(cursor_path, "w", encoding="utf-8") as fh:
            fh.write(str(log_size))
    except Exception:
        pass

    branch = _current_branch_cheap(git_root)
    if not branch.startswith("work/"):
        return None

    try:
        cache_path = os.path.join(git_root, "state", "orientation_cache.md")
        if os.path.isfile(cache_path):
            log_mtime = os.path.getmtime(log_path)
            cache_mtime = os.path.getmtime(cache_path)
            if cache_mtime >= log_mtime:
                return None
    except Exception:
        pass

    failed_lines = [ln for ln in new_lines if _PUSH_FAILED_LINE_RE.search(ln)]
    if not failed_lines:
        return None

    n_new = len(failed_lines)
    last_line = failed_lines[-1]

    verdict_result, _degrade_reason = _push_failure_verdict(git_root)
    if verdict_result is not None:
        rendered = _render_push_failure_verdict(verdict_result, n_new, branch, last_line)
        if rendered is not None:
            return rendered

    # Fallback: the in-process classifier answered nothing usable (a
    # malformed/unexpected shape) — reproduce the pre-classifier alarm
    # shape rather than going silent, present-tense claim intact per the
    # source's own fail-toward-firing contract. No `_unpushed_commit_count`
    # subprocess rung here — see this function's own docstring.
    return (
        "AUTO-PUSH MID-SESSION FAILURE — {n} new push failure(s) landed in "
        ".git/push-failures.log on `{branch}` since this session started, not "
        "yet reflected in state/orientation_cache.md's Auto-push health "
        "section:\n"
        "  {last}\n"
        "Crash insurance may be silently NOT insuring right now — consider "
        "`git push`, or read the full log for the failure class.\n"
    ).format(n=n_new, branch=branch, last=last_line) + _PUSH_FAILURE_REFERENCE_LINE


# ---------------------------------------------------------------------------
# PLUGIN-HOOKS-JSON-RESTART-GATED — verbatim port of the source script's
# `_check_hooks_json_staleness`. No engine op involved: a single sha256 file
# hash plus a per-session cursor file.
# ---------------------------------------------------------------------------
def _hooks_json_path(git_root: str) -> str:
    return os.path.join(git_root, "coordinator", "hooks", "hooks.json")


def _hash_file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read())
        return h.hexdigest()
    except Exception:
        return None


def _check_hooks_json_staleness(git_root: str, session_id: str, common_dir: str) -> Optional[str]:
    if not common_dir:
        return None

    if not session_id or not _ID_CHARSET_RE.match(session_id):
        return None

    current_hash = _hash_file_sha256(_hooks_json_path(git_root))
    if current_hash is None:
        return None

    cursor_dir = os.path.join(common_dir, "coordinator-sessions", session_id)
    cursor_path = os.path.join(cursor_dir, "hooks-json-boot-hash.txt")

    baseline = None
    if os.path.isfile(cursor_path):
        try:
            with open(cursor_path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            baseline = text or None
        except Exception:
            baseline = None

    if baseline is None:
        if not _ensure_cursor_dir(cursor_dir):
            return None
        try:
            with open(cursor_path, "w", encoding="utf-8") as fh:
                fh.write(current_hash)
        except Exception:
            pass
        return None

    if current_hash == baseline:
        return None

    try:
        with open(cursor_path, "w", encoding="utf-8") as fh:
            fh.write(current_hash)
    except Exception:
        pass

    return (
        "PLUGIN-HOOKS-JSON-RESTART-GATED — coordinator/hooks/hooks.json changed on disk "
        "since this session booted. This session's own hook registrations are a SNAPSHOT "
        "taken at boot: a matcher edit landing after boot is NOT live in this session "
        "(the same matcher loaded from .claude/settings.local.json IS live, no restart "
        "needed — this trap is specific to the plugin's own hooks.json). Any guard "
        "observation this session makes about a changed matcher — e.g. \"I edited the "
        "guard and it still doesn't block\" — is INVALID until a fresh session starts: "
        "restart before trusting it, or a working guard reads as inert and gets "
        "\"fixed\" twice.\n" + _HOOKS_JSON_STALE_REFERENCE_LINE
    )


# ---------------------------------------------------------------------------
# Registered op.
# ---------------------------------------------------------------------------
@register_op("hooks.runtime_tripwire_em_check")
def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Agent) leg of DoE-claude's `runtime-tripwire-em-check.py`.

    Every input comes from `params["payload"]` — never `os.environ` or this
    process's own `cwd`/session. `repo_root` (the framework-supplied handler
    argument) is unused — this op is scope "none" and resolves its own git
    root from `payload["cwd"]`, matching every other payload-cwd-resolving
    `hooks.*` op in this family.

    Fail-open on every path — this hook is advisory-only. Returns
    `no_advisory()` whenever there is nothing to report (including a
    subagent-side firing session, an unresolvable git root, or a malformed
    payload); otherwise `post_advisory(<text>)`.
    """
    try:
        payload = params.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}

        session_id = payload.get("session_id") or ""
        if not isinstance(session_id, str):
            session_id = ""
        if session_id and not _ID_CHARSET_RE.match(session_id):
            session_id = ""
        if not session_id:
            return no_advisory()

        agent_id = payload.get("agent_id") or ""
        if not isinstance(agent_id, str):
            agent_id = ""

        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return no_advisory()

        try:
            git_root = show_toplevel(cwd)
        except Exception:
            git_root = None
        if not git_root:
            return no_advisory()

        common_dir = str(_fail_open(resolve_git_common_dir, git_root, default="") or "")
        sessions_dir = os.path.join(common_dir, "coordinator-sessions") if common_dir else ""
        agents_dir = os.path.join(sessions_dir, ".agents") if sessions_dir else ""

        # --- Subagent-detect inverse: primary path (SESSION_ID as an agent dir) ---
        if agents_dir and os.path.isfile(os.path.join(agents_dir, session_id, "em-session-id.txt")):
            return no_advisory()

        # --- Resolver-based fallback for named teammates ---
        if agent_id:
            canonical = _resolve_subagent_identity(agent_id, session_id)
            if (
                canonical
                and agents_dir
                and os.path.isfile(os.path.join(agents_dir, canonical, "em-session-id.txt"))
            ):
                return no_advisory()

        push_failure_msg = _fail_open(_check_push_failures, git_root, session_id)
        hooks_json_stale_msg = _fail_open(
            _check_hooks_json_staleness, git_root, session_id, common_dir
        )

        parts = [p for p in (push_failure_msg, hooks_json_stale_msg) if p]
        if not parts:
            return no_advisory()
        return post_advisory("\n\n".join(parts))
    except Exception:
        return no_advisory()
