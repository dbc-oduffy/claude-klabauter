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

Why the verdict is a deny
-------------------------
A PreToolUse hook has exactly one channel for returning content to the model, and it is
`permissionDecisionReason` on a deny. So "deny" here means ALREADY HANDLED, not refused:
the grep subprocess does not run, and the caller receives the output it would have
produced -- and the message now LEADS with that answer having already been produced
(never with a "Denied"/"Refused"/"Blocked" lead a caller reads as a bare refusal),
mentioning second, but still clearly, that the literal command did not run; see
"Composed message leads with the contract, not the output" below for why, and the
`_ANSWERED_MARKER` comment block further down for why the lead word itself changed.

This is the `# UNDOCUMENTED-DENY` shape (`hooks._envelope.deny`), spike-verified on
harness 2.1.193 but not a documented harness contract. That dependency was surfaced to
the PM and accepted rather than assumed.

Composed message leads with the contract, not the output (2026-08-11)
-----------------------------------------------------------------------
This module used to compose `rendered + footer` -- the caller's answer first, the
"already handled" framing last. That shape is what let a genuine leak-shaped read
happen: `cross-repo/inbox/2026-08-11-example-doctrine-repo-em-guard-unlock-banner-still-reads-
as-agent-instruction.md` Item 2 reports dispatched reviewers meeting real, correct
results under what looked like a bare denial and having to infer, from where they
sat in the text, that the results below were a substitution rather than a leaked
answer to a refused command. `check()` now composes `footer + rendered` -- the
already-handled/substitution statement is the FIRST thing in the payload, so an
agent reading top-to-bottom meets "this is a substitution, not a refusal" before it
meets anything that could otherwise be misread as a leak. The latched short marker
(`_ANSWERED_MARKER`, below) carries the identical framing standalone, since after
the first firing of a session it is the ONLY text an agent gets -- see "Session
latch" for why the short form still had to say the same thing the paragraph does.

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

Spec backlink: example-doctrine-repo state/audits/2026-07-29-sanctioned-search-path-design.md
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

#: Review: review-integrator -- Finding 3/4 (nit, mirrors guard_grep_via_
#: bash.py's own precedent): these three attributes are vestigial in
#: `bash_guards` -- `dispatch.py` imports `check` explicitly and hardcodes
#: ordering + `fail_closed` in its `guard_chain` literal rather than doing
#: attribute-based discovery (only `write_guards/tests/`, a different guard
#: family, reads `CLASS`). `CLASS = "hard-deny"` here does NOT mean this
#: guard fails closed -- see the module docstring's own Negative-spec
#: ("Does NOT fail closed") and this file's `fail_closed=False` registration
#: in `dispatch.py`. `PRIORITY` governs nothing here either; `41` happening
#: to sort ahead of `guard_grep_via_bash.py`'s `42` is coincidental, not
#: enforced -- see `dispatch.py`'s `guard_chain` list for the actual order.
CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: Disables answering alone, leaving every other grep-related verdict intact. Named
#: distinctly from `guard_grep_via_bash`'s own override so an operator can switch off
#: either seam without ambiguity about which one they turned off. Read inline at call
#: time, never hoisted -- this package's established `_override` convention.
_DISABLE_ENV_VAR = "COORDINATOR_DISABLE_INPROCESS_SEARCH"

#: SC-DR-009 (example-doctrine-repo `scoped-safety-commits.md`): the ONLY acceptable session-id source
#: for session-scoped state. `.current-session-id` is documented last-writer-wins
#: under concurrent sessions and is never an acceptable fallback for this latch -- see
#: the module docstring's "Session latch" section.
_SESSION_ID_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

#: Marker filename under `<git-COMMON-dir>/coordinator-sessions/<sid>/`, mirroring the
#: existing `bash_guards` convention for session-scoped state under that same subtree
#: (e.g. `dispatch_checks.py`'s `overrides.log`).
_LATCH_MARKER_NAME = "inprocess-search-footer-seen"

#: The one-line invariant marker every answered call carries once the explanatory
#: paragraph has already fired this session -- register contract (docs/wiki/
#: guard-messaging.md § Register): one fact, stated once, declaratively. Not a
#: substring of `_footer`'s full paragraph below -- the paragraph adds
#: "recognized as a search", which this marker omits; that divergence is what
#: `test_guard_inprocess_search.py::test_first_call_carries_full_paragraph` and
#: `test_alternative_liveness_gate.py::test_fire_guard_isolates_from_a_pre_
#: existing_session_latch` key off to assert the marker is absent from a
#: first-call (full-paragraph) reason.
_ANSWERED_MARKER = (
    "[Answered in-process: no subprocess spawned, results below are this "
    "engine's own answer.]"
)


def _repo_root_from_cwd(cwd: str) -> Optional[str]:
    """Walk upward from `cwd` looking for a `.git` entry (directory OR file -- a
    linked worktree's `.git` is a file, see `resolve_git_common_dir`'s own docstring)
    without spawning `git`. This module answers searches specifically so a Bash call
    can be avoided; shelling out to resolve the repo root would undercut its own
    reason for existing. Returns None (never raises) on any I/O failure or if no
    `.git` entry is found before the filesystem root."""
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
    """Resolve the session-scoped latch marker's path for `sid`, or None if the repo
    root can't be found. Routed through `resolve_git_common_dir` (`a6daf112`) rather
    than a literal `<repo_root>/.git` join, so a linked worktree, `--separate-git-dir`
    clone, or submodule all land the marker in the one shared location."""
    repo_root = _repo_root_from_cwd(cwd)
    if not repo_root:
        return None
    common_dir = resolve_git_common_dir(repo_root)
    return Path(common_dir) / "coordinator-sessions" / sid / _LATCH_MARKER_NAME


def _footer_seen(cwd: str, sid: str) -> bool:
    """True iff this session's explanatory paragraph has already fired. Any I/O
    failure (read-only `.git`, MinGit permissions, a mid-resolution race) degrades to
    False -- fail open toward emitting the full paragraph again, never toward
    crashing the hook."""
    try:
        path = _latch_path(cwd, sid)
        return bool(path and path.is_file())
    except Exception:
        return False


def _mark_footer_seen(cwd: str, sid: str) -> None:
    """Best-effort write of the session latch marker. Never raises: a write failure
    (read-only `.git`, MinGit permissions) means the paragraph fires again on the next
    answered call in this session -- a token-cost regression, not a correctness one,
    and strictly preferable to crashing the PreToolUse(Bash) hot path."""
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
    # constant's own comment), plus "recognized as a search" -- a PINNED
    # substring (test_guard_inprocess_search.py, test_alternative_liveness_gate.py)
    # that discriminates this full paragraph from the latched marker; keep it
    # verbatim if this paragraph is edited again.
    full = (
        "[Answered in-process: your command was recognized as a search, no "
        "subprocess spawned, results below are this engine's own answer.]"
    )

    if sid:
        try:
            _mark_footer_seen(cwd, sid)
        except Exception:
            pass

    return full


def _extract_command(payload: Dict[str, Any]) -> Optional[str]:
    """Return the CRLF-normalized `command` for a Bash PreToolUse payload, else None."""
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    command = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    return command.replace("\r", "") if command else None


def check(
    payload: Dict[str, Any], host_is_windows: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Answer a grep-shaped Bash call in-process, or return None to fall through.

    `host_is_windows` is accepted for chain-signature compatibility and deliberately
    unused: answering is strictly better than spawning on every platform, so there is no
    platform split to make here.

    C5 (row 21, `docs/reference/guard-dialect-coverage.md`) disposition: stays
    bash-only for PowerShell, by design -- its parse delegates through
    `coordinator_core.search.answer` down to `search.engine.parse_grep_segment`, a
    hand-rolled GNU-grep flag grammar with ZERO `Select-String` vocabulary, and the two
    flag surfaces do not map 1:1 even semantically (grep's `-C` is one symmetric count;
    `Select-String -Context` takes two asymmetric ones). Converting would mean authoring
    a second, parallel flag grammar for an entirely different cmdlet's option surface --
    out of this chunk's scope (`coordinator_core/search/engine.py` is outside this
    plan's declared scope list). Records SILENT for a recognized Select-String/sls
    invocation this guard can SEE but cannot safely translate -- mirroring
    `guard_grep_via_bash._check_powershell`'s shape-scoped SILENT (row 12) rather than
    declaring every PowerShell command "declined to rule" -- an ordinary, non-search
    PowerShell command genuinely clean-passes here, same as the Bash leg below returns
    bare `None` for a non-search command.
    """
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is Dialect.POWERSHELL:
        tokens = _dialect.tokenize_command(
            (
                (payload.get("tool_input") or {}).get("command")
                if isinstance(payload.get("tool_input"), dict)
                else None
            )
            or "",
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
        return None
    command = _extract_command(payload)
    if not command:
        return None
    if os.environ.get(_DISABLE_ENV_VAR, "0") == "1":
        return None
    try:
        # Review: review-integrator -- Finding 1. Both imports are deferred
        # into this try, not just `search.answer`: an import-time failure in
        # this module breaks `dispatch.py`'s own module-scope import of
        # `guard_inprocess_search.check`, which crashes the PreToolUse(Bash)
        # hook for EVERY Bash call in a session, not just search-shaped
        # ones -- so this module must stay import-safe even when
        # `_hook_envelope` or `search.answer` are mid-refactor. `check()`
        # degrades to `return None` (never raises) if either import fails.
        from coordinator_core._hook_envelope import deny
        from coordinator_core.search.answer import answer

        cwd = payload.get("cwd") or os.getcwd()
        rendered = answer(command, cwd=cwd)
    except Exception:
        return None
    if rendered is None:
        return None
    # footer FIRST, rendered SECOND: the substitution contract must be the
    # first thing an agent reads, before anything that could otherwise be
    # misread as a leaked answer beneath a denial -- see the module
    # docstring's "Composed message leads with the contract, not the output".
    return deny("PreToolUse", "%s\n\n%s" % (_footer(cwd), rendered))
