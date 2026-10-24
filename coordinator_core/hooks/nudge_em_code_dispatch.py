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
     (relayed ask R1). The residue window opens on landing (run ids, no live run),
     superseding an XS lane; an XS lane closes to hand edits after GOLF_DENY_LIMIT
     projected denies. An S+ sized session whose route has not fired is denied at strict
     until it fires; once a run is live the advisory applies, once it has landed the
     residue window applies.

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
import tokenize
from pathlib import Path, PurePosixPath

from coordinator_core._hook_envelope import deny
from coordinator_core.hooks._envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks._payload import field, present
from coordinator_core.hooks.doc_data_extensions import _DOC_DATA_EXTENSIONS
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.session.dispatch_nudge_sentinel import (
    sentinel_path as dispatch_nudge_sentinel_path,
)
from coordinator_core.hooks._em_write_budget import (  # noqa: F401 -- re-bound for callers and tests that resolve these on the hook
    _ROUTE_UNFIRED_DENY,
    _SESSION_SIZING,
    _SIZED_ADVISORY,
    _budget_core,
    _is_bootstrap_or_out_of_repo,
    _is_scratch_or_tmp,
    _session_sizing_state,
    shell_write_budget,
)
from coordinator_core.hooks.dispatch_escape import (
    _take_escape,
    _resolve_session_id_op,
)
from coordinator_core.session import general_work_scope
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
        "Use instead: `SendMessage` its live EM; none live -> "
        "`cross-repo-memo draft <slug> --to <receiver-em>`.",
    )


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


def _is_scratch_py(file_path: str) -> bool:
    """A `.py` file under a `scratch/` directory: the probe-script shape."""
    if _ext_of(file_path) != ".py":
        return False
    return "scratch" in os.path.abspath(file_path).replace("\\", "/").split("/")[:-1]


_UNSIZED_ADVISORY = (
    "[em-code-dispatch nudge] No sizing this session. " + fire_step()
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


_POST_FIRE_DENY = (
    "This session already fired engine-ask; its product edits belong to the run. "
    "Send the gap through workflow.revise, or fire a follow-up engine-ask whose em_context "
    "quotes the receipt's gap line."
)


def _post_fire_deny(session_id: str) -> dict | None:
    """Deny the main EM's product edit once the session has launched a Workflow run, whichever
    paths that run declared. Before the first fire this returns None and the XS budget decides.
    Any read failure is no signal: the deny fails open (pinned in the budget test module)."""
    if not session_id:
        return None
    try:
        from coordinator_core.hooks import flag_em_poll_in_flight

        fired = bool(flag_em_poll_in_flight.session_workflow_run_ids(session_id))
    except Exception:
        return None
    if not fired:
        return None
    return apply_guard_level("nudge-em-code-dispatch", deny("PreToolUse", _POST_FIRE_DENY))


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


def op(payload: dict) -> dict | None:
    """Route the main EM's product-code write to the session's next step. Unsized, the first
    write opens the XS lane and is admitted inside the budget (`_budget_decision`); where no lane
    can open (no repo root) it is denied at guard level strict and advised at warn; sized, it is advised. The text
    repeats on every Edit and names the step for the session's sizing state. A subagent
    (`agent_id` present) is denied only as a size-stage run spawn (`_size_stage_write_deny`).

    A recorded XS lane allows in-budget edits and denies the first past 2 files / 30 changed
    lines (`_budget_decision`). A lane closes after GOLF_DENY_LIMIT projected denies.

    Once the session has fired a Workflow run, live or landed, every product Edit is denied
    (`_post_fire_deny`); only the shell-write path still measures a residue window
    (pinned by test_landed_residue_shell_write_is_budgeted).

    An S+ sized session whose route has not fired is denied at strict until it fires (advised
    at warn). `touched_s_or_larger` reads the session's whole touch record, so any S+ (or
    unreadable) sizing it touched counts, not only the one being delivered.

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

    post_fire = _post_fire_deny(session_id)
    if post_fire is not None:
        return post_fire

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
