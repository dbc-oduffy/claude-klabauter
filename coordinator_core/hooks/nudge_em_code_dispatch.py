"""
coordinator_core.hooks.nudge_em_code_dispatch — PreToolUse op.

Purpose: Routes the EM's (main-agent) inline code writes to executor dispatch.
The async `_handler` below stays advisory. `op()` denies the EM's product-code
write in an unsized session at guard level strict (an author box) and advises at warn
(a consumer box); a sized or plan-routed session gets the advisory at every level.

Suppression conditions (no_advisory):
  1. agent_id present — subagent writes are allowed; executors write code. The one exception is
       a product write by a spawn of a live raw-ask run that has bound no sizing (the size stage),
       attributed by `_size_stage_write_deny`; any other subagent passes.
  2. file_path absent or extension in DOC_DATA_EXTENSIONS denylist.
  3. Session sentinel present, platform-temp-dir-rooted, resolved via
       coordinator_core.session.dispatch_nudge_sentinel.sentinel_path.
  4. Autonomous-run mode resolved via
       coordinator_core.session.mode_resolution.resolve_mode("autonomous", session_id)
       — a session-wins key, so the session's own sentinel always takes precedence
       over any fleet-level value for this key.
  5. `op()` ONLY (the async `_handler` above never receives old_string/new_string/
     content — see the MultiEdit negative-spec below): the triggering edit is a
     semantic-bypass shape — whitespace-only, comment/docstring-only (heuristic,
     see `_is_comment_or_docstring_only_diff`), or a single-token rename. These
     shapes are the size floor for every session. A learned threshold (session
     histogram, recent-commit sizes) is not used. Separately, a recorded XS lane
     or a post-landing residue window applies a ruled diff-measured budget
     (`session.xs_lane_budget`: 2 files, 30 changed lines) to the EM's own
     edits; authority is the APM ruling on calibration-ladder findings 60/61
     (relayed ask R1).

  6. `op()` ONLY: scratch/tmp paths. Elsewhere `op()` answers the main EM's
     product-code write with a repeating advisory.
     The sentinel (3) suppresses it only when its content is a non-empty
     reason line. Each honoured sentinel appends a JSON line to the per-session escape log
     (`dispatch-escapes.jsonl` in the session's share dir).

Negative-spec:
  - MultiEdit edits[] are NOT forwarded by the mcp_tool hook (only scalar fields
    are forwarded). The source JS's MultiEdit edits[] walk is not portable here.
    # TODO(pcore-04 C5): if MultiEdit edits[] forwarding is added to the hook
    #   input contract, re-plumb the extension-check to walk edits[].file_path
    #   the same way nudge-em-code-dispatch.js lines 97-105 do.
  - `op()` never writes the working tree. Its one write is the session's XS-lane/residue
    record (a session-dir file, like the escape log). Sentinels are only CHECKED, never
    written — writing the dispatch-nudge-ok sentinel remains the EM's job via bash.
  - The budget branches run before the autonomous exemption: autonomy buys no extra budget.
    An escaped edit (`_take_escape`) is not added to the lane's path set.

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C5
Source: coordinator/hooks/scripts/nudge-em-code-dispatch.js
Design: coordinator-content-repo coordinator/docs/wiki/dispatching-parallel-agents/eager-agent-calibration.md (design-as-offers)
Tripwire: coordinator-tripwires.md § NUDGE-EM-CODE-DISPATCH
"""

from __future__ import annotations

import difflib
import io
import os
import re
import socket
import sys
import tokenize
from pathlib import Path, PurePosixPath

from coordinator_core._hook_envelope import deny
from coordinator_core import machine_resolver
from coordinator_core._settings_home import settings_home
from coordinator_core.group_em.gem_home import contains as _path_contains
from coordinator_core.hooks._envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks._payload import field, present
from coordinator_core.hooks.doc_data_extensions import _DOC_DATA_EXTENSIONS
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.session.dispatch_nudge_sentinel import (
    sentinel_path as dispatch_nudge_sentinel_path,
)
from coordinator_core.hooks.dispatch_escape import (
    _take_escape,
    _resolve_session_id_op,
)
from coordinator_core.session import general_work_scope
from coordinator_core.session.engine_route_state import session_sizing_state as _session_sizing_state
from coordinator_core.session.mode_resolution import resolve_mode
from coordinator_core.warp_fire import fire_step

GENERATES: list = []


def _sanitize_for_hostname(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", s)


def _resolve_session_id(session_id: str) -> tuple[str, bool]:
    """Return (resolved_sid, is_true_session_id).

    When session_id is non-empty, returns it with True (the authoritative path).
    When absent, falls back to hostname-pid (same shape as the JS fallback),
    with False — the fallback sentinel is invocation-scoped and will not match
    across hook re-invocations for the same session.

    FALLBACK-NOTE (mirrored from JS source lines 46-55): if session_id is absent,
    the sentinel path built from this fallback is pid-scoped. An EM writing a
    suppression sentinel manually must use the payload's session_id, not this
    fallback, or the match will only fire if the engine process happens to share
    the same OS pid.
    """
    if session_id:
        return session_id, True
    hostname = _sanitize_for_hostname(socket.gethostname())
    return f"{hostname}-{os.getpid()}", False


def _ext_of(file_path: str) -> str:
    p = PurePosixPath(file_path)
    return p.suffix.lower()


def _sentinel_exists(path: str) -> bool:
    try:
        os.stat(path)
        return True
    except OSError:
        return False


@register_op("hooks.nudge_em_code_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    import asyncio

    if present(params, "agent_id"):
        return no_advisory()

    file_path = field(params, "file_path")
    if not file_path:
        return no_advisory()

    ext = _ext_of(file_path)
    if ext in _DOC_DATA_EXTENSIONS:
        return no_advisory()

    raw_sid = field(params, "session_id")
    session_id, has_true_sid = _resolve_session_id(raw_sid)

    nudge_ok_tmp = str(dispatch_nudge_sentinel_path(session_id))

    found = await asyncio.to_thread(_sentinel_exists, nudge_ok_tmp)
    if found:
        return no_advisory()

    autonomous_run = await asyncio.to_thread(resolve_mode, "autonomous", session_id)
    if autonomous_run:
        return no_advisory()


    sentinel_note = "." if has_true_sid else " (this OS pid only — session_id absent)."

    nudge_message = (
        f"EM, not typist. Code-file write — dispatch an executor instead "
        f"(agent-dispatch-economics.md). Suppress by writing {nudge_ok_tmp}"
        f"{sentinel_note}"
    )

    return context_only("PreToolUse", f"[em-code-dispatch nudge] {nudge_message}")


# =============================================================================
# op(payload) — synchronous, in-process, stdin->stdout trampoline entry point.
#
# Purpose: full line-for-line port of nudge-em-code-dispatch.js's `main()`
# orchestration for the DoE-resident stdin->stdout hook stub
# (coordinator/hooks/scripts/nudge-em-code-dispatch.py), replacing the `node`
# cold-spawn on every Write/Edit/MultiEdit (constraint 7, performant-or-dead).
#
# This is a DELIBERATELY SEPARATE code path from the `_handler`/register_op
# async op above: that op is the pcore-04 mcp_tool IPC-daemon integration
# (flat-scalar `field()`/`present()` payload contract, MultiEdit edits[] NOT
# forwarded) — a different transport wired to a different (currently unused)
# consumer. `op()` below consumes the SAME raw PreToolUse JSON payload the JS
# hook received (nested tool_input, full
# MultiEdit edits[] array) and reproduces every JS branch, including the F7
# bootstrap/out-of-repo carve-out, EXT_EXECUTOR_MAP/COORDINATOR_PATH_MARKERS
# executor-type derivation, and the pending-dispatch artifact write — none of
# which the pcore-04 op implements. Do not conflate the two; do not route the
# DoE stub through the async op above.
#
# Contract: takes the raw stdin-parsed payload dict, returns a Form-A
# hookSpecificOutput dict (see context_only()) when the nudge fires, or None
# for every silent-allow/bypass path. Never raises on well-formed input;
# callers (the DoE stub) wrap this in a broad try/except for fail-open ALLOW
# on any resolve/import/run failure per constraint discipline.
#
# Spec backlink: coordinator-content-repo:pln-bash-to-naked-python-engine-mi-c09292
# Source: coordinator/hooks/scripts/nudge-em-code-dispatch.js (435 lines, ported whole)
# =============================================================================

# ---------------------------------------------------------------------------
# Executor type derivation — extension -> type mapping. Mirrors JS EXT_EXECUTOR_MAP.
# ---------------------------------------------------------------------------
_EXT_EXECUTOR_MAP: dict[str, str] = {
    ".py": "python-executor",
    ".js": "js-executor",
    ".mjs": "js-executor",
    ".cjs": "js-executor",
    ".ts": "ts-executor",
    ".tsx": "ts-executor",
    ".jsx": "js-executor",
    ".sh": "shell-executor",
    ".bash": "shell-executor",
    ".rb": "ruby-executor",
    ".go": "go-executor",
    ".rs": "rust-executor",
    ".java": "jvm-executor",
    ".kt": "jvm-executor",
    ".c": "cpp-executor",
    ".cpp": "cpp-executor",
    ".cc": "cpp-executor",
    ".h": "cpp-executor",
    ".hpp": "cpp-executor",
    ".swift": "swift-executor",
    ".sql": "sql-executor",
}

# Path markers that signal coordinator-domain files -> coordinator-executor override.
# Mirrors JS COORDINATOR_PATH_MARKERS.
_COORDINATOR_PATH_MARKERS: list[str] = [
    "coordinator/",
    "coordinator_core/",
    "plugins/coordinator",
]

_DEVOPS_FILENAME_RE = re.compile(
    r"^(?:[Mm]akefile|[Dd]ockerfile(?:\..+)?|[Rr]akefile|[Gg]runtfile\.js|[Gg]ulpfile\.js)$"
)


def _bootstrap_dirs() -> list[str]:
    """Machine-config/bootstrap dirs the F7 carve-out skips the nudge for.

    Mirrors JS bootstrapDirs() for the `$HOME`-rooted rungs: HOME must be set
    (matches JS's process.env.HOME guard), and those rungs are omitted when it
    is absent.

    DIVERGENCE FROM THE JS MIRROR — `<settings-home>/bin` is resolved
    independently of the HOME guard, via the canonical
    `coordinator_core._settings_home.settings_home()` (COORDINATOR_SETTINGS_HOME,
    else CLAUDE_HOME, else the platform home, with `.coordinator-claude-settings`
    appended to the latter two). That resolver has a Windows rung `Path.home()`
    that a bare `$HOME` read does not, so gating it behind `HOME` would drop the
    live executable surface's carve-out on exactly the platform where HOME is
    unset. Resolution failure (a cwd-relative COORDINATOR_SETTINGS_HOME, or an
    undeterminable home) degrades to omitting the rung, never to raising — this
    is an advisory hook on the write path.

    `~/.claude/bin` is retained as a legacy rung: its executable surface moved to
    `<settings-home>/bin` on 2026-07-28 (the old dir renamed aside), but a machine
    that has not been reinstalled since still has live bits there, and a stale
    carve-out entry costs nothing but a suppressed nudge on a dir nobody writes.

    Matching is prefix-on-abspath (see `_is_under_bootstrap_dir`), not realpath —
    a symlinked settings home is matched by the path as written, not its target.
    """
    dirs: list[str] = []

    try:
        dirs.append(str(settings_home() / "bin"))
    except (ValueError, RuntimeError, OSError):
        pass

    home = os.environ.get("HOME")
    if home:
        dirs.extend([
            os.path.join(home, ".local", "bin"),
            os.path.join(home, ".claude", "bin"),
            os.path.join(home, ".claude", "shell"),
        ])

    return dirs


def _is_under_bootstrap_dir(file_path: str) -> bool:
    abs_file = os.path.normcase(os.path.abspath(file_path))
    for d in _bootstrap_dirs():
        abs_dir = os.path.normcase(os.path.abspath(d))
        if abs_file == abs_dir or (abs_file + os.sep).startswith(abs_dir + os.sep):
            return True
    return False


def _is_outside_git_work_tree(file_path: str) -> bool:
    d = os.path.abspath(os.path.dirname(file_path))
    root = os.path.splitdrive(d)[0] + os.sep if os.name == "nt" else os.sep
    while True:
        try:
            if os.path.exists(os.path.join(d, ".git")):
                return False
        except OSError as exc:
            print(f"nudge_em_code_dispatch: cannot stat {d}: {exc} (treating as not found)", file=sys.stderr)
        if d == root:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return True


def _is_under_general_work_root(file_path: str) -> bool:
    """Target is under the registry-declared `coordinator.general_work_root`; an unset or unreadable key exempts nothing."""
    root = general_work_scope.general_work_root()
    return bool(root) and _path_contains(root, file_path)


def _display_checkout(checkout: str, file_path: str) -> str:
    """`checkout` in the filesystem's own casing: gem_home.inside_checkout returns a normcased path
    (lowercase on Windows), which the operator should not be shown. Falls back to `checkout` as given."""
    want = os.path.normcase(checkout)
    for anc in Path(os.path.realpath(file_path)).parents:
        if os.path.normcase(str(anc)) == want:
            return str(anc)
    return checkout


def _foreign_checkout_deny(checkout: str, file_path: str) -> dict:
    return deny(
        "PreToolUse",
        f"[em-code-dispatch nudge] {_display_checkout(checkout, file_path)} is another EM's repo. "
        "Use instead: `cross-repo-memo draft <slug> --to <receiver-em>`.",
    )


def _is_bootstrap_or_out_of_repo(file_path: str) -> bool:
    if _is_under_bootstrap_dir(file_path):
        return True
    if _is_outside_git_work_tree(file_path):
        return True
    return _is_under_general_work_root(file_path)


def _derive_executor_info(file_path: str) -> tuple[str, bool]:
    ext = os.path.splitext(file_path)[1].lower()
    basename = os.path.basename(file_path)
    norm_path = file_path.replace("\\", "/")

    if any(marker in norm_path for marker in _COORDINATOR_PATH_MARKERS):
        return "coordinator-executor", False

    by_ext = _EXT_EXECUTOR_MAP.get(ext)
    if by_ext:
        return by_ext, False

    if _DEVOPS_FILENAME_RE.match(basename):
        return "devops-executor", False

    return "generic-executor", True


def _describe_edit(payload: dict) -> str:
    tool_name = payload.get("tool_name") or "Write/Edit"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        return f"{tool_name}: {len(edits)} string replacement(s) in this call"
    if "old_string" in tool_input:
        return f"{tool_name}: string replacement"
    if "content" in tool_input:
        return f"{tool_name}: full-file content write"
    return f"{tool_name} on this file"


def _build_dispatch_brief(file_path: str, executor_type: str, edit_description: str) -> str:
    del file_path
    return "\n".join(
        [
            f"type: {executor_type}",
            f"task: {edit_description}",
        ]
    )


# ---------------------------------------------------------------------------
# Semantic-bypass mechanism (AC6) — the size floor for the nudge.
#
# Purpose: the size floor for the nudge is semantic, not a learned number. An
# edit confined to whitespace, to comment/docstring text, or to a single
# identifier rename is defensible without a threshold — it is closer to
# what the nudge is FOR (steering substantive code authorship to a
# dispatched executor) than any character count would be. The ruled
# diff-measured budget is a separate layer (`_budget_decision`).
#
# Negative-spec: this bypass exists ONLY on `op()`. The async `_handler`
# above (the pcore-04 mcp_tool op) never receives old_string/new_string or
# MultiEdit edits[] — see the module's MultiEdit negative-spec — so it has
# no diff to classify and is unaffected by this mechanism.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+|\W+")


def _tokenize_for_classification(src: str) -> list | None:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None
    return [(tok.type, tok.string) for tok in tokens]


def _is_whitespace_only_diff(old: str, new: str) -> bool:
    old_tokens = _tokenize_for_classification(old)
    new_tokens = _tokenize_for_classification(new)
    if old_tokens is None or new_tokens is None:
        return False
    ignored = {
        tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
        tokenize.ENCODING, tokenize.ENDMARKER,
    }
    old_sig = [(t, s) for t, s in old_tokens if t not in ignored]
    new_sig = [(t, s) for t, s in new_tokens if t not in ignored]
    return old_sig == new_sig


def _is_comment_or_docstring_only_diff(old: str, new: str) -> bool:
    old_tokens = _tokenize_for_classification(old)
    new_tokens = _tokenize_for_classification(new)
    if old_tokens is None or new_tokens is None:
        return False
    if old_tokens == new_tokens:
        return False
    ignored = {
        tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
        tokenize.ENCODING, tokenize.ENDMARKER, tokenize.COMMENT,
    }
    old_sig = [(t, s) for t, s in old_tokens if t not in ignored]
    new_sig = [(t, s) for t, s in new_tokens if t not in ignored]
    return old_sig == new_sig


def _is_single_token_rename(old: str, new: str) -> bool:
    old_tokens = _WORD_RE.findall(old)
    new_tokens = _WORD_RE.findall(new)
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    replacements = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    if len(replacements) != 1:
        return False
    tag, i1, i2, j1, j2 = replacements[0]
    if tag != "replace":
        return False
    old_slice = old_tokens[i1:i2]
    new_slice = new_tokens[j1:j2]
    if len(old_slice) != 1 or len(new_slice) != 1:
        return False
    old_tok, new_tok = old_slice[0], new_slice[0]
    identifier_re = re.compile(r"[A-Za-z_]\w*")
    if not identifier_re.fullmatch(old_tok) or not identifier_re.fullmatch(new_tok):
        return False
    return old_tok != new_tok


def _is_semantic_bypass_edit(old: str, new: str) -> bool:
    if old == new:
        return True
    if _is_whitespace_only_diff(old, new):
        return True
    if _is_comment_or_docstring_only_diff(old, new):
        return True
    if _is_single_token_rename(old, new):
        return True
    return False


def _read_pre_edit_content(file_path: str) -> str | None:
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _semantic_bypass_applies(file_path: str, tool_input: dict) -> bool:
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        pairs: list[tuple[str, str]] = []
        for edit in edits:
            if not isinstance(edit, dict):
                return False
            old = edit.get("old_string")
            new = edit.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                return False
            pairs.append((old, new))
        return bool(pairs) and all(_is_semantic_bypass_edit(o, n) for o, n in pairs)

    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if isinstance(old_string, str) and isinstance(new_string, str):
        return _is_semantic_bypass_edit(old_string, new_string)

    content = tool_input.get("content")
    if isinstance(content, str):
        old_content = _read_pre_edit_content(file_path)
        if old_content is None:
            return False
        return _is_semantic_bypass_edit(old_content, content)

    return False


def _is_scratch_or_tmp(file_path: str) -> bool:
    import tempfile

    abs_path = os.path.abspath(file_path)
    if "scratch" in abs_path.replace("\\", "/").split("/"):
        return True
    tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    return (os.path.normcase(abs_path) + os.sep).startswith(tmp + os.sep)


def _is_scratch_py(file_path: str) -> bool:
    """A `.py` file under a `scratch/` directory: the probe-script shape."""
    if _ext_of(file_path) != ".py":
        return False
    return "scratch" in os.path.abspath(file_path).replace("\\", "/").split("/")[:-1]


_SESSION_SIZING = "<this session's sizing path>"

_UNSIZED_ADVISORY = (
    "[em-code-dispatch nudge] No sizing this session. " + fire_step()
)

_SIZED_ADVISORY = (
    "[em-code-dispatch nudge] EM edits route through the warp run. "
    + fire_step(_SESSION_SIZING)
)

_PLAN_ROUTE_ADVISORY = (
    "[em-code-dispatch nudge] This session's sizing routed to a plan. "
    + fire_step(_SESSION_SIZING)
)


_SIZE_STAGE_DENY = (
    "Size stage: change no file but the sizing. The run's execute stage makes the change "
    "and its review checks it."
)


def _size_stage_write_deny(payload: dict) -> dict | None:
    """A product write by a spawn of a live raw-ask run that has bound no sizing: only the size
    stage runs then, and a fix it writes ships unreviewed. The writer is attributed by its spawn
    transcript's workflow dir; any other subagent, or one that cannot be attributed, passes."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        return None
    if os.path.splitext(file_path)[1].lower() in _DOC_DATA_EXTENSIONS:
        return None
    if _is_bootstrap_or_out_of_repo(file_path) or _is_scratch_or_tmp(file_path):
        return None
    from coordinator_core import warp_live
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel

    try:
        spawn = terminal_commit_params_channel.spawn_transcript_path(payload)
    except Exception:
        return None
    if spawn is None:
        return None
    root = _repo_root(payload)
    session_id = payload.get("session_id")
    if root is None or not isinstance(session_id, str):
        return None
    try:
        run = warp_live.size_stage_run(root, session_id, os.path.dirname(spawn))
    except Exception:
        return None
    if run is None:
        return None
    return apply_guard_level("nudge-em-code-dispatch", deny("PreToolUse", _SIZE_STAGE_DENY))


def _budget_text(prefix: str, usage) -> str:
    return f"{prefix} {usage.files}/2 files, {usage.lines}/30 lines."


def _budget_deny(text: str) -> dict | None:
    return apply_guard_level("nudge-em-code-dispatch", deny("PreToolUse", text))


def _budget_decision(payload: dict, session_id: str, file_path: str, tool_input: dict) -> tuple[bool, dict | None]:
    """The XS-lane / residue-window answer: (handled, result). A session with no sizing opens the
    XS lane at HEAD on its first write. Not handled means the session is in
    neither state and the sizing branches decide. Runs after the escape check and ahead of the
    autonomous exemption, so autonomy cannot buy past the budget."""

    def _project() -> str:
        from coordinator_core.session import xs_lane_budget as xlb

        projected = xlb.project(file_path, tool_input)
        if projected is None:
            raise ValueError("edit cannot be projected")
        return projected

    return _budget_core(payload, session_id, file_path, _project)


def shell_write_budget(
    payload: dict,
    session_id: str,
    file_path: str,
    projected: str | None,
    unprojectable: str | None,
) -> dict | None:
    """Budget verdict for a main-EM shell write to `file_path` whose post-write text is `projected`.
    Runs the Edit/Write hook's exemption chain, then the lane/residue measure; returns the hook
    envelope (deny, residue context) or None. `projected=None` with an `unprojectable` reason is
    denied as unmeasurable only where a lane governs the session. Consumes an honoured escape
    sentinel, as the hook does: the escape is taken ahead of the lane check, so it is spent even
    where no lane would have denied."""
    if "agent_id" in payload or not file_path:
        return None
    if _is_bootstrap_or_out_of_repo(file_path):
        return None
    if os.path.splitext(file_path)[1].lower() in _DOC_DATA_EXTENSIONS:
        return None
    if _is_scratch_or_tmp(file_path):
        return None
    if _take_escape(payload, session_id, file_path, "nudge-em-code-dispatch"):
        return None

    def _project() -> str:
        if projected is None:
            raise _Unprojectable(unprojectable or "write cannot be projected")
        return projected

    handled, result = _budget_core(payload, session_id, file_path, _project)
    return result if handled else None


class _Unprojectable(Exception):
    """Carries the caller's own reason; rendered without an exception-type prefix."""


def _budget_core(payload: dict, session_id: str, file_path: str, project) -> tuple[bool, dict | None]:
    """Lane open/residue/measure logic shared by `_budget_decision` and `shell_write_budget`;
    `project()` returns the post-write text or raises."""
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root
    from coordinator_core.session import xs_lane_budget as xlb

    root = _repo_root(payload)
    if root is None or not session_id:
        return False, None
    try:
        s_plus = xlb.touched_s_or_larger(session_id, root)
        rec = xlb.read_lane(root, session_id)
        if s_plus and rec is None:
            from coordinator_core import warp_live
            from coordinator_core.git import git_state
            from coordinator_core.hooks.flag_em_poll_in_flight import session_workflow_run_ids

            landed = bool(session_workflow_run_ids(session_id)) and (
                warp_live.live_run_for_session(root, session_id) is None
            )
            head = git_state.head_sha(root) if landed else None
            if head and xlb.open_residue_window(root, session_id, head):
                rec = xlb.read_lane(root, session_id)
        if rec is None and not s_plus and not _session_sizing_state(payload, session_id)[0]:
            from coordinator_core.git import git_state

            head = git_state.head_sha(root)
            if head and xlb.open_xs_lane(root, session_id, head):
                rec = xlb.read_lane(root, session_id)
    except Exception:
        return False, None
    if rec is None:
        return False, None
    kind = rec.get("kind")
    if s_plus and kind != xlb.KIND_RESIDUE:
        return False, None
    residue = kind == xlb.KIND_RESIDUE
    if residue:
        executor, _ = _derive_executor_info(file_path)
        over_text = lambda u: _budget_text("Residue budget spent (projected):", u) + f" Dispatch {executor} for the rest."
    else:
        over_text = lambda u: _budget_text("XS budget spent (projected):", u) + " Size S or larger. " + fire_step()
    if rec.get("corrupt"):
        return True, _budget_deny(over_text(xlb.Usage(0, 0, True)))
    try:
        usage = xlb.measure(root, rec, file_path, project())
    except Exception as exc:
        reason = str(exc) if isinstance(exc, _Unprojectable) else f"{type(exc).__name__}: {exc}"
        text = f"Budget unmeasurable ({reason}). Make the change as one plain Edit per file."
        return True, _budget_deny(text)
    if usage.over:
        try:
            measured = xlb.measure(root, rec, file_path, xlb._read_text(file_path))
        except Exception:
            measured = xlb.Usage(0, 0, False)
        return True, _budget_deny(
            over_text(usage)
            + f" Measured from landed diff: {measured.files}/2 files, {measured.lines}/30 lines."
        )
    try:
        xlb.add_path(root, session_id, file_path)
    except Exception:
        pass
    if residue:
        return True, context_only(
            "PreToolUse",
            _SIZED_ADVISORY + " " + _budget_text("residue", usage),
        )
    return True, None


def op(payload: dict) -> dict | None:
    """Route the main EM's product-code write to the session's next step. Unsized, the first
    write opens the XS lane and is admitted inside the budget (`_budget_decision`); where no lane
    can open (no repo root) it is denied at guard level strict and advised at warn; sized, it is advised. The text
    repeats on every Edit and names the step for the session's sizing state. A subagent
    (`agent_id` present) is denied only as a size-stage run spawn (`_size_stage_write_deny`).

    A recorded XS lane allows in-budget edits and denies the first past 2 files / 30 changed
    lines; a landed S-or-larger session's residue edits are advised under that budget and denied
    over it (`_budget_decision`).

    Exempt: doc/data files, bootstrap/out-of-repo, semantic-bypass
    diffs, scratch/tmp, autonomous mode, and a sentinel carrying a reason
    (each honoured sentinel is logged to the session escape log).
    """
    if "agent_id" in payload:
        return _size_stage_write_deny(payload)

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = tool_input.get("file_path")

    edits = tool_input.get("edits")
    if not file_path and isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            edit_path = edit.get("file_path") or ""
            if os.path.splitext(edit_path)[1].lower() not in _DOC_DATA_EXTENSIONS:
                file_path = edit_path
                break

    if not file_path:
        return None

    if _is_bootstrap_or_out_of_repo(file_path):
        return None

    if os.path.splitext(file_path)[1].lower() in _DOC_DATA_EXTENSIONS:
        return None

    if _semantic_bypass_applies(file_path, tool_input):
        return None

    if _is_scratch_py(file_path):
        from coordinator_core.bash_guards.guard_post_executor_verification import after_executor_deny

        if resolve_mode("autonomous", _resolve_session_id_op(payload)):
            return None
        return after_executor_deny(payload, file_path)

    if _is_scratch_or_tmp(file_path):
        return None

    session_id = _resolve_session_id_op(payload)

    if _take_escape(payload, session_id, file_path, "nudge-em-code-dispatch"):
        return None

    if general_work_scope.in_general_work_session(payload.get("cwd")):
        checkout = general_work_scope.foreign_checkout(file_path)
        if checkout is not None:
            return apply_guard_level("nudge-em-code-dispatch", _foreign_checkout_deny(checkout, file_path))

    handled, budget_result = _budget_decision(payload, session_id, file_path, tool_input)
    if handled:
        return budget_result

    has_sizing, plan_pending = _session_sizing_state(payload, session_id)
    # Autonomous mode exempts a sized session only: the EM can toggle it on itself,
    # so it must not buy the way past sizing.
    if has_sizing and resolve_mode("autonomous", session_id):
        return None
    if not has_sizing:
        # Author box (strict): denied until the session sizes. Consumer (warn): advisory.
        return apply_guard_level("nudge-em-code-dispatch", deny("PreToolUse", _UNSIZED_ADVISORY))
    if plan_pending:
        return context_only("PreToolUse", _PLAN_ROUTE_ADVISORY)
    return context_only("PreToolUse", _SIZED_ADVISORY)
