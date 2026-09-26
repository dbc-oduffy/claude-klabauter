"""coordinator_core.bash_guards.guard_inprocess_search -- PreToolUse(Bash) seam that
ANSWERS a grep-shaped command instead of letting it spawn one.

This is the sanctioned non-bash search path for a main EM session. It exists because
there is no other: `Grep`/`Glob` are unreachable from a top-level session on this
harness build (confirmed at every layer under this project's control -- permissions,
launcher, agent frontmatter with `Tools: *`, and tool deferral), so an agent that wants
to search has historically had only bash.

Why answering beats rewriting
-----------------------------
The PreToolUse(Bash) dispatcher is already a Python process, spawned on every Bash tool
call. A search executed in THAT process costs zero additional forks. The pre-existing
alternative (`dispatch_checks.check_grep_via_bash_rewrite`) rewrites the command into a
SECOND `python3 -c` subprocess -- paying interpreter startup twice and saving no fork on
the shape that actually dominates.

Measured against a 69,329-command corpus: the rewrite covers 1.068% of real
search-shaped commands, because it can only ever attempt a bare single-segment grep, and
that is 2.7% of real usage. Answering in-process -- including absorbing the downstream
`| head` / `| wc -l` stage -- covers 14.65% of the same corpus.

Why the verdict is a rewrite, not a deny (2026-08-13)
------------------------------------------------------
A successful in-process answer is SUCCESS, not a refusal, and it must arrive through a
channel the harness itself marks as success. This module used to answer via `deny()` +
`permissionDecisionReason` -- the ONLY channel available before `updatedInput` was
verified binding on harness 2.1.220 (see `nudge_foreground_agent_dispatch.py`'s own
2026-07-31 re-land notes) -- which meant a correct, already-computed answer was
delivered inside the harness's `<error>` framing on every call. A dispatched agent in a
sibling repo read that framing as "your command was recognized as a search, no
subprocess spawned, results below are this engine's own answer" wrapped in `<error>`,
called it "a fabricated banner... not something I recognize as genuine harness output",
and re-did the work by reading files directly rather than trust results that were
already correct: `cross-repo/inbox/2026-08-13-example-cockpit-repo-em-guard-advisories-read-
as-injection-to-subagents.md`, instance 3. `state/audits/2026-08-11-guard-text-
injection-mechanism-proof.md` records the same trust-erosion class.

`check()` now answers via `hooks._envelope.rewrite_input` -- Shape (f), documented and
harness-verified BINDING (2.1.220), not the `# UNDOCUMENTED-DENY` spike-only shape.
`updated_input` replaces the tool's `command` with `true` (a real, PATH-resolvable
coreutils no-op: exits 0, no output, no filesystem or network effect -- not the shell
builtin `:`, which `_alternative_liveness.py`'s guard-message-liveness gate flags DEAD
since it has no on-PATH binary to resolve) while preserving every other key of the
original `tool_input`, and the rendered answer plus the footer travel in
`additionalContext` -- `rewrite_input`'s success channel, never
`permissionDecisionReason`. The harness runs the no-op in place of the real search, so
"no subprocess spawned" stays literally true in the sense that matters (no `grep`/`rg`/
etc. process forks; the trivial `true` costs nothing worth measuring against the search
this module already computed), and the model reads the answer as ordinary tool output,
not as an error.

This module no longer imports `deny()` at all: `_extract_command` already requires
`tool_input` to be a dict carrying a `command` string before `check()` reaches the
rewrite branch, so by the time a rewrite is built there is always a complete input
object to copy and replace `command` in. A caller that reaches this point without a
well-formed `tool_input` never gets past `_extract_command`'s own `None` return, and
`check()` falls through to the real command exactly as every other declined shape does.

Composed message leads with the contract, not the output (2026-08-11, envelope changed 2026-08-13)
-----------------------------------------------------------------------------------------------------
This module used to compose `rendered + footer` -- the caller's answer first, the
"already handled" framing last. That shape is what let a genuine leak-shaped read
happen: `cross-repo/inbox/2026-08-11-doe-claude-em-guard-unlock-banner-still-reads-
as-agent-instruction.md` Item 2 reports dispatched reviewers meeting real, correct
results under what looked like a bare denial and having to infer, from where they
sat in the text, that the results below were a substitution rather than a leaked
answer to a refused command. `check()` composes `footer + rendered` -- the
already-handled/substitution statement is the FIRST thing in `additionalContext`, so
an agent reading top-to-bottom meets "this ran as a substitution" before it meets
anything that could otherwise be misread as a leak. The latched short marker
(`_ANSWERED_MARKER`, below) carries the identical framing standalone, since after
the first firing of a session it is the ONLY text an agent gets -- see "Session
latch" for why the short form still had to say the same thing the paragraph does.
The ordering rule survived the 2026-08-13 move off `deny()`; only the envelope key
carrying the composed text changed (`additionalContext`, not
`permissionDecisionReason`).

Register (docs/wiki/guard-messaging.md § Register, trimmed 2026-08-13)
--------------------------------------------------------------------------
The pre-2026-08-13 paragraph closed with "...results below are this engine's own
answer" -- self-legitimacy work: the more a banner protests its own authenticity,
the more it reads as an attempt to be believed rather than as an ordinary fact.
Boring is the trust signal, not reassurance. `_footer()`'s text now states the two
facts the reader needs (recognized as a search / no subprocess spawned) once each
and stops -- it does not additionally vouch for itself.

Ordering
--------
Registered BEFORE `guard_grep_via_bash` in `dispatch.py`'s chain. An answered search
needs neither that guard's Windows deny nor its macOS advisory -- there is no subprocess
left to warn about. Anything this module declines falls through to that guard entirely
unchanged, which is why it owns no verdict of its own beyond "answered".

Negative-spec -- what this module deliberately does NOT do:
  - Does NOT modify, wrap, or shadow `guard_grep_via_bash`'s logic. It runs earlier and
    returns None on every command it cannot answer, leaving that guard's contract and
    its test suite untouched.
  - Does NOT fail closed. Any unexpected error degrades to normal bash behaviour. This
    runs on the hot path for EVERY Bash call; a defect here must never wedge a tool call.
  - Does NOT answer partially. `search.answer` returns either a faithful answer or None
    -- there is no "probably right" result, because the caller has no reason to re-check
    one that looks plausible.
  - Does NOT full-latch the footer. See "Session latch on the explanatory paragraph"
    below -- every answered call still carries an unambiguous already-handled signal,
    first call or Nth.
  - Does NOT ship a successful answer through the harness's error/deny channel. A
    completed in-process search is SUCCESS -- `check()` returns `rewrite_input`
    (`updatedInput` + `additionalContext`), never `deny` (`permissionDecisionReason`),
    for the answered path. `cross-repo/inbox/2026-08-13-example-cockpit-repo-em-guard-
    advisories-read-as-injection-to-subagents.md` instance 3 is the record of what
    happens when a correct answer arrives framed as a failure: a dispatched agent
    distrusted genuinely correct results and redid the work. This property is pinned
    by `TestSuccessNeverShipsAsError` in this module's own test suite, not by a
    single string match -- it asserts the shape of every answered `check()` return,
    not one banner's wording.

Session latch on the explanatory paragraph (AC5, 2026-08-01)
--------------------------------------------------------------
`_footer()` used to render its ~45-word explanatory paragraph on EVERY answered call,
byte-identical every time -- legal under Axis A (it asks nothing of the agent) but a
report-clause violation: it carries no new state and repeats with substantively
identical text call after call within one session.

Prior art, read before touching this section again rather than re-deriving it:
  - `_helpers.operator_override_note`'s own docstring records that a session-keyed
    marker-file dedup was drafted for ITS cut and discarded, on the argument that "a
    per-firing pointer is cheaper than session-keyed state and has nothing to get
    wrong, since there is no marker file, no atomicity concern, and no fail-open leg to
    reason about." That is the right call for a ONE-LINE pointer sentence. It is the
    wrong call here: this paragraph is ~45 words repeated on every answered search in a
    session that may run hundreds of them, so the amortized cost of the state this
    module now carries (one small marker file, one stat, one write, both wrapped to
    fail open) is paid back many times over by the tokens saved -- a different cost
    profile from the pointer sentence `operator_override_note` renders, not a silent
    re-opening of that discarded design.
  - `_blanket_disarm.py`'s `(session_id, is_em)` cache is a PROCESS-LOCAL memoization
    covering several guards consulting the same marker within one dispatch chain
    invocation -- it does not survive across the spawn-per-call boundary and is not a
    session latch in the sense this module needs. Before this change there was no
    SESSION-persisted latch, throttle, or seen-set anywhere in `bash_guards`; this is
    the first one.

Mechanism: a marker file at
`<git-COMMON-dir>/coordinator-sessions/<sid>/inprocess-search-footer-seen`, `<sid>`
resolved from the `CLAUDE_CODE_SESSION_ID` environment variable only (SC-DR-009;
never `.current-session-id`, which is documented last-writer-wins under concurrent
sessions and would let one session's marker answer for another's). The git COMMON dir
is resolved via `coordinator_core.git.git_dir.resolve_git_common_dir`
(`a6daf112`'s fix for the same literal-`.git`-join failure mode, reused rather than
re-derived) so a linked worktree, `--separate-git-dir` clone, or submodule lands the
marker in the one shared location every topology resolves to. Absent session id, an
unresolvable repo root, or any I/O failure on the read or the write: fail OPEN toward
emitting the FULL paragraph -- this module's own negative-spec already forbids failing
closed, and a spurious full paragraph is a token-cost regression, never a correctness
one.

Spec backlink: DoE-claude state/audits/2026-07-29-sanctioned-search-path-design.md
Consumes: coordinator_core/search/answer.py, coordinator_core/git/git_dir.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards import _dialect
from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _segments_from_tokens_with_pipe_flag,
    token_matches_binary as _token_matches_binary,
)
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.git.git_dir import resolve_git_common_dir

#: in `dispatch.py`. `PRIORITY` governs nothing here either; `41` happening
CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

_DISABLE_ENV_VAR = "COORDINATOR_DISABLE_INPROCESS_SEARCH"

#: SC-DR-009 (DoE `scoped-safety-commits.md`): the ONLY acceptable session-id source
_SESSION_ID_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

_LATCH_MARKER_NAME = "inprocess-search-footer-seen"

_ANSWERED_MARKER = (
    "[Answered in-process: no subprocess spawned.]"
)


def _repo_root_from_cwd(cwd: str) -> Optional[str]:
    try:
        current = os.path.abspath(cwd)
    except OSError:
        return None
    seen = set()
    while current not in seen:
        seen.add(current)
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return current
        except OSError:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def _latch_path(cwd: str, sid: str) -> Optional[Path]:
    repo_root = _repo_root_from_cwd(cwd)
    if not repo_root:
        return None
    common_dir = resolve_git_common_dir(repo_root)
    return Path(common_dir) / "coordinator-sessions" / sid / _LATCH_MARKER_NAME


def _footer_seen(cwd: str, sid: str) -> bool:
    try:
        path = _latch_path(cwd, sid)
        return bool(path and path.is_file())
    except Exception:
        return False


def _mark_footer_seen(cwd: str, sid: str) -> None:
    try:
        path = _latch_path(cwd, sid)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except Exception:
        return


def _footer(cwd: str) -> str:
    """Built at call time, never hoisted to a module constant (see
    `_DISABLE_ENV_VAR`'s own comment).

    As of 2026-08-11 (docs/plans/2026-08-11-guard-messages-point-to-docs-
    never-name.md, C3): renders NO override pointer at all. This path
    accompanies a SUCCESSFULLY ANSWERED search, not a denial -- a
    non-denial has no bypass to offer, so `operator_override_note` is not
    called here (it previously named `_DISABLE_ENV_VAR`, mirroring every
    other guard's advisory; that mirroring was the defect this chunk
    corrects, not a shape worth softening). An operator who wants to
    disable in-process answering still finds `_DISABLE_ENV_VAR` documented
    in `docs/reference/guard-override-keys.md`.

    As of 2026-08-01 (AC5): latches the EXPLANATORY PARAGRAPH, not the whole
    footer -- see the module docstring's "Session latch on the explanatory
    paragraph" section. `sid` absent, repo root unresolvable, or any latch
    I/O failure all fail OPEN toward the full paragraph (never toward a bare
    marker with no already-handled signal, and never toward crashing)."""
    sid = os.environ.get(_SESSION_ID_ENV_VAR) or ""
    if sid:
        try:
            if _footer_seen(cwd, sid):
                return _ANSWERED_MARKER
        except Exception:
            pass

    # States the identical fact `_ANSWERED_MARKER` carries standalone (see that
    full = (
        "[Answered in-process: recognized as a search, no subprocess spawned.]"
    )

    if sid:
        try:
            _mark_footer_seen(cwd, sid)
        except Exception:
            pass

    return full


def _extract_command(payload: Dict[str, Any]) -> Optional[str]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    command = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    return command.replace("\r", "") if command else None


def check(
    payload: Dict[str, Any], host_is_windows: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is Dialect.POWERSHELL:
        command = (
            (payload.get("tool_input") or {}).get("command")
            if isinstance(payload.get("tool_input"), dict)
            else None
        ) or ""
        command = command.replace("\r", "") if command else ""
        tokens = _dialect.tokenize_command(
            command,
            Dialect.POWERSHELL,
            guard_name="guard_inprocess_search",
        )
        if tokens is not None:
            for seg_tokens, _pipe_before in _segments_from_tokens_with_pipe_flag(tokens):
                if seg_tokens and any(
                    _token_matches_binary(seg_tokens[0], b)
                    for b in ("Select-String", "sls")
                ):
                    record_silent(
                        "guard_inprocess_search",
                        "PowerShell dialect: Select-String/sls invocation "
                        "recognized, but search.answer's grep-flag grammar "
                        "(search.engine.parse_grep_segment) has zero "
                        "Select-String vocabulary and the two flag surfaces "
                        "do not map 1:1",
                    )
                    break
        if not command:
            return None
        if os.environ.get(_DISABLE_ENV_VAR, "0") == "1":
            return None
        try:
            from coordinator_core._hook_envelope import rewrite_input
            from coordinator_core.search.answer import answer

            cwd = payload.get("cwd") or os.getcwd()
            rendered = answer(command, cwd=cwd, tool_name="PowerShell")
        except Exception:
            return None
        if rendered is None:
            return None
        tool_input = payload.get("tool_input") or {}
        updated_input = dict(tool_input)
        updated_input["command"] = "true"
        return rewrite_input(
            "PreToolUse", updated_input, context="%s\n\n%s" % (_footer(cwd), rendered)
        )
    command = _extract_command(payload)
    if not command:
        return None
    if os.environ.get(_DISABLE_ENV_VAR, "0") == "1":
        return None
    try:
        from coordinator_core._hook_envelope import rewrite_input
        from coordinator_core.search.answer import answer

        cwd = payload.get("cwd") or os.getcwd()
        rendered = answer(command, cwd=cwd)
    except Exception:
        return None
    if rendered is None:
        return None
    # cost against the search already computed) -- `rewrite_input` REPLACES
    tool_input = payload.get("tool_input") or {}
    updated_input = dict(tool_input)
    updated_input["command"] = "true"
    return rewrite_input(
        "PreToolUse", updated_input, context="%s\n\n%s" % (_footer(cwd), rendered)
    )
