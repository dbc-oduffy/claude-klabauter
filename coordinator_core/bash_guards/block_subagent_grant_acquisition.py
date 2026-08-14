"""coordinator_core.bash_guards.block_subagent_grant_acquisition -- PreToolUse
(Bash) hard-deny guard closing the ACQUISITION-side hole in the doctrine-
surface write grant: a dispatched subagent invoking `coordinator_core.session.
claude_md_grant`'s `grant` subcommand (over `python -m`/`python3 -m`, or over
an inline `python -c`/`python3 -c` import-and-call) to mint its own
`granted_by: "pm"` record and clear `block_unauthorized_claude_md_write`'s
gate for itself.

SPEC: `docs/plans/2026-08-08-discriminate-the-caller-on-the-write-grant.md`,
chunks C1a/C1b (this module), read together with that plan's `## Problem`,
`## Two bypass vectors, now in scope`, and `## Complementarity and the
residual that survives both legs` sections, which this docstring does not
restate in full.

THE HOLE THIS CLOSES. `claude_md_grant.py` validates `granted_by` against a
VALUE enum (`_VALID_GRANTED_BY = frozenset({"pm"})`), never against WHO is
calling. `block_unauthorized_claude_md_write`'s own deny text names the exact
`grant pm "<note>"` CLI invocation as the reader's remediation -- and that
remediation is self-clearing for every member of its audience, because its
deny leg allows unconditionally when no `agent_id` is present, so the only
callers who ever see the deny are dispatched subagents. Confirmed live
2026-08-08: a subagent hit the deny, ran the named remediation verbatim, and
produced a grant record reading `granted_by: "pm"` for an edit no human
authorized (`cross-repo/inbox/2026-08-08-doe-claude-em-claude-md-grant-self-
minted-as-pm.md`). The intended flow -- EM obtains PM ratification, EM
acquires the grant, EM dispatches, the subagent's write inherits the EM's
grant via shared-session-id resolution -- stays intact under this fix; only
ACQUISITION is restricted to the EM. See the plan's "What the grant was
actually for" subsection for why inheritance, not a new `granted_by` value,
is the right shape.

IDENTITY-GATE POSTURE -- fail CLOSED, modeled on `block_subagent_stash_
creation.py`'s posture (itself modeled on `block_subagent_commit.py`'s), and
DELIBERATELY NOT modeled on either of this package's fail-open siblings:

  - `block_subagent_destructive_action.py` resolves a subagent KIND and
    falls open (allow) when a present `agent_id` fails to resolve one --
    correct there because that guard routes to per-kind messaging and
    forward-paths, and an unresolvable kind still needs a caller to reach
    some allowed path.
  - `block_subagent_plan_body_bash_write.py` was observed allowing when its
    own back-pointer chain cannot resolve a subagent's kind.

Neither posture is safe here. This guard has exactly one allowed caller (the
EM, i.e. no `agent_id` on the payload at all) and exactly one gated action
(acquiring the grant); there is no per-kind branching that a resolution
failure could legitimately need to fall through to. The discriminator is the
RAW presence of the harness-supplied `agent_id` field alone, not whether it
resolves further -- `agent_id` present but unresolvable is still,
unambiguously, "not the EM", so it denies. Falling open on an unresolvable
`agent_id` here would silently re-open the exact hole this module exists to
close.

HEURISTIC-NOT-EXHAUSTIVE -- stated plainly, not left implicit. Bash-side
recognition below, `-m` and `-c` alike, is a token-shape CLASSIFIER: it
pattern-matches the segment-walked, de-quoted argv shape of a Bash command
against the known module path and function names. It is not, and cannot be
made, exhaustive. A subagent that writes a script file to disk and then
executes that file, or that reaches `claude_md_grant`'s write function
through some other indirection this token-walk does not tokenize for (a
wrapper, an alternate interpreter, a serialized/obfuscated argv the
tokenizer cannot resolve), is NOT caught by this module. This is not framed
here as "the only illegitimate route" or any other completeness claim --
this plan was corrected once already for exactly that overstatement (see
the plan's Problem section footnote), and this docstring does not repeat it.

What backstops the residual: `block_subagent_grant_record_write`
(`coordinator_core/write_guards/block_subagent_grant_record_write.py`, plan
chunk C9, authored concurrently) denies the Write/Edit/MultiEdit/
NotebookEdit tool channel to the grant-record artifact
(`.git/coordinator-sessions/<sid>/claude-md-write-grant.json`) directly, at
its git-common-dir-RESOLVED location -- independent of whether this module's
Bash-side token-shape recognition catches the invocation that produced the
write. The two legs cover different channels (Bash tool-call token shape
here; Write-tool-call path there) and neither subsumes the other -- see the
plan's "Complementarity and the residual that survives both legs" for the
one channel that still survives both (a write performed *inside* a spawned
Python process reached by an unrecognized Bash shape never surfaces as a
Write-tool call for C9 to see either).

RECOGNIZED SHAPES (classification only -- see C1b for the identity gate and
deny text that consume these classifiers):

  1. `python`/`python3` + `-m` + module `coordinator_core.session.
     claude_md_grant` + a `grant` subcommand token immediately following the
     module path. `read` and `check` are explicitly NOT gated (AC4) -- this
     guard's audience is acquisition, not every invocation of the CLI.
  2. WIDENED per PM ruling 2026-08-08 (AC11): `python`/`python3` + `-c` +
     a following string argument that both (a) contains the substring
     `coordinator_core.session.claude_md_grant` and (b) references the
     grant-writing function name `write_claude_md_write_grant` -- e.g.
     `python3 -c "from coordinator_core.session.claude_md_grant import
     write_claude_md_write_grant as w; w('pm', '...')"`. The identical
     substring check against a `-c` payload that instead references only
     `read_claude_md_write_grant`/`check_claude_md_write_grant` is NOT
     grant-shaped and is not classified here, matching the `-m` form's
     subcommand discrimination.

Tokenization and shell-shape handling (env-assignment skipping, subshell
peeling, `;`/`&&`/`|` segmentation, heredoc-body stripping, bundled-short-
flag recognition) is REUSED from `block_subagent_destructive_action.py` and
`_command_tokenizer.py`, never re-implemented -- see those modules' own
docstrings for the bypass history each closes.

OVERRIDE -- none at this layer. `COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE` is a
separate, documented rare-use escape hatch for `block_unauthorized_claude_md_
write`'s own gate and is out of this plan's scope (see plan `## Out of
scope`); this module does not consult it.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._command_tokenizer import (
    _extract_dash_c_payload,
)

CLASS = "hard-deny"
MATCHERS = ("Bash",)
#: `dispatch.py` hardcodes chain ordering explicitly, so this value governs
#: nothing at runtime; matches the sibling identity-gated hard-denies
#: (`block_subagent_commit`, `block_subagent_destructive_action`,
#: `block_subagent_stash_creation`) it is registered near.
PRIORITY = 40

#: The dotted module path this guard gates acquisition of, matching
#: `claude_md_grant.py`'s own `python3 -m` invocation contract documented in
#: that module's `main()` docstring.
_GRANT_MODULE = "coordinator_core.session.claude_md_grant"

#: Only `grant` acquires the authorization this guard exists to gate --
#: `read`/`check` are informational and explicitly NOT gated (AC4).
_GATED_SUBCOMMANDS = frozenset({"grant"})

#: The grant-writing function name -- a `-c` payload referencing this is
#: grant-shaped, matching the `-m` form's `grant` subcommand.
_WRITE_FUNC_NAME = "write_claude_md_write_grant"

#: Read/check-shaped function names -- referenced here only so a `-c`
#: payload calling one of these (and not `_WRITE_FUNC_NAME`) is documented
#: as the deliberate non-match, not an oversight. Not consulted as a
#: positive test anywhere below: absence of `_WRITE_FUNC_NAME` is already
#: sufficient to not-classify, per the HEURISTIC-NOT-EXHAUSTIVE posture --
#: this guard does not need to prove a payload is read/check-shaped, only
#: that it is not grant-shaped.
_READ_CHECK_FUNC_NAMES = frozenset(
    {"read_claude_md_write_grant", "check_claude_md_write_grant"}
)

#: `python`, `python3`, `python3.11`, `python2` -- re-declared identically
#: to `_sentinel_creation_guard.py`'s own constant of the same name (not
#: re-exported there), per this package's established reuse convention.
_PYTHON_BASENAME_RE = re.compile(r"^python[0-9.]*$")

#: Bundled `-m<module>` form, e.g. `python3 -mcoordinator_core.session.
#: claude_md_grant` -- CPython's own arg parser accepts the module path
#: attached directly to `-m` with no intervening space, same as `-c`'s
#: documented bundled-short-flag acceptance elsewhere in this package.
_DASH_M_BUNDLED_RE = re.compile(r"^-m(.+)$")


def _extract_dash_m_module_and_rest(
    argv_after_interpreter: List[str],
) -> Optional[Tuple[str, List[str]]]:
    """Scan `argv_after_interpreter` for a standalone `-m <module>` or a
    bundled `-m<module>` flag, and return `(module, rest)` where `rest` is
    every token following the module path (the invoked module's own argv,
    e.g. `["grant", "pm", "<note>"]`) -- or `None` if no `-m` flag is
    present. Mirrors `_command_tokenizer._extract_dash_c_payload`'s own
    bundled/standalone scan shape for the sibling `-c` flag.
    """
    n = len(argv_after_interpreter)
    for i, tok in enumerate(argv_after_interpreter):
        if tok == "-m":
            if i + 1 >= n:
                return None
            return argv_after_interpreter[i + 1], argv_after_interpreter[i + 2:]
        m = _DASH_M_BUNDLED_RE.match(tok)
        if m:
            return m.group(1), argv_after_interpreter[i + 1:]
    return None


def _classify_dash_m_invocation(working: List[str]) -> Optional[str]:
    """Classify a single already-segmented, subshell/env-stripped token
    list `working` (head token is the interpreter itself) for the `-m`
    grant-acquisition shape. Returns a deny_kind label, or `None` if this
    segment is not a gated `-m` invocation of `claude_md_grant grant`.
    """
    found = _extract_dash_m_module_and_rest(working[1:])
    if found is None:
        return None
    module, rest = found
    if module != _GRANT_MODULE:
        return None
    subcmd = rest[0] if rest else None
    if subcmd not in _GATED_SUBCOMMANDS:
        return None
    return f"python -m {_GRANT_MODULE} {subcmd}"


#: Word-boundary match for `_WRITE_FUNC_NAME` inside a `-c` payload --
#: plain substring containment (the prior form of this check) would also
#: classify an unrelated identifier that merely CONTAINS the function name
#: as a substring (e.g. `_write_claude_md_write_grant_helper`) as
#: grant-shaped, which is wider than this module's own "references the
#: grant-writing function name" claim (AC10). `(?<![A-Za-z0-9_])` /
#: `(?![A-Za-z0-9_])` are identifier-boundary lookarounds, not `\b` --
#: `\b` alone would still treat a leading digit boundary inconsistently
#: with Python identifier rules; the explicit character classes match
#: exactly the set of characters Python identifiers are made of.
_WRITE_FUNC_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])" + re.escape(_WRITE_FUNC_NAME) + r"(?![A-Za-z0-9_])"
)


def _classify_dash_c_invocation(working: List[str]) -> Optional[str]:
    """Classify a single already-segmented, subshell/env-stripped token
    list `working` for the WIDENED (AC11) `-c` inline-import grant shape.
    Returns a deny_kind label, or `None` if this segment's `-c` payload (if
    any) does not reference both the grant module and the grant-writing
    function name as a whole identifier (not merely as a substring of some
    other identifier -- see `_WRITE_FUNC_NAME_RE`).
    """
    payload = _extract_dash_c_payload(working[1:])
    if payload is None:
        return None
    if _GRANT_MODULE not in payload:
        return None
    if not _WRITE_FUNC_NAME_RE.search(payload):
        return None
    return f"python -c ...{_GRANT_MODULE}.{_WRITE_FUNC_NAME}..."


def _evaluate(cmd: str) -> Optional[str]:
    """Primary classification pass: segment-walk `cmd`'s tokenized shape
    (per-segment, quote-aware `;`/`&&`/`|` split) and classify each segment
    against both the `-m` and `-c` grant-acquisition shapes. Returns the
    first deny_kind label found, or `None` if nothing in `cmd` matches
    either recognized shape (see module docstring "HEURISTIC-NOT-
    EXHAUSTIVE" -- `None` here is "not recognized", never a claim that the
    command is safe).
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return None

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue

        working = _strip_leading_subshell_and_env(seg_tokens)
        if not working:
            continue

        head_base = _normalize_executable_basename(working[0])
        if not _PYTHON_BASENAME_RE.match(head_base):
            continue

        verdict = _classify_dash_m_invocation(working)
        if verdict is not None:
            return verdict

        verdict = _classify_dash_c_invocation(working)
        if verdict is not None:
            return verdict

    return None


def _deny_reason(cmd: str, deny_kind: str) -> str:
    """Design-as-offer deny text for a recognised grant-acquisition
    invocation carrying an `agent_id` (see module docstring "IDENTITY-GATE
    POSTURE" -- this only ever renders for a dispatched subagent, never the
    EM).

    `deny_kind` and `cmd` are accepted for call-site parity with the
    classifier but deliberately UNUSED in the rendered text below -- a
    dispatched subagent is never shown the governed module path, the
    gated subcommand, or its own attempted command, because any of those
    is a pasteable recipe to the one audience this guard exists to keep in
    the dark (see module docstring "THE HOLE THIS CLOSES" -- this is the
    exact self-clearing shape confirmed live 2026-08-08). Tone per this
    chunk's brief, functional not decorative: this deny fires almost
    exclusively on agents acting in good faith, so it does not frame the
    caller's own work as suspect. It states the STRUCTURAL reason a
    dispatched subagent cannot itself confirm PM ratification -- it never
    speaks to the PM directly, only hears what its EM tells it, true of
    every subagent on every dispatch -- and hands the agent the pieces its
    BLOCKED report needs, never a command the agent could run itself to
    clear its own gate. Matches the register of
    `block_unauthorized_claude_md_write._deny_reason` (C4), read as the
    tone precedent for this text.
    """
    del deny_kind, cmd
    return (
        "BLOCKED: acquiring a CLAUDE.md write grant is an EM action, not a "
        "dispatched agent's.\n\n"
        "This is not a judgment on the work behind this call -- a "
        "dispatched agent cannot see, from where it stands, whether its "
        "dispatch carries a PM ratification; it only hears what its EM "
        "tells it. Confirming that is the EM's call, not something to "
        "resolve from here. Report BLOCKED to your EM with:\n"
        "  Reason: grant acquisition is EM-only; a subagent's write "
        "should inherit the EM's own grant via shared-session-id "
        "resolution, not mint one of its own.\n"
        "  Unblock: the EM obtains PM ratification, then acquires the "
        "grant itself in the session that will dispatch the write, before "
        "dispatching the write."
    )


def check(payload: dict) -> Optional[dict]:
    """Evaluate the grant-ACQUISITION gate against a PreToolUse payload.

    Identity-gated to subagents only -- fail CLOSED on a present-but-
    unresolvable `agent_id`, modeled on `block_subagent_stash_creation.py`'s
    posture (see module docstring "IDENTITY-GATE POSTURE" for why this
    guard, unlike its fail-open siblings, has no per-kind branching a
    resolution failure could legitimately fall through to). Gates on the
    raw presence of `agent_id` alone -- never `resolve_effective_types`'
    OR-resolved triple, per this chunk's brief: Claude Code documents
    `agent_type` as also populated for a session launched with `--agent`,
    so an EM on such a session would resolve as NOT-EM under that broader
    triple and have a legitimate grant denied. `agent_id` is documented
    subagent-only.
    """
    # Deliberately no try/except -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards.
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    # EM/subagent discriminator -- raw presence of `agent_id` alone (AC5),
    # not whether it resolves further. A present-but-unresolvable
    # `agent_id` is still, unambiguously, "not the EM" (AC3) -- see module
    # docstring "IDENTITY-GATE POSTURE".
    raw_agent_id = payload.get("agent_id")
    if not raw_agent_id:
        return None

    cmd_for_classification = _strip_heredoc_bodies(cmd)

    deny_kind = _evaluate(cmd_for_classification)
    if deny_kind is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, deny_kind),
        }
    }
