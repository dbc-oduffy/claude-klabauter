"""coordinator_core.bash_guards._dc_git_misc -- git-misc guard families: no-verify, destructive git orphan, git clean, git revert.

Folded: ``check_no_verify`` family
(block-no-verify.sh): denies a git invocation carrying a hook-bypass flag
(``--no-verify``, ``--no-gpg-sign``, ``-c commit.gpgsign=false``), including
through ``sh -c`` and heredoc-fed shell interpreters.

Section 1 of the former ``dispatch_checks`` module, moved verbatim.

Folded: guard family 2 of the
``dispatch_checks`` split: ``check_destructive_git_orphan``
(block-destructive-git-orphan.sh) and its positional git-argv helpers.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.

Folded: guard family 4 of the
``dispatch_checks`` split: ``check_destructive_git_clean``
(block-destructive-git-clean.sh).

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.

Folded: guard family 5 of the
``dispatch_checks`` split: ``check_destructive_git_revert``
(block-destructive-git-revert.sh), including its advisory twin
``check_destructive_git_revert_advisory``.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Callable, Dict, List, Optional, Tuple
from coordinator_core.bash_guards import _dc_git_probe
from coordinator_core.bash_guards._command_tokenizer import (
    _skip_wrapper_own_argv,
    exceeds_tokenizable_ceiling as _bt_exceeds_tokenizable_ceiling,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._dc_git_probe import (
    _new_dirty_paths_memo,
    _rm_peer_claim_of,
)
from coordinator_core.bash_guards._dc_git_shared import (
    _BYPASS_PREFIX,
    _FREETEXT_OPERAND_FLAGS,
    _bt_cd_chain_cwds_by_segment,
    _bt_leading_cd_prefix_cwd,
    _extract_git_c_dir,
    _is_loadbearing,
    _new_git_memo,
    _orphan_c_cwd,
    _ps_git_bypass_segments,
)
from coordinator_core.bash_guards._dc_shell_text import (
    _RM_WRAPPER_WORDS,
    _heredoc_shell_payloads,
    _join_backslash_newlines,
    _restore_protected_quotes,
    _shell_c_unwrap_payloads,
    _split_segments,
    _strip_heredoc_bodies,
    _strip_heredoc_bodies_for_prose_scan,
    _strip_q,
    _strip_shell_comments_for_prose_scan,
    _strip_ws_quoted_spans,
    _word_present,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    expand_start_process_invocations,
    resolve_segments_for_dialect,
    tokenize_command,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._rewrite_support import (
    _GIT_GLOBAL_OPT_NO_ARG_SIMPLE,
    _GIT_GLOBAL_OPT_WITH_ARG,
    _alt_record,
    _crlf_strip,
    _offer,
    _override,
    _ready,
)
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _normalize_windows_argv0_head_path_with_spaces,
)


# ---------------------------------------------------------------------------
# 1. check_no_verify -- block-no-verify.sh
# ---------------------------------------------------------------------------

#: Catastrophic-backtracking fix, second cause (2026-08-05, same corpus):
#: the previous single-regex `_BYPASS_RE` put an unbounded `.*` between
#: `git\s+` and the literal flag alternation -- `git\s+(.*\s)?(flag-alts)`
#: -- so on a segment with a `git` head but no bypass flag, the engine tries
#: every possible split of `.*` before concluding no match, and `re.search`
#: repeats that whole O(n) probe at every segment-boundary candidate
#: (`^`/`&&`/`||`/`;`/`|`) in the text, compounding to superlinear cost.
#: Restructured into two BOUNDED regexes run in sequence rather than one
#: combined pattern: `_BYPASS_HEAD_RE` locates the leftmost git-invocation
#: head (anchor + prefix + `git\s+`, no `.*`, atomic prefix as above --
#: linear), then `_BYPASS_MARKER_RE` -- itself free of unbounded
#: quantifiers, just three literal alternatives each with bounded `\s+`/
#: `\s*` -- searches ONLY the text from that head's end onward for a
#: marker preceded by whitespace. Checking the LEFTMOST head only is
#: sufficient, not merely convenient: for any later head H2, the region
#: after H2 is a subset of the region after the leftmost head H1 (H2 occurs
#: later in the string), so "a marker exists after H1" is implied by "a
#: marker exists after H2" -- if the marker search from H1 fails, it would
#: fail from every later head too, and if it succeeds, the leftmost head
#: alone already witnesses the match `_BYPASS_RE.search` is asking for.
#: `_BypassRe.search` below reproduces the original combined pattern's
#: match-or-None contract so every existing call site (`bool(_BYPASS_RE.
#: search(...))`) is unchanged.
#:
#: Semantics preserved exactly (this is a deliberately OVER-inclusive
#: fail-closed fallback, reached only when the command could not be
#: tokenized -- over-matching is the safe direction here, under-matching is
#: not): a match still means "a git invocation head exists at a segment
#: boundary, and one of the three bypass markers appears at or after that
#: head, preceded by whitespace". The literal `in`-check pre-filter is a
#: speed short-circuit for the (overwhelmingly common) case where none of
#: the three markers appear anywhere in the text at all -- it is NOT relied
#: on for correctness when a marker IS present (an embedded `--no-verify`
#: still gets the full bounded head+marker scan, which stays linear
#: regardless of payload size).
_BYPASS_HEAD_RE = re.compile(r"(^|&&|\|\||;|\|)\s*" + _BYPASS_PREFIX + r"git\s+")
_BYPASS_MARKER_RE = re.compile(
    r"(?:^|\s)(?:-c\s+commit\.gpgsign\s*=\s*false|--no-verify|--no-gpg-sign)"
)
_BYPASS_MARKER_LITERALS = ("--no-verify", "--no-gpg-sign", "commit.gpgsign")


class _BypassRe:
    """Drop-in replacement for the former single compiled `_BYPASS_RE`
    pattern -- exposes the same `.search(text) -> Optional[Match]` surface
    every call site already uses via `bool(_BYPASS_RE.search(...))`, backed
    by the two-regex bounded scan documented above instead of one
    catastrophically-backtracking pattern.
    """

    @staticmethod
    def search(text: str) -> Optional[re.Match[str]]:
        if not any(marker in text for marker in _BYPASS_MARKER_LITERALS):
            return None
        head = _BYPASS_HEAD_RE.search(text)
        if head is None:
            return None
        # `head` ends with the trailing `\s+` of `git\s+`, so `head.end() -
        # 1` is guaranteed to be one of those whitespace characters -- back
        # the marker scan up by one so a marker sitting IMMEDIATELY after
        # `git ` (no additional separator text) still satisfies `_BYPASS_
        # MARKER_RE`'s own `(?:^|\s)` boundary. `re.search`'s `^` anchors to
        # absolute string position 0 only, never to a `pos=` start offset,
        # so starting exactly at `head.end()` would silently require a
        # SECOND whitespace character before the marker that the original
        # combined pattern never required.
        return _BYPASS_MARKER_RE.search(text, head.end() - 1)


_BYPASS_RE = _BypassRe()

#: `_BYPASS_RE` above scans RAW segment TEXT, so commit-message prose that
#: merely *describes* `--no-verify` tripped it. `_seg_has_git_bypass_flag`
#: shlex-splits `seg` ONCE and drops the -m/--message operand TOKEN from the
#: token LIST -- deliberately NOT via `_seg_excluding_freetext_operands`'s
#: string-rejoin-then-re-split round trip, which would drop the quoting around
#: any OTHER operand (`-c "commit.gpgsign = false"` would re-split into three
#: tokens and no longer match). Only an actual `--no-verify`/`--no-gpg-sign`
#: token, or `-c` followed by a `commit.gpgsign=false` operand, counts.
_BYPASS_WRAPPER_WORDS = frozenset({
    "sudo", "command", "time", "exec", "nice", "nohup",
    "ionice", "timeout", "stdbuf",
})
_BYPASS_FLAG_TOKENS = frozenset({"--no-verify", "--no-gpg-sign"})
_BYPASS_GPGSIGN_FALSE_RE = re.compile(r"^commit\.gpgsign\s*=\s*false$")

#: `which`/`type` REPORT on a command without executing it
#: (`_command_tokenizer.WrapperSemanticClass.INSPECTS_WITHOUT_EXECING`), so a
#: segment whose command-position head resolves to one of them is never a real
#: bypass: `_seg_has_git_bypass_flag` returns a confirmed `False` for it
#: rather than falling through to the raw-text scan. Scope is `check_no_verify`
#: only -- `_BYPASS_PREFIX`/`_BYPASS_RE` (also used by `check_blanket_git_add`)
#: are unchanged.
_INSPECTS_WITHOUT_EXECING_WORDS = frozenset({"which", "type"})

#: Wrapper binaries that take their OWN argument(s) before the wrapped
#: command -- `timeout 30 <cmd>`, `nice -n 10 <cmd>`, `ionice -c 2 <cmd>` /
#: `ionice -c2 <cmd>`, `stdbuf -oL <cmd>`. This token-list walk consumes both
#: the separate-token and attached flag-value forms; the regex fallback
#: (`_BYPASS_PREFIX`) only tolerates the attached form without risking
#: swallowing the real command token. `_skip_wrapper_own_argv` lives in
#: `_command_tokenizer.py`; a segment the walk cannot fully peel falls back
#: to the raw `_BYPASS_RE` scan, which over-blocks rather than under-blocks.


def _seg_has_git_bypass_flag(seg: str, pretokenized: Optional[List[str]] = None) -> bool:
    """Positionally scan `seg`'s ACTUAL git argv tokens (after dropping the
    free-text operand TOKEN of -m/--message, done on the token list itself
    -- see module comment above for why NOT via `_seg_excluding_freetext_
    operands`'s string round trip) for a genuine --no-verify /
    --no-gpg-sign / `-c commit.gpgsign=false` bypass flag.

    Fails CLOSED on anything it cannot positively tokenize: unparseable argv
    (shlex ValueError) or a segment with no confirmed command-position `git`
    after skipping wrapper/env/assignment prefix tokens both fall back to
    the pre-existing raw-text `_BYPASS_RE` scan on THIS segment -- ambiguity
    still over-blocks, it never under-blocks. Only a segment that
    POSITIVELY resolves to a git invocation with NO bypass token in its own
    argv is allowed to clear this check.

    `pretokenized`, when given, is this segment's own already-shlex-split
    token list (quote-aware segment boundaries already resolved by the
    caller -- see `check_no_verify`'s Finding-1 fix comment). Passing it
    skips the redundant re-`shlex.split(seg, ...)` below entirely, since a
    segment carved by the caller's quote-aware tokenizer can never itself
    raise `ValueError` here the way a naive re-split-then-parse segment
    could. `seg` (the segment's flat text, reconstructed by the caller for
    this purpose only) is still used for the two `_BYPASS_RE` raw-scan
    fallback branches below, which need literal text, not a token list.
    """
    if pretokenized is not None:
        raw_tokens = pretokenized
    elif _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- the over-inclusive raw `_BYPASS_RE` scan an unparseable
        # segment already falls back to (linear, so safe at any length).
        return bool(_BYPASS_RE.search(seg))
    else:
        try:
            raw_tokens = shlex.split(seg, posix=True)
        except ValueError:
            return bool(_BYPASS_RE.search(seg))

    tokens: List[str] = []
    skip_next = False
    for tok in raw_tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _FREETEXT_OPERAND_FLAGS:
            skip_next = True
            continue
        if any(tok.startswith(flag + "=") for flag in _FREETEXT_OPERAND_FLAGS):
            continue
        tokens.append(tok)

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # Peel `{` / `(` grouping tokens: left in place, the "first effective
        # token" is never `git` and the walk falls to `_BYPASS_RE`, whose
        # prefix has no `{` tolerance -- `{ git commit --no-verify -m x; }`
        # would be silently allowed. The tokenizer yields each as its own token.
        if tok in ("{", "("):
            i += 1
            continue
        base = _normalize_executable_basename(tok)
        if base in _BYPASS_WRAPPER_WORDS:
            i += 1
            i = _skip_wrapper_own_argv(tokens, i, base)
            continue
        if base == "env":
            i += 1
            while i < n and (
                tokens[i] in ("-i", "--ignore-environment")
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
            ):
                i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$", tok):
            i += 1
            continue
        break
    if i < n and _normalize_executable_basename(tokens[i]) in _INSPECTS_WITHOUT_EXECING_WORDS:
        # M5P Piece 1: a positively-resolved `which`/`type` head never
        # executes anything past it -- this is a CONFIRMED False, not an
        # ambiguous case, so it must not fall through to the raw-text scan
        # immediately below (that regex's shared `_BYPASS_PREFIX` still
        # tolerates `which`/`type`, unchanged, for `check_blanket_git_add`'s
        # sake -- see `_INSPECTS_WITHOUT_EXECING_WORDS`'s own docstring).
        return False
    if i >= n or _normalize_executable_basename(tokens[i]) != "git":
        # No confirmed command-position `git` in this segment's own argv --
        # fall back to the raw scan rather than guessing this is prose.
        return bool(_BYPASS_RE.search(seg))

    i += 1
    while i < n:
        tok = tokens[i]
        if tok in _BYPASS_FLAG_TOKENS:
            return True
        if tok == "-c" and i + 1 < n and _BYPASS_GPGSIGN_FALSE_RE.match(tokens[i + 1]):
            return True
        i += 1
    return False


def check_no_verify(
    cmd: str,
    session_id: str = "",
    *,
    resolved: Optional[List[Any]] = None,
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Deny a git invocation carrying a hook-bypass flag.

    ``resolved`` (M5, 2026-07-30) is the dispatcher's shared
    ``_command_tokenizer.resolve_command_positions(cmd)`` result -- one
    ``ResolvedCommand`` per top-level segment of ``cmd`` -- threaded in so
    this guard can consume that one shared segmentation walk instead of
    running its own `_bt_tokenize_full_command` + `_bt_segments_from_
    tokens_simple` pass (AC-8). Consumes each segment's ``raw_tokens``
    (the tokens BEFORE the resolver's own wide `EXECS_ITS_ARGV_WRAPPERS`
    peel) rather than its peeled ``tokens`` -- `_seg_has_git_bypass_flag`
    still applies THIS module's own narrower `_BYPASS_WRAPPER_WORDS`
    vocabulary as a post-filter, so this migration cannot widen (or
    narrow) what the guard matches; it only removes the redundant
    tokenize. `resolved is None` (every caller predating this parameter)
    takes the original self-contained path below -- which, as of C2 of
    `docs/plans/2026-08-26-the-destructive-core-learns-the-shell-it-guards
    .md`, itself now routes through `_dialect.resolve_segments_for_dialect`
    (dialect read from `hook_payload["tool_name"]`, defaulting to
    `Dialect.BASH`) instead of a direct `_bt_tokenize_full_command` +
    `_bt_segments_from_tokens_simple` pair -- byte-identical for the BASH
    leg (AC2); see that block's own inline comment for the fail-direction
    argument (AC4).
    """
    if _override("COORDINATOR_OVERRIDE_NO_VERIFY", payload=hook_payload):
        return None
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    flat = re.sub(r"\\n", " ", cmd.replace("\r", "").replace("\n", " "))

    _nv_note = operator_override_note(
        "COORDINATOR_OVERRIDE_NO_VERIFY", payload=hook_payload, git_root=git_root
    )

    def _nv_bypass_deny() -> Any:
        base = (
            "BLOCKED: git bypass flag detected. The coordinator doctrine "
            "prohibits --no-verify, --no-gpg-sign, and -c commit.gpgsign=false."
        )
        if _nv_note:
            base += " If the PM has explicitly authorized bypassing hooks: %s" % _nv_note
        return _alt_record(
            "deny", "no-verify",
            "BLOCKED: git bypass flag detected. The coordinator doctrine "
            "prohibits --no-verify, --no-gpg-sign, and -c commit.gpgsign=false.",
            (_offer("the same git command without the bypass flag", "the command"),),
            _NOVERIFY_ENV, report="the git command you need", authorize="bypassing hooks",
            em_text=base,
        )

    if re.search(r"\bgit\b", flat):
        if resolved:
            # Consume the shared resolver's segmentation via `raw_tokens`
            # instead of re-tokenizing `flat` from scratch (see docstring).
            # A segment whose `raw_tokens` is empty (the resolver's
            # unparseable/depth-cap fallback shape) falls through to this
            # function's own self-contained tokenize path below rather than
            # being silently skipped -- `used_resolved` stays False for that
            # segment set and the whole `resolved` list is abandoned in
            # favor of the always-safe fallback.
            used_resolved = True
            for rc in resolved:
                seg_tokens = list(getattr(rc, "raw_tokens", None) or [])
                if not seg_tokens:
                    used_resolved = False
                    break
                seg_text = " ".join(seg_tokens)
                if _seg_has_git_bypass_flag(seg_text, pretokenized=seg_tokens):
                    return _nv_bypass_deny()
            if used_resolved:
                return _no_verify_rescan_shell_c_and_heredoc(cmd, flat, session_id, hook_payload, git_root)

        # Segment via the shared quote-aware tokenizer, not a raw
        # `re.split(r"[;&|]+", cmd)`: a quoted `;`/`&`/`|` inside a commit
        # message (`git commit -m "release; ship it" --no-verify`) would tear
        # one git invocation into halves that each look innocent.
        #
        # Routed through `_dialect.resolve_segments_for_dialect`; `dialect`
        # comes from `hook_payload["tool_name"]`, and an absent payload or
        # unrecognized `tool_name` falls back to `Dialect.BASH`. `MATCHERS`
        # is `("Bash",)`, so live dispatch never reaches the PowerShell
        # branch below; it exists for fixtures.
        #
        # FAIL DIRECTION: for the BASH leg, `None` means
        # `tokenize_full_command` returned `None` (unterminated quote /
        # trailing backslash), and the over-inclusive raw-text `_BYPASS_RE`
        # fail-CLOSED scan below fires only in that case. For any OTHER
        # dialect, `None` means a DIFFERENT thing (a PowerShell parse failure, already recorded
        # SILENT inside `_powershell_tokens`) and must NOT fall through to
        # the bash raw-text deny scan -- that would misattribute a
        # PowerShell parse failure as a bash unparseable-command fail-close,
        # a different fail shape than today's guard ever produced. Silence
        # is preserved instead.
        dialect = dialect_from_tool_name((hook_payload or {}).get("tool_name")) or Dialect.BASH
        segments = resolve_segments_for_dialect(flat, dialect, guard_name="no-verify")
        if segments is None:
            if dialect is Dialect.BASH:
                # Unparseable (unterminated quote / trailing backslash) --
                # fail CLOSED via the original whole-string raw scan,
                # over-inclusive by construction (never under-blocks a
                # genuine bypass, may over-block ambiguous prose -- the safe
                # direction for an unparseable command).
                if _BYPASS_RE.search(flat):
                    return _nv_bypass_deny()
            # else: non-BASH dialect that failed to parse -- SILENT already
            # recorded by `resolve_segments_for_dialect`; do not deny.
        else:
            for seg_tokens, _pipe_before in segments:
                if not seg_tokens:
                    continue
                seg_text = " ".join(seg_tokens)
                if _seg_has_git_bypass_flag(seg_text, pretokenized=seg_tokens):
                    return _nv_bypass_deny()

    return _no_verify_rescan_shell_c_and_heredoc(cmd, flat, session_id, hook_payload, git_root)


def _no_verify_rescan_shell_c_and_heredoc(
    cmd: str,
    flat: str,
    session_id: str,
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-13/BX-14 rescan legs, shared by every `check_no_verify` return path
    above (the `resolved`-consuming early return included) so migrating the
    segment walk onto `raw_tokens` can never accidentally drop either rescan.
    The recursive `check_no_verify` calls here deliberately do NOT thread
    `resolved` back in: an unwrapped `sh -c` payload or heredoc body is not a
    segment of the outer `cmd` the caller's `resolved` was computed against,
    so re-running the full self-contained (`resolved=None`) path against
    each unwrapped payload is correct, not a missed optimization. They DO
    thread `hook_payload`/`git_root` back in (2026-08-13, audience-gated
    `operator_override_note` migration) -- the unwrapped text is a different
    COMMAND, not a different CALLER, so the hook envelope this rescan is
    evaluating on behalf of is unchanged.
    """
    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    for payload in _shell_c_unwrap_payloads(flat):
        result = check_no_verify(payload, session_id, hook_payload=hook_payload, git_root=git_root)
        if result is not None:
            return result

    # BX-14: a heredoc fed to a shell interpreter (`bash <<'EOF' ... EOF`) is
    # executed, not inert text -- unwrap and re-scan it too. Runs on `cmd`
    # (real newlines), never `flat` -- see `_heredoc_shell_payloads` docstring.
    for payload in _heredoc_shell_payloads(cmd):
        result = check_no_verify(payload, session_id, hook_payload=hook_payload, git_root=git_root)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# 2. check_destructive_git_orphan -- block-destructive-git-orphan.sh
# F0: PER-SEGMENT `git -C <dir>` resolution -- never a single dispatcher-level
# git root. See module docstring / recipe Sec(e).
# ---------------------------------------------------------------------------

def _orphan_override_hint(payload: Optional[Dict[str, Any]], git_root: Optional[str]) -> str:
    """Per-call replacement for the former module-level ``_ORPHAN_OVERRIDE_HINT``
    constant (2026-08-13, audience-gated ``operator_override_note`` migration;
    tasks/guard-messages-keys/DECISIONS.md D1/D2). A module-level constant
    built from ``operator_override_note(...)`` was computed once at IMPORT
    time, before any request-scoped ``payload``/``git_root`` existed to
    resolve an audience against -- the same no-payload-at-composition-time
    defect this whole workstream exists to remove, one level up from the
    per-firing string it wraps. Callers pass their own in-scope ``payload``/
    ``git_root``; each of the four call sites below applies the SPLICE
    CONTRACT documented on ``operator_override_note`` itself (gate the
    trailing sentence on ``bool(...)`` rather than always appending it) --
    an empty return here means the caller must render NO trailing pointer
    sentence at all, not a bare "If this is genuinely intended: ".
    """
    note = operator_override_note("COORDINATOR_ALLOW_ORPHAN", payload=payload, git_root=git_root)
    if not note:
        return ""
    return "If this is genuinely intended: %s" % note


_ORPHAN_ENV = "COORDINATOR_ALLOW_ORPHAN"
_ORPHAN_KW = dict(report="the git operation you need", authorize="the history-orphaning operation")
_CLEAN_ENV = "COORDINATOR_OVERRIDE_GIT_CLEAN"
_CLEAN_KW = dict(report="the git clean you need", authorize="deleting load-bearing untracked files")
_REVERT_ENV = "COORDINATOR_OVERRIDE_GIT_REVERT"
_REVERT_KW = dict(report="the checkout, restore, reset or stash you need", authorize="discarding uncommitted work")
_NOVERIFY_ENV = "COORDINATOR_OVERRIDE_NO_VERIFY"


def _seg_excluding_freetext_operands(seg: str) -> str:
    """Tokenize `seg` with shlex and drop the operand tokens of free-text
    flags (-m / --message) before CHECK 2 pattern-matches it, so a commit
    message that merely *discusses* force-pushing (prose) can never trip the
    same regex that detects an ACTUAL forcing `git push`/`-f`/`+refspec`
    token. Only the flag's OWN token is ever stripped -- everything else
    (subcommand, remote, branch, other flags) passes through unchanged, so
    this can only narrow what CHECK 2 sees, never widen it.

    Fails closed, never open: if `shlex.split` raises (malformed/unterminated
    quoting -- can't be parsed as a real argv), returns `seg` UNCHANGED, so
    the caller's existing raw-segment regex scan runs exactly as it did
    before this fix (still over-blocking, never under-blocking).
    """
    if _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- returns `seg` UNCHANGED, the same over-blocking branch an
        # unterminated quote already reaches.
        return seg
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return seg
    kept: List[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _FREETEXT_OPERAND_FLAGS:
            skip_next = True
            continue
        if any(tok.startswith(flag + "=") for flag in _FREETEXT_OPERAND_FLAGS):
            continue
        kept.append(tok)
    return " ".join(kept)


def _git_argv_subcommand_position(tokens: List[str]) -> Optional[int]:
    """Index of git's SUBCOMMAND in `tokens`, or `None` when that cannot be
    established with confidence.

    Extracted so the subcommand's NAME and its POSITION come from one walk.
    They were briefly derived separately: `_after_reset_slice` found its
    subcommand by taking the first token equal to `"reset"`, which is the
    same answer for almost every real command and the WRONG one whenever a
    preceding global option takes `reset` as its VALUE. `git -C reset reset
    --hard HEAD~3` matched the `-C` operand, so the target slice kept
    `reset --hard HEAD~3`, CHECK 1 resolved no target, and a genuine
    ref-move was allowed -- a deny the greedy regex it replaced had caught,
    because `.*` backs off to the LAST match. Caught in review before it
    shipped; a position derived from a second walk is what made it possible.

    Fails CLOSED exactly as its caller contract requires: no command-position
    `git`, or an unrecognized flag whose consumption shape is unknown,
    returns `None` and never guesses.
    """
    if not tokens or _normalize_executable_basename(tokens[0]) != "git":
        return None
    index = 1
    count = len(tokens)
    while index < count:
        token = tokens[index]
        if token in _GIT_GLOBAL_OPT_WITH_ARG:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        if token.startswith("-"):
            if token in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                index += 1
                continue
            return None
        return index
    return None


def _seg_resolved_git_subcommand(seg: str) -> Optional[str]:
    """Positionally resolve the git SUBCOMMAND `seg` invokes -- the first
    non-flag token after a command-position `git`, walking past git's own
    global options (`_GIT_GLOBAL_OPT_WITH_ARG` / `_GIT_GLOBAL_OPT_NO_ARG_
    SIMPLE`, defined below and shared with `_command_really_invokes`) --
    or `None` if that cannot be determined with confidence.

    CHECK 2 originally excluded `git stash push` via a substring regex
    (`\\bstash\\s+push\\b`), which a coordinator review caught as a bypass
    of a bypass-prevention guard: `git push origin main --force -- stash
    push` and `git push origin main --force # git stash push` both contain
    that substring while genuinely invoking `push`, so the exclusion
    suppressed CHECK 2 on a real forcing push. This positional walk fixes
    that by construction -- it returns the FIRST non-flag token after
    `git`, so a trailing pathspec or comment mentioning `stash push` can
    never influence which subcommand actually got resolved.

    Same design as `block_subagent_destructive_action._real_git_
    subcommand` (its own module docstring's "COMMAND-POSITION GIT-TOKEN
    FIX", ~line 1132 there) -- not reused directly (different module,
    different token-source contract: that function walks tokens already
    past a confirmed `git` token, this one also confirms command-position
    `git` itself via `_normalize_executable_basename`) but the same
    fail-CLOSED discipline: an unresolvable case (unparseable `seg`, no
    command-position `git`, or an unrecognized global flag whose argument
    shape is unknown) returns `None`, NEVER guesses. `None` means "cannot
    positively confirm a subcommand" -- callers must treat that as
    "keep the pre-existing, more conservative behavior", never as
    confirmation of any particular subcommand (including `push`).

    A `<<` heredoc marker anywhere in `seg` also forces `None`, even when
    `shlex.split` parses cleanly: a heredoc body is NOT part of the
    invoking command's own argv in real shell semantics (the shell reads
    it as raw input until the terminator line, never as further tokens on
    the command line), so treating it as ordinary follow-on argv would be
    confidently wrong, not merely uncertain. Concretely, `TestCheck2Heredoc
    BodyProseExclusion.test_unterminated_heredoc_with_force_push_in_body_
    fails_closed` feeds an UNTERMINATED `git commit -F - <<'MSG'` whose
    body is `git push origin main --force` -- `shlex.split` happily parses
    the whole thing as one argv (the quoted `'MSG'` closes immediately, so
    nothing raises) and resolves the OUTER command's subcommand as
    `commit`, which is positionally accurate but would silence CHECK 2 on
    a segment the existing fail-closed contract requires to keep denying
    (the caller upstream already decided this malformed heredoc could not
    be safely stripped, and left the raw text, `--force` included, for the
    regex fallback to catch).
    """
    if "<<" in seg:
        return None
    if _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `None` routes the caller to its raw `\bpush\b` word match,
        # the same over-inclusive fallback an unparseable segment takes.
        return None
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return None
    position = _git_argv_subcommand_position(tokens)
    return None if position is None else tokens[position]


def _seg_forcing_form_scan_text(seg: str) -> str:
    """Narrow `seg` to the argv of the `git` it invokes, for CHECK 2's
    forcing-form regex only -- or return `seg` whole when that cannot be
    established.

    CHECK 2 resolves whether a segment is a push candidate positionally,
    but then scanned the WHOLE segment for `--force`/`-f`/`+refspec`. A
    wrapper's own flags live in that text and are not git's: `/usr/bin/time
    -f "%e" git push` was denied as a "forcing form" because `time` takes
    `-f FORMAT`, which is how the obvious way to MEASURE a push became the
    one command that cannot run. Same shape for `env -f`, `nice`, `xargs
    -0f` and any other spawning head -- these are exactly the segments
    `_seg_confirmed_not_git_invocation` must leave ambiguous (they really
    can launch git), so neither existing seam covers them.

    Scoping to the tokens at-and-after the first command-position-agnostic
    `git` token cannot open a bypass: `--force` and `+refspec` are push
    ARGUMENTS, so a genuine forcing push carries them after that token and
    stays inside the scanned text. `git push origin main --force` behind
    any wrapper still denies. What drops out is only text that git never
    sees.

    Fail-CLOSED on every uncertainty, mirroring `_seg_resolved_git_
    subcommand`'s contract: a heredoc marker, an untokenizable segment, an
    over-ceiling segment, or no `git` token at all all return `seg`
    unchanged, leaving the pre-existing over-inclusive scan in place. The
    return narrows the scan ONLY when a real `git` token was positively
    located.
    """
    if "<<" in seg:
        return seg
    if _bt_exceeds_tokenizable_ceiling(seg):
        return seg
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return seg
    for idx, tok in enumerate(tokens):
        if _normalize_executable_basename(tok) == "git":
            # Space-join rather than raw slicing: the regex matches
            # whitespace-delimited flag tokens, and shlex has already
            # stripped the quoting that the raw text would still carry.
            return " ".join(tokens[idx:])
    return seg


#: ALLOWLIST (deliberately, not a denylist) of ordinary POSIX-ish utility
#: heads that are positionally CONFIRMED to never themselves launch `git` --
#: none of these execs argv it's handed, spawns a subprocess, or interprets
#: further shell/script text, so once one of these sits at command position,
#: nothing later in the segment (a path, a pattern, a message) can still
#: turn into a `git push --force`. Covers the P2/P3 shapes exactly: `test -f
#: .../push-failures.log`, `rm -f push.log`, `grep -f patterns-push.txt`.
#: An unrecognized head (anything NOT in this set, including a wrapper,
#: shell, or scripting interpreter, or plain unfamiliarity) stays AMBIGUOUS
#: -- see `_seg_confirmed_not_git_invocation`'s own docstring for why this
#: must be an allowlist, not "everything except a known-dangerous set": a
#: scripted spawn like `subprocess.run(['git', 'push', ..., '--force'])`
#: (kept visible by `_strip_heredoc_bodies_for_prose_scan`'s spawn-indicator
#: check) has a head token that is neither `git` nor any recognizable
#: wrapper/interpreter word either -- a denylist would wrongly call that
#: "confirmed safe" and skip the fallback scan that is this shape's ONLY
#: remaining detection path.
_CHECK2_SAFE_NONSPAWNING_HEADS = frozenset({
    "test", "[", "[[", "true", "false", "echo", "printf",
    "cat", "ls", "stat", "wc", "head", "tail", "grep", "rm", "cp", "mv",
    "touch", "mkdir", "rmdir", "basename", "dirname", "pwd", "du", "df",
    "chmod", "chown", "ln", "readlink", "realpath",
})


def _seg_confirmed_not_git_invocation(seg: str) -> bool:
    """True only when `seg` positionally resolves to a command whose HEAD is
    a member of `_CHECK2_SAFE_NONSPAWNING_HEADS` -- a plain utility that is
    definitely not `git` and definitely cannot itself spawn `git` further
    down (unlike a shell, `env`, `xargs`, or a scripting interpreter, all of
    which are deliberately left OUT of that allowlist and so stay ambiguous).

    P2/P3 fix (2026-07-30): CHECK 2's forcing-push scan previously treated
    EVERY segment where `_seg_resolved_git_subcommand` returned `None` the
    same way -- falling back to a raw `\\bpush\\b` word scan over the whole
    segment text, then a bare `-f` scan. That fallback exists for a
    genuinely ambiguous case (a heredoc body, or a scripted spawn like
    `subprocess.run(['git', 'push', ..., '--force'])`, where positional
    resolution legitimately cannot be trusted and CHECK 2 must still see the
    raw text). But `_seg_resolved_git_subcommand` also returns `None` for a
    SECOND, entirely different reason: a segment that parses cleanly and
    positively resolves to a command that is simply not `git` at all --
    `test -f /Users/x/.git/push-failures.log` parses with `tokens[0] ==
    "test"`, a CONFIRMED non-git, non-spawning command, not an unresolvable
    one. Collapsing "confirmed safe" into the same bucket as "cannot tell"
    let a read-only `test -f` against a path that merely CONTAINS the
    substrings "git" and "push" (plus a `-f` flag with unrelated semantics)
    get denied as though it were a forcing push. Same root cause misfires on
    `rm -f push.log` / `grep -f patterns-push.txt`.

    This function narrows the fallback to genuine ambiguity only: a segment
    that (a) fails to shlex-parse, (b) contains a heredoc marker, or (c)
    positionally resolves to any head NOT in the allowlist above (a shell,
    `env`, `xargs`, a scripting interpreter, an unrecognized wrapper, or
    truly anything else this function doesn't positively vouch for) remains
    eligible for the raw `\\bpush\\b` fallback scan. Only a segment whose
    head is a KNOWN, enumerated, non-spawning utility is definitively not a
    push candidate -- no further text scan runs, so a `push`/`-f` substring
    anywhere in ITS operands can no longer matter. Fails closed as always:
    any parse failure, empty tokens, or unrecognized head returns `False`
    (stays eligible for the existing raw scan), never `True`.
    """
    if "<<" in seg:
        return False
    if _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `False` keeps the segment eligible for the raw scan, the
        # same fail-closed answer a parse failure already gives.
        return False
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    head = _normalize_executable_basename(tokens[0])
    return head in _CHECK2_SAFE_NONSPAWNING_HEADS


_RESET_MODE_FLAGS = ("--hard", "--soft", "--mixed", "--keep", "--merge")


def _git_reset_invocation(seg: str) -> bool:
    r"""True when `reset` is git's SUBCOMMAND in this segment, or when the
    segment is too ambiguous to rule that out.

    CHECK 1's gate was a bare `\breset\b` word match, which denied a 14.6KB
    sizing command whose PROSE contained the word -- the operator is told
    their read-only command is a destructive ref-move. The repair is
    positional resolution, and it must be built on this module's existing
    quote-aware walk rather than a fresh one: an earlier hand-rolled
    `seg.split()` version of this function shipped and was caught in review
    allowing `git 'reset' --hard HEAD~3`, `git re""set --hard`, `git r\eset
    --hard` and `git -c user.name="John Doe" reset --hard` -- every one a real
    shell construct that executes a genuine ref-move. A false negative here
    orphans commits on a tree ~50 peer sessions share, so this predicate is
    over-inclusive BY CONSTRUCTION and only ever narrows on a positive
    resolution.

    Three answers, in strict order:

    - `_seg_resolved_git_subcommand` resolved a subcommand: trust it. That
      walk is `shlex`-based, so quoting, concatenation and backslash forms
      all normalize to the same token, and it consumes git's own global
      options (`git -C <dir> reset`, `git -c k=v reset`).
    - `_seg_confirmed_not_git_invocation`: the segment positively resolves to
      an enumerated non-git, non-spawning utility, so no `reset` inside its
      operands can be a subcommand. Same narrowing CHECK 2 already makes.
    - Otherwise the segment is genuinely ambiguous -- unparseable, or a
      heredoc whose body is not this command's argv -- and the original word
      match stands unchanged.

    NEGATIVE SPEC -- this does NOT fix the reported sizing-command denial.
    That command is a `python - <<'PY'` heredoc whose prose contains the token
    `subprocess`, so `_heredoc_body_has_spawn_indicator` keeps the body
    visible (correctly: a Python body that can spawn really can run git), the
    segment stays ambiguous, and the third branch denies on the prose
    `reset`. That is NOT a stripper defect -- measured 2026-09-12,
    `_strip_heredoc_bodies` reduces the same input 14661 -> 15 bytes.
    Narrowing it would mean deciding a spawn-capable body does not spawn THIS
    command, which needs a language parser rather than a token scan, and the
    cost of guessing wrong is an orphaned commit on a shared tree. The
    fixture at `tests/data/check1_reset_prose_false_positive.cmd.txt` is kept
    as the reproduction, asserted as a known accepted false positive.
    """
    resolved = _seg_resolved_git_subcommand(seg)
    if resolved is not None:
        return resolved == "reset"
    if _seg_confirmed_not_git_invocation(seg):
        return False
    return bool(re.search(r"\breset\b", seg))


def _after_reset_slice(seg: str) -> str:
    r"""The argv slice FOLLOWING git's `reset` subcommand -- the text CHECK 1
    reads the reset TARGET out of.

    Split quote-aware, not on a bare `reset` word. The regex fallback below
    (`.*(^|\s)reset(\s|$)`) requires whitespace on both sides of the token,
    so `git 'reset' --hard HEAD~3` matches nothing, `after` keeps the whole
    segment, no target is extracted, and a real ref-move is allowed. Measured
    2026-09-12 against a throwaway 4-commit repo: `git 'reset' --hard HEAD~3`,
    `git "reset" --hard HEAD~3` and `git re""set --hard HEAD~3` were all
    ALLOWED while the unquoted form denied -- identically before and after
    that day's `_git_reset_invocation` rewrite, so this is the extractor's own
    blind spot and not a regression in the invocation predicate.

    Falls back to the original regex whenever `shlex` cannot resolve the
    segment (unparseable, a heredoc, no command-position `git`). That keeps
    the pre-existing over-inclusive behaviour on exactly the inputs it already
    covered, and never trades a deny for an allow.
    """
    if "<<" not in seg and not _bt_exceeds_tokenizable_ceiling(seg):
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            tokens = None
        position = _git_argv_subcommand_position(tokens or [])
        if position is not None and tokens[position] == "reset":
            return " " + " ".join(tokens[position + 1:])
    return re.sub(r".*(^|\s)reset(\s|$)", " ", seg, count=1)


def _reset_ref_moving_mode(seg: str):
    """Return the reset MODE string for a segment that is a ref-moving
    `git reset`, or None if the segment is not one.

    Ref-moving is the property CHECK 1 actually cares about (see its own
    comment): every mode here moves the branch ref and therefore orphans the
    same commits. The mode is returned rather than a bool purely so the deny
    messages can name the command the operator actually typed -- reporting a
    `--soft` reset as `git reset --hard` would send them to verify the wrong
    thing.

    Returns the DEFAULT mode `--mixed` for a bare `git reset <target>`, which
    is what git itself does. That is not a widening: a bare reset resolves
    target HEAD and CHECK 1's own `rev-list --count HEAD..HEAD` is zero, so it
    denies nothing on its own -- but `git reset HEAD~1`, which IS a silent
    ref-move, is caught.

    Deliberately word-boundary matched on `reset` exactly as the pre-existing
    gate was, so no segment that previously reached CHECK 1 stops reaching it.
    """
    if not _git_reset_invocation(seg):
        return None
    for flag in _RESET_MODE_FLAGS:
        if re.search(r"(^|\s)" + re.escape(flag) + r"(\s|$)", seg):
            return flag
    return "--mixed"


def check_destructive_git_orphan(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """F0-b (2026-08-05, spawn-count defect): CHECK 1's `rev-parse --verify
    <target>^{commit}` and `rev-list --count <target>..HEAD` probes ran once
    PER SEGMENT even though `target`/`git_cwd` are almost always identical
    across a chained command (`git reset --hard X && git reset --hard X`),
    answering a question this call already answered -- linear-in-segment-
    count subprocess spawns (measured ~2 spawns per segment at HEAD, ~1,778
    spawns / 9.9s on a 16 KiB chained-reset command). Fixed via `_new_git_
    memo`'s per-call, exact-(cwd, args)-keyed memo -- see that factory's
    docstring for why this is sound and why it is deliberately NOT
    module-level. Only wraps calls that are provably safe to reuse
    (repo/ref queries whose result depends solely on their own args + cwd,
    not on which segment reached them) -- see call sites below for which
    legs were left unmemoized and why.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    if _override("COORDINATOR_ALLOW_ORPHAN", payload=payload):
        return None

    # BX-13 (2026-08-17, confirmed live bypass -- guard-bypass-triage
    # Finding 1): captured BEFORE the heredoc/quoted-span stripping below,
    # which deletes a whitespace-containing `sh -c '...'`/`bash -c "..."`
    # payload WHOLESALE as "inert prose" -- the exact mechanism that
    # correctly allows `echo "reviewing git commit conventions"` -- so
    # `sh -c 'git reset --hard $(echo HEAD~3)'` never reached the `\bgit\b`
    # gate this function used to have here at all. Rescanned via
    # `_shell_c_unwrap_payloads` (this module's own copy of
    # `block_subagent_commit.py`'s `_wrapped_shell_c_payloads` -- see that
    # constant's docstring above) AFTER the segment loop below, near the
    # bottom of this function, same shape `check_no_verify`/
    # `check_destructive_rm` already use for the identical bug class.
    raw_cmd = cmd
    # Prose-heredoc-body stripping runs BEFORE `_strip_ws_quoted_spans` --
    # that scanner's whitespace-containing-span rule can otherwise mangle a
    # multi-line heredoc body containing an unpaired quote char, corrupting
    # the terminator line this function still needs to find. See
    # `_strip_heredoc_bodies_for_prose_scan`'s own docstring for the
    # shell-fed-vs-prose discriminator (CHECK 2/3 fix, cross-repo memo
    # 2026-07-2x heredoc-body-prose-false-positive).
    cmd = _strip_heredoc_bodies_for_prose_scan(cmd)
    # Comments run AFTER heredoc-body stripping (so a `#` inside a body the
    # pass above discarded is never consulted) and BEFORE
    # `_strip_ws_quoted_spans` (which would otherwise delete the quoted
    # spans this scanner needs to track quote state across). See
    # `_strip_shell_comments_for_prose_scan` for why this cannot widen.
    cmd = _strip_shell_comments_for_prose_scan(cmd)
    cmd = _strip_ws_quoted_spans(cmd)
    # Restore quote chars sentinel-protected by `_strip_heredoc_bodies_for_
    # prose_scan` for KEPT heredoc-body lines, now that the whitespace-quoted-
    # span scanner above has run (and so can no longer delete a multi-word
    # source-string literal it would otherwise mistake for shell free text).
    cmd = _restore_protected_quotes(cmd)

    # No top-level `\bgit\b` fast-path return here (deliberately removed,
    # BX-13 fix): the per-segment gate at the top of the loop below already
    # skips every git-free segment, and a hard early return here is exactly
    # what let the `sh -c` bypass above short-circuit past the rescan at the
    # bottom of this function before it could ever run.

    # Per-call memo, keyed on the exact (cwd, args) pair actually run --
    # see `_new_git_memo` docstring for why this is sound and why it is
    # deliberately NOT module-level.
    _memo_run_git = _new_git_memo()
    _orphan_hint = _orphan_override_hint(payload, git_root)

    # C3 (pln-the-destructive-core-learns-the-she): `git`'s own argv is
    # byte-identical across dialects, so the base match needs no new
    # vocabulary -- only the anti-bypass surface, recovered via the shared
    # `_ps_git_bypass_segments` seam and appended to the SAME bash-shaped
    # segment list this loop already walks. `None` (PowerShell parse
    # failure) yields no synthetic segments -- silence is preserved (AC4),
    # never a deny on text this seam could not parse.
    #
    # FED THE PROSE-STRIPPED `cmd`, NEVER `raw_cmd`. This chunk first passed
    # `raw_cmd` (captured above, before the heredoc/quoted-span stripping)
    # on the reasoning that the PowerShell tokenizer wants unstripped text.
    # Measured, that reintroduces the exact CHECK 2/3 prose bug the stripping
    # exists to prevent: a heredoc COMMIT MESSAGE documenting a hazard
    # (`git reset --hard $(...)` quoted inside the body as prose) was
    # re-tokenized out of the heredoc body and denied as a real subshell-
    # resolved target -- caught by `test_check1_hazard_prose_not_subshell_
    # target.py::heredoc_commit_message`. Bash heredoc bodies are not
    # PowerShell syntax, so stripping them costs the PowerShell leg nothing;
    # keeping them costs a false deny on ordinary commit prose, which is the
    # worse direction on a CONFINEMENT_DENY entry. The two sibling call
    # sites (`destructive-git-clean`, `blanket-git-add`) already pass `cmd`.
    _orphan_ps_segments = _ps_git_bypass_segments(
        cmd, "destructive-git-orphan", payload
    ) or []

    _orphan_bash_segments = list(_split_segments(cmd))
    _orphan_payload_cwd = (payload or {}).get("cwd") or None
    # Precomputed ONCE, not re-walked per git segment: `_bt_cd_chain_cwds_
    # by_segment` is a single forward pass, so a many-segment command stays
    # linear here. Calling `_bt_cd_chain_cwd_before` fresh per segment would
    # turn this into an O(n^2) rescan -- measured blowing the dispatch
    # latency budget on the `many_segments_git` corpus shape at the
    # tokenizer ceiling.
    _orphan_cd_chain_cwds = _bt_cd_chain_cwds_by_segment(
        _orphan_bash_segments, _orphan_payload_cwd
    )
    for _seg_idx, seg in enumerate(_orphan_bash_segments + _orphan_ps_segments):
        if not seg.strip():
            continue
        # `_word_present`, not a raw scan: the quote-split verb class reaches
        # this gate too -- see that helper's docstring for the widening contract.
        if not _word_present(r"\bgit\b", seg):
            continue

        c_dir = _extract_git_c_dir(seg)
        # `-C` on THIS segment still wins where present. Failing that, a
        # synthetic PowerShell-bypass segment (appended past
        # `_orphan_bash_segments`, so it has no position in that list) falls
        # back to the single-leading-`cd` resolution the sibling guard
        # already uses; a real bash segment gets the full `cd`-chain walk --
        # see `_bt_cd_chain_cwds_by_segment`'s docstring for why this guard
        # needs more than a leading `cd`.
        if _seg_idx < len(_orphan_bash_segments):
            git_cwd = (
                _orphan_c_cwd(c_dir, _orphan_payload_cwd or git_root)
                or _orphan_cd_chain_cwds[_seg_idx]
            )
        else:
            git_cwd = _orphan_c_cwd(
                c_dir, _orphan_payload_cwd or git_root
            ) or _bt_leading_cd_prefix_cwd(cmd, _orphan_payload_cwd)

        # CHECK 1 -- git reset <target>, any ref-moving mode
        #
        # Orphaning is a property of the BRANCH REF MOVING BACKWARDS, not of
        # what the reset does to the index or worktree: `--soft`, `--mixed`
        # and `--hard` all drop the same commits out of branch history;
        # `--soft` only makes the loss quieter.
        #
        # Both arms below are mode-independent: a subshell-resolved target is
        # equally unverifiable in any mode, and the `rev-list --count
        # <target>..HEAD` probe measures ref movement. Non-ref-moving
        # forms stay out via the PRE-EXISTING guards, not the mode gate -- the
        # `--` pathspec form is excluded below, `git reset <path>` resolves no
        # commit so its `rev-parse --verify <path>^{commit}` fails, and a bare
        # `git reset` resolves target HEAD and counts zero.
        _reset_mode = _reset_ref_moving_mode(seg)
        if _reset_mode:
            after = _after_reset_slice(seg)
            # A lone `$(`/backtick in `after` is not proof of a subshell-
            # resolved target: prose that NAMES the hazard (a markdown code
            # span, a comment, a string literal assembled elsewhere) can
            # leave exactly one delimiter in this slice -- the other half of
            # its pair sits before `reset`, outside `after` entirely. A real
            # target opens AND closes its subshell inside `after`, so require
            # the matched pair rather than bare presence (DR-144).
            #
            # The pair is matched across NEWLINES, not just within one line:
            # `git reset --hard $(\n  git rev-parse origin/main\n)` is ordinary
            # shell, and a single-line-only rule dropped it from CHECK 1
            # entirely (it survived only via the dirty-tree arm, which does not
            # fire on a clean tree). Prose cannot exploit the widening because
            # the two strippers upstream of here remove the shapes that carry
            # it -- `_strip_shell_comments_for_prose_scan` for comments, and
            # `_heredoc_body_has_spawn_indicator`'s interpreter-gated backtick
            # arm for Python/Node bodies -- so what reaches `after` with
            # delimiters still in it is shell, where they mean what they say.
            if re.search(r"\$\(.*\)|`.*`", after, re.DOTALL):
                return _alt_record(
                    "deny", "destructive-git-orphan",
                    "BLOCKED: 'git reset %s' targets a subshell-resolved "
                    "ref -- cannot verify it. Resolve to a literal first." % _reset_mode,
                    (_offer("git rev-list --count <resolved-ref>..HEAD", "the resolved ref"),),
                    _ORPHAN_ENV, **_ORPHAN_KW,
                    em_text=(
                        (
                            "BLOCKED: 'git reset %s' targets a subshell-resolved "
                            "ref -- cannot verify it.\n\n"
                            "Resolve to a literal first; use instead:\n"
                            "  git rev-list --count <resolved-ref>..HEAD"
                        ) % _reset_mode
                        + ("\n\n" + _orphan_hint if _orphan_hint else "")
                    ),
                )
            if not re.search(r"(^|\s)--(\s|$)", after):
                bare = [
                    _strip_q(tok) for tok in after.split() if not tok.startswith("-")
                ]
                if len(bare) <= 1:
                    target = bare[0] if bare else "HEAD"
                    # F0-b: memoized -- (target, git_cwd) is usually identical
                    # across every segment of a chained `git reset --hard`, and
                    # repo state cannot change mid-dispatch, so re-resolving
                    # the same ref/count on segment 2..N is pure repeated
                    # work. See `_new_git_memo` docstring.
                    rc, _ = _memo_run_git(["rev-parse", "--verify", "%s^{commit}" % target], git_cwd)
                    if rc == 0:
                        rc2, out = _memo_run_git(["rev-list", "--count", "%s..HEAD" % target], git_cwd)
                        n_str = out.strip() if rc2 == 0 else "0"
                        try:
                            n = int(n_str)
                        except ValueError:
                            n = 0
                        if n > 0:
                            # Routed through the same per-call memo as the
                            # rev-parse/rev-list probes above (2026-08-07,
                            # spawn-storm sweep D6) -- this call is a
                            # single-shot in practice (the function returns
                            # on this branch immediately below), so the
                            # memo buys no spawn-count reduction here, but
                            # nothing about repo state can change mid-
                            # dispatch either, so there is no freshness
                            # reason to keep it as a bare `_run_git` call
                            # sitting beside two memoized siblings.
                            _, branch_out = _memo_run_git(["rev-parse", "--abbrev-ref", "HEAD"], git_cwd)
                            cur_branch = branch_out.strip() or "HEAD"
                            _, subj_out = _dc_git_probe._run_git(
                                ["log", "--format=  - %h %s", "%s..HEAD" % target], cwd=git_cwd
                            )
                            subjects = "\n".join(subj_out.splitlines()[:5])
                            more = "\n  ... and %d more" % (n - 5) if n > 5 else ""
                            _reset_trailer = (
                                "\n\nIf those %d commits are genuinely disposable (or "
                                "provably safe on another ref), %s" % (n, _orphan_hint)
                                if _orphan_hint
                                else ""
                            )
                            return _alt_record(
                                "deny", "destructive-git-orphan",
                                "BLOCKED: 'git reset %s %s' would drop %d "
                                "commit(s) from branch '%s'. These commits are "
                                "reachable from HEAD but NOT from %s, so the reset "
                                "orphans them:\n%s%s\n"
                                "This is the 2026-05-28 near-miss shape: a reset to "
                                "a ref that is BEHIND your current work. Re-derive "
                                "the TRUE state yourself (do not trust a remembered count)."
                                % (_reset_mode, target, n, cur_branch, target, subjects, more),
                                (
                                    _ready("git rev-list --count %s..HEAD" % target),
                                    _ready("git branch -a --contains HEAD"),
                                ),
                                _ORPHAN_ENV, **_ORPHAN_KW,
                                em_text=(
                                    "BLOCKED: 'git reset %s %s' would drop %d "
                                    "commit(s) from branch '%s'.\n\n"
                                    "These commits are reachable from HEAD but NOT from "
                                    "%s, so the reset orphans them:\n%s%s\n\n"
                                    "This is the 2026-05-28 near-miss shape: a reset to "
                                    "a ref that is BEHIND your current work. Before overriding, "
                                    "re-derive the TRUE state yourself (do not trust a "
                                    "remembered count):\n"
                                    "  git rev-list --count %s..HEAD   # commits you "
                                    "would lose; must be 0 to be safe\n"
                                    "  git branch -a --contains HEAD          # other refs "
                                    "that already hold this work"
                                    % (_reset_mode, target, n, cur_branch, target, subjects, more, target)
                                )
                                + _reset_trailer,
                            )

        # CHECK 2 -- force push
        # Evaluated on the tokenized segment with -m/--message operands
        # removed -- see `_seg_excluding_freetext_operands` docstring. This
        # must match on what the command DOES, never on what a commit
        # message SAYS.
        seg_check2 = _seg_excluding_freetext_operands(seg)
        # MENTION-vs-INVOCATION gate. `\bpush\b` also matches `push` as
        # `git stash push`'s own subcommand token -- a local, non-remote
        # operation with no forcing semantics at all -- so the free-text
        # word match alone denies `git stash push -f -- <paths>` and
        # `git stash push -m "x" -- +path` as if they were a forced remote
        # push.
        #
        # A prior fix here used a `\bstash\s+push\b` substring exclusion --
        # a coordinator review found that's a bypass of a bypass-prevention
        # guard: `git push origin main --force -- stash push` and `git push
        # origin main --force # git stash push` both genuinely invoke
        # `push` yet contain that substring, so the exclusion silently
        # suppressed CHECK 2 on a real forcing push. Fixed by resolving the
        # segment's subcommand POSITIONALLY instead (`_seg_resolved_git_
        # subcommand`, first non-flag token after `git`) -- a substring can
        # be planted anywhere in a segment; a subcommand's ARGV POSITION
        # cannot.
        #
        # `resolved is None` (unparseable segment, no command-position
        # `git`, or an unrecognized global flag) deliberately falls back to
        # the plain `\bpush\b` word match rather than skipping CHECK 2 --
        # this is what keeps `TestCheck2ScriptableHeredocSpawnDetection`
        # failing CLOSED: a heredoc body's mangled shlex tokens (`subprocess
        # .run(['git', 'push', ...])`) never resolve a clean `git` command
        # position, so `resolved` is `None` there and CHECK 2 still examines
        # the segment via the original word-match path.
        _resolved_subcommand = _seg_resolved_git_subcommand(seg_check2)
        if _resolved_subcommand is not None:
            _is_push_candidate = _resolved_subcommand == "push"
        elif _seg_confirmed_not_git_invocation(seg_check2):
            # Positionally confirmed non-git, non-spawning head (`test -f
            # .../push-failures.log`, `rm -f push.log`, `grep -f
            # patterns-push.txt`) -- P2/P3 fix, see
            # `_seg_confirmed_not_git_invocation` docstring. Never a push
            # candidate; the raw `\bpush\b`/`-f` fallback below must not run
            # against this segment's free-text operands.
            _is_push_candidate = False
        else:
            _is_push_candidate = bool(re.search(r"\bpush\b", seg_check2))
        if _is_push_candidate:
            if re.search(
                r"(--force([^-=]|$)|(^|\s)-[a-zA-Z]*f[a-zA-Z]*(\s|$)|(^|[\s\"'])\+\S+)",
                _seg_forcing_form_scan_text(seg_check2),
            ):
                return _alt_record(
                    "deny", "destructive-git-orphan",
                    "BLOCKED: this 'git push' uses a forcing form (--force / -f / "
                    "+refspec) that rewrites remote history and can drop commits "
                    "existing only on the remote (a concurrent push you have not "
                    "fetched). --force-with-lease refuses the push if the remote "
                    "moved since your last fetch — exactly the protection plain "
                    "--force discards.",
                    (_offer("git push <remote> <branch> --force-with-lease", "the remote and branch"),),
                    _ORPHAN_ENV, **_ORPHAN_KW,
                    em_text=(
                        "BLOCKED: this 'git push' uses a forcing form (--force / -f / "
                        "+refspec) that rewrites remote history and can drop commits "
                        "existing only on the remote (a concurrent push you have not "
                        "fetched).\n\n"
                        "Use --force-with-lease instead. It refuses the push if the "
                        "remote moved since your last fetch — exactly the protection "
                        "plain --force discards:\n"
                        "  git push <remote> <branch> --force-with-lease"
                        + ("\n\n" + _orphan_hint if _orphan_hint else "")
                    ),
                )

        # CHECK 3 -- force-delete branch
        if re.search(r"\bbranch\b", seg):
            force_delete = False
            if re.search(r"(^|\s)-[a-zA-Z]*D[a-zA-Z]*(\s|$)", seg):
                force_delete = True
            if (
                re.search(r"((^|\s)-[a-zA-Z]*d[a-zA-Z]*(\s|$)|--delete)", seg)
                and re.search(r"((^|\s)-[a-zA-Z]*f[a-zA-Z]*(\s|$)|--force)", seg)
            ):
                force_delete = True
            if force_delete:
                # F0-b: memoized -- `git_cwd` is the only input (a fixed
                # "is this a repo" probe), so it is loop-invariant across
                # every force-delete segment sharing the same -C/cwd, unlike
                # the two calls below whose result varies with `br` (branch
                # name) per segment.
                rc, _ = _memo_run_git(["rev-parse", "--git-dir"], git_cwd)
                if rc == 0:
                    after = re.sub(r".*(^|\s)branch(\s|$)", " ", seg, count=1)
                    # F0-c: a single batched membership set, NOT the
                    # exact-key memo above -- `br` varies per token, so the
                    # (cwd, args)-keyed memo cannot collapse a per-token
                    # `rev-parse --verify`. See `_local_branch_names`
                    # docstring for the equivalence argument and the fail-
                    # direction preserved on lookup failure.
                    _branch_names = _local_branch_names(_memo_run_git, git_cwd)
                    for tok in after.split():
                        if tok.startswith("-"):
                            continue
                        br = _strip_q(tok)
                        if not _branch_names or br not in _branch_names:
                            continue
                        rc3, out3 = _memo_run_git(
                            ["branch", "-a", "--contains", "refs/heads/%s" % br, "--format=%(refname)"],
                            git_cwd,
                        )
                        others = [
                            line for line in out3.splitlines()
                            if line != "refs/heads/%s" % br and not re.match(r"refs/remotes/[^/]*/HEAD", line)
                        ]
                        if not others:
                            _branch_trailer = (
                                "\n\nIf '%s' is truly disposable, %s" % (br, _orphan_hint)
                                if _orphan_hint
                                else ""
                            )
                            return _alt_record(
                                "deny", "destructive-git-orphan",
                                "BLOCKED: force-deleting branch '%s' would orphan "
                                "its commits — they live on NO other ref (no local "
                                "branch, no remote). '%s' is not contained in any "
                                "other branch or remote. The lowercase, non-forced "
                                "'git branch -d' refuses exactly this case; the force "
                                "form ('-D', or '-d --force') overrides that safety."
                                % (br, br),
                                (_offer("git checkout <target> && git merge %s" % br,
                                        "the target branch; preserves the work first"),),
                                _ORPHAN_ENV, **_ORPHAN_KW,
                                em_text=(
                                    "BLOCKED: force-deleting branch '%s' would orphan "
                                    "its commits — they live on NO other ref (no local "
                                    "branch, no remote).\n\n"
                                    "'%s' is not contained in any other branch or "
                                    "remote. The lowercase, non-forced 'git branch -d' "
                                    "refuses exactly this case; the force form ('-D', or "
                                    "'-d --force') overrides that safety.\n\n"
                                    "To preserve the work first:\n"
                                    "  git checkout <target> && git merge %s"
                                    % (br, br, br)
                                )
                                + _branch_trailer,
                            )

    # BX-13 rescan (see comment near `raw_cmd` above): unwrap any
    # `sh -c`/`bash -c`/etc. payload from the RAW, pre-stripping command text
    # and re-run this SAME function against it, so a wrapped `git reset
    # --hard $(...)` (or force-push, or force-delete-branch) gets the full
    # CHECK 1-3 treatment rather than a second, independently-drifting
    # implementation. Bounded recursion: `_shell_c_unwrap_payloads` itself
    # caps nesting depth and fails safe (`[]`) on unparseable input, so this
    # can only ADD coverage, never loop or crash.
    for _shell_payload in _shell_c_unwrap_payloads(raw_cmd):
        _result = check_destructive_git_orphan(_shell_payload, session_id, payload, git_root)
        if _result is not None:
            return _result

    return None


def _local_branch_names(
    memo_run_git: Callable[[List[str], Optional[str]], Tuple[int, str]], git_cwd: Optional[str]
) -> Optional[set]:
    """Single-spawn membership set for "does refs/heads/<name> exist",
    replacing a `rev-parse --verify refs/heads/<name>` spawned once PER
    BRANCH TOKEN (F0-c, 2026-08-05: `git branch -D <N tokens>` spawned N
    `rev-parse --verify` calls -- exactly 1 per token, ~1,780 ms at 400
    tokens). Unlike the reset/rm legs (F0-b), the per-call args here
    genuinely VARY per token (a different branch name each time), so the
    exact-(cwd, args)-keyed `_new_git_memo` cannot collapse repeats of THIS
    call the way it does for the reset/rm legs -- the fix instead replaces
    N distinct existence probes with a single enumeration.

    `git for-each-ref --format=%(refname:short) refs/heads/` lists every
    local branch in ONE spawn, ref-backend agnostic (loose or packed) and
    correct for slash-named branches (`feature/x` prints as `feature/x`,
    not `refs/heads/feature/x`) -- the same domain `rev-parse --verify
    refs/heads/<name>` was checking membership of, so a Python `in` test
    against the returned set is an equivalent existence check, not an
    approximation.

    Returns `None` on a for-each-ref failure so the caller treats EVERY
    candidate branch the way a failed `rev-parse --verify` would have
    (skip, never deny) -- preserving the "a nonexistent/unresolvable ref
    causes `continue`, not a deny" fail direction this guard has always
    had. The call itself is routed through the shared per-call
    `memo_run_git` (args are constant per `git_cwd`, so repeated per-
    segment calls within one dispatch already collapse to a single real
    spawn via that memo, same as any other loop-invariant call in this
    function).
    """
    rc, out = memo_run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], git_cwd)
    if rc != 0:
        return None
    return set(out.splitlines())


# ---------------------------------------------------------------------------
# 4. check_destructive_git_clean -- block-destructive-git-clean.sh
# ---------------------------------------------------------------------------

_GC_CLEAN_CMD_RE = re.compile(
    r"^\s*((sudo|command|time|exec|nice|nohup|ionice|timeout|stdbuf|which|type)\s+|"
    r"env\s+(\S+=\S*\s+)*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(/\S*/)?git(\s+(-C\s+\S+|"
    r"-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|--exec-path(=\S+|\s+\S+)?|"
    r"-p|--paginate|--no-pager|--bare|--no-replace-objects|--literal-pathspecs|"
    r"--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*"
    r"\s+clean\b"
)

def _gc_is_clean_segment(seg: str) -> bool:
    prev = None
    s = seg
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("$("):
            s = s[2:]
        elif s.startswith("("):
            s = s[1:]
        elif s.startswith("{"):
            s = s[1:]
        elif s.startswith("!"):
            s = s[1:]
        elif s.startswith("`"):
            s = s[1:]
        elif s.startswith("\\"):
            s = s[1:]
    probe = s.replace("'", "").replace('"', "")
    # Bundled `-c` (2026-07-29, EM-run second-pass integration, confirmed
    # live): `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact `-c`
    # token requirement missed it -- `sh -ic "rm -rf ..."` / `sh -ic "git
    # clean -fdx"` / `sh -ic "git <revert-verb>..."` all silently sailed
    # through as prose. Widened to `-[a-zA-Z]*c[a-zA-Z]*` (same tolerance as
    # `_BUNDLED_C_FLAG_RE` elsewhere in this file/package).
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        return bool(re.search(r"\bgit\b.*\bclean\b", probe))
    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        return bool(re.search(r"\bgit\b.*\bclean\b", s))
    return bool(_GC_CLEAN_CMD_RE.search(probe))


def check_destructive_git_clean(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    cmd = _strip_heredoc_bodies(cmd)

    # C3: see `_ps_git_bypass_segments` docstring -- git's own argv is
    # unchanged across dialects, so the gap closed here is the anti-bypass
    # surface, not a new vocabulary. `None` (unparseable PowerShell) is
    # treated the same as "found nothing" for gate purposes -- the bash
    # leg's own regex gate still governs whether this returns None here.
    _gc_ps_segments = _ps_git_bypass_segments(
        cmd, "destructive-git-clean", payload
    ) or []

    if (
        not _word_present(r"\bgit\b", cmd)
        or not _word_present(r"\bclean\b", cmd)
    ) and not _gc_ps_segments:
        return None
    if _override("COORDINATOR_OVERRIDE_GIT_CLEAN", payload=payload):
        return None

    # Memo: a chained command repeating the same `git clean` invocation
    # (`git clean -nd dirA && git clean -nd dirA`) would re-run the oracle per
    # segment. `extra_env` is a fixed `{"LC_ALL": "C"}`, so it stays out of
    # the memo key.
    _clean_oracle_memo: Dict[Tuple[str, ...], Tuple[int, str]] = {}
    # The oracle must run where the command runs: git then resolves the
    # innermost repo itself, instead of the hook process's own cwd repo.
    _pc = (payload or {}).get("cwd") if isinstance(payload, dict) else None
    _clean_oracle_cwd = _pc if isinstance(_pc, str) and os.path.isdir(_pc) else None

    def _memo_run_clean_oracle(args: List[str]) -> Tuple[int, str]:
        key = tuple(args)
        cached = _clean_oracle_memo.get(key)
        if cached is None:
            cached = _dc_git_probe._run_git(args, cwd=_clean_oracle_cwd, extra_env={"LC_ALL": "C"})
            _clean_oracle_memo[key] = cached
        return cached

    for seg in list(_split_segments(cmd)) + _gc_ps_segments:
        if not seg.strip():
            continue
        if not _gc_is_clean_segment(seg):
            continue

        c_dir = _extract_git_c_dir(seg)
        after_clean = re.sub(r".*(^|\s)clean(\s|$)", " ", seg, count=1)

        if re.search(r"(^|\s)-[a-zA-Z]*n[a-zA-Z]*(\s|$)|(^|\s)--dry-run(\s|$)", after_clean):
            continue

        oracle_args = ["-c", "color.ui=never"]
        if c_dir:
            oracle_args += ["-C", c_dir]
        oracle_args += ["clean", "-nd"]

        if re.search(r"(^|\s)-[a-zA-Z]*X[a-zA-Z]*(\s|$)", after_clean):
            oracle_args.append("-X")
        elif re.search(r"(^|\s)-[a-zA-Z]*x[a-zA-Z]*(\s|$)", after_clean):
            oracle_args.append("-x")

        for m in re.finditer(r"(-e\s+(\S+)|--exclude=(\S+))", after_clean):
            pat = m.group(2) or m.group(3)
            if pat:
                oracle_args += ["-e", pat]

        pathspecs: List[str] = []
        if re.search(r"(^|\s)--(\s|$)", after_clean):
            tail = re.sub(r".*\s--\s*", "", after_clean, count=1)
            pathspecs = tail.split()

        rc, out = _memo_run_clean_oracle(oracle_args + (["--", *pathspecs] if pathspecs else []))
        if rc == -1:
            _clean_note = operator_override_note(
                "COORDINATOR_OVERRIDE_GIT_CLEAN", payload=payload, git_root=git_root
            )
            return _alt_record(
                "deny", "destructive-git-clean",
                "BLOCKED: git clean safety check timed out (2s) — cannot "
                "confirm no load-bearing files would be lost.",
                (_offer("git clean -nd [your flags]", "your flags"),),
                _CLEAN_ENV, **_CLEAN_KW,
                em_text=(
                    "BLOCKED: git clean safety check timed out (2s) — cannot "
                    "confirm no load-bearing files would be lost.\n\n"
                    "Use instead:\n"
                    "  git clean -nd [your flags]"
                    + ("\n\nOr: " + _clean_note if _clean_note else "")
                ),
            )
        if rc != 0:
            continue
        if not out.strip():
            continue

        loadbearing: List[str] = []
        for line in out.splitlines():
            if not line.startswith("Would remove "):
                continue
            p = line[len("Would remove "):].rstrip("/")
            if _is_loadbearing(p):
                loadbearing.append(p)

        if not loadbearing:
            continue

        shown_count = len(loadbearing)
        shown = "\n".join("  %s" % p for p in loadbearing[:8])
        more = "\n  ... and %d more (first 8 shown)" % (shown_count - 8) if shown_count > 8 else ""
        _clean_note2 = operator_override_note(
            "COORDINATOR_OVERRIDE_GIT_CLEAN", payload=payload, git_root=git_root
        )
        return _alt_record(
            "deny", "destructive-git-clean",
            "BLOCKED: git clean would delete %d untracked load-bearing "
            "file(s), unrecoverable (no commit/stash/reflog):\n%s%s"
            % (shown_count, shown, more),
            (_offer('git stash push -u -m "wip" -- <paths>', "the paths"),),
            _CLEAN_ENV, **_CLEAN_KW,
            em_text=(
                (
                    "BLOCKED: git clean would delete %d untracked load-bearing "
                    "file(s), unrecoverable (no commit/stash/reflog):\n%s%s\n\n"
                    "Use instead:\n"
                    '  git stash push -u -m "wip" -- <paths>'
                    % (shown_count, shown, more)
                )
                + ("\n\nOr: " + _clean_note2 if _clean_note2 else "")
            ),
        )

    return None


# ---------------------------------------------------------------------------
# 5. check_destructive_git_revert -- block-destructive-git-revert.sh
# ---------------------------------------------------------------------------

#: The verbs this guard resolves, in the order it tries them. Hoisted from
#: three identical inline tuples so a verb cannot be added to one resolution
#: path and forgotten in the other two.
#:
#: `git switch -f <branch>` discards every uncommitted modification exactly as
#: `git checkout -f <branch>` does. It carries no pathspec form (`git restore`
#: took that over), so only the force leg below ever speaks for it.
_GR_VERBS = ("checkout", "restore", "reset", "stash", "switch")

_GR_BASE_RE = (
    r"^\s*((sudo|command|time|exec|nice|nohup|ionice|timeout|stdbuf|which|type)\s+|"
    r"env\s+(\S+=\S*\s+)*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(/\S*/)?git(\s+(-C\s+\S+|"
    r"-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|--exec-path(=\S+|\s+\S+)?|"
    r"-p|--paginate|--no-pager|--bare|--no-replace-objects|--literal-pathspecs|"
    r"--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*\s+"
)




#: Reuses `block_subagent_destructive_action`'s shared normalizer rather than a
#: git-only copy: this module already imports from it and it imports nothing
#: from here, so no cycle. The shared one covers git+bash+sh+zsh+python[3]+
#: env+xargs and spaced UNC paths, and single-quotes an unquoted spaced path
#: so it lands as ONE `shlex` token.
def _normalize_windows_git_path_head(cmd: str) -> str:
    """Thin alias over the shared normalizer -- kept as its own name (rather
    than inlining the import at every call site) so this module's own
    `_command_really_invokes` docstring, which explicitly discusses this
    step, keeps a stable name to reference.
    """
    return _normalize_windows_argv0_head_path_with_spaces(cmd)


#: Same argv0-HEAD-position anchor as `_WINDOWS_GIT_PATH_HEAD_RE`, but the
#: leading path component is OPTIONAL and the trailing `git(?:\.exe)?` is
#: the entire match target -- collapses ANY head-position git invocation
#: (bare `git.exe`/`GIT.EXE`, a no-space POSIX path `/usr/bin/git.exe`, or a
#: spaced Windows path `C:\Program Files\Git\bin\git.exe`) down to the bare
#: literal `git`, case-folded. Review: code-reviewer -- Finding 3 (P2,
#: 2026-07-28): `check_destructive_git_revert`'s own verb-resolution
#: (`_gr_is_revert_segment`/`_GR_BASE_RE`) requires a LITERAL `git` token
#: immediately followed by known flags/whitespace -- `_command_really_
#: invokes`'s Windows-exe corroboration (added same diff) is only ever
#: reached from INSIDE the `stash` verb branch, which `_gr_is_revert_segment`
#: never enters for `git.exe`/`GIT.EXE`/a Windows-path git.exe spelling in
#: the first place, since `_GR_BASE_RE` has no `.exe`-suffix or spaced-path
#: awareness at all. Confirmed live (2026-07-28, this fix): pre-fix,
#: `check_destructive_git_revert("git.exe -C <repo> stash")` returned
#: `None` (silent allow) against a repo with real uncommitted peer work --
#: `_command_really_invokes`'s own passing unit tests never exercised the
#: real entrypoint, exactly the "tests pass on an unwired guard" failure
#: mode this session exists to eliminate. Fix: normalize the whole `cmd` to
#: its canonical bare-`git` spelling BEFORE segment-splitting/verb
#: resolution runs, so `_GR_BASE_RE`'s literal `git` match (and every
#: downstream `checkout`/`restore`/`reset`/`stash` verb walk) sees the same
#: text it already handles for a POSIX `git` invocation.
_GIT_EXE_HEAD_TO_BARE_RE = re.compile(
    r"(?P<sep>\A|[;&|\n])(?P<ws>\s*)(?P<q>[\"']?)"
    r"(?:(?:[A-Za-z]:)?[\\/](?:[^\\/\r\n]+?[\\/])*)?git(?:\.exe)?"
    r"(?P=q)(?=[\s;&|]|\Z)",
    re.IGNORECASE,
)


def _normalize_git_exe_head_to_bare(cmd: str) -> str:
    """Collapse a head-position git invocation -- however spelled (bare
    `git.exe`, case-varied `GIT.EXE`/`Git.exe`, a no-space POSIX path, or a
    spaced Windows path) -- down to the canonical bare `git` token, quotes
    and any path prefix dropped. See `_GIT_EXE_HEAD_TO_BARE_RE` for the
    exact-basename anchoring discipline (never substring matching:
    `gitk.exe`, `git-foo.exe`, and `legit.exe` are all left untouched --
    the literal boundary lookahead immediately after `git`/`git.exe` fails
    to match anything else attached to that basename).
    """

    def _rewrite(m: "re.Match[str]") -> str:
        return m.group("sep") + m.group("ws") + "git"

    return _GIT_EXE_HEAD_TO_BARE_RE.sub(_rewrite, cmd)


def _command_really_invokes(cmd: str, subcommand: str) -> bool:
    """True iff ``cmd`` invokes ``git <subcommand>`` at ARGV POSITION anywhere
    in the FULL command text -- the subcommand is the first non-flag token
    following a standalone ``git`` token, not merely a word appearing in the
    text.

    Deliberately evaluated over the whole command rather than per segment.
    `_split_segments` is not quote-aware, so a `|`/`;`/`&` inside a quoted
    operand -- `grep -i "... \\|git <verb>"` -- manufactures fragments
    (`git <verb>"`) that the free-text classifiers read as real invocations
    AND that `shlex` then refuses to tokenize (unterminated quote), so a
    per-segment corroboration would fail closed on precisely the fragments it
    exists to dismiss. `shlex` over the intact command keeps a quoted operand
    as ONE token that can never equal ``git``, which is what makes the
    mention case distinguishable at all.

    Fails CLOSED (returns True -- "treat as an invocation, keep checking")
    when the full command cannot be tokenized: this corroboration step exists
    to remove false positives, never to open a bypass.

    A Windows argv0 path (`C:\\Program Files\\Git\\bin\\git.exe`) is rewritten
    to its forward-slash equivalent via `_normalize_windows_git_path_head`
    BEFORE `shlex.split` runs: POSIX `shlex` treats a bare backslash as an
    escape character and silently eats it, which would otherwise mangle
    `C:\\Program Files\\Git\\bin\\git.exe` into two garbage tokens
    (`C:Program`, `FilesGitbingit.exe` -- the backslashes vanish AND the
    unescaped space still splits the path) with no fragment whose basename
    resolves to `git`. Converting to forward slashes first still leaves the
    unescaped-space split (`C:/Program Files/Git/bin/git.exe` ->
    `['C:/Program', 'Files/Git/bin/git.exe']`), but that is harmless here:
    the second fragment's OWN basename is still `git.exe`, which
    `_normalize_executable_basename` resolves correctly regardless of the
    unrelated garbage token ahead of it.
    """
    cmd = _normalize_windows_git_path_head(cmd)
    # Punctuation-adjacent-token fix (2026-07-29, EM-run second-pass
    # integration, confirmed live): a raw `shlex.split` does NOT separate
    # `;`/`&`/`|` from an immediately-preceding word with no whitespace
    # between them (`git stash;echo done` -> tokens `['git', 'stash;echo',
    # 'done']`... in fact even `{ git -C <dir> stash; }`, produced by this
    # very file's own `"{ %s; }" % cmd` test-shape template, tokenizes
    # `stash;` as ONE token) -- `tok == "stash"` then never matches, this
    # corroboration falls through to the next (nonexistent) `git` candidate,
    # and the sweep-shape `is_sweep_shape and not _command_really_invokes`
    # gate above silently skips a genuine unscoped `git stash`. Switched to
    # `_bt_tokenize_full_command` (this package's shared tokenizer,
    # `punctuation_chars=";&|"`), which always yields `;`/`&`/`|` as their
    # OWN separate tokens regardless of adjacent whitespace, while still
    # respecting POSIX quoting exactly as `shlex.split` did.
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return True
    n = len(tokens)
    for idx, tok in enumerate(tokens):
        # Windows-shaped executable tokens (`git.exe`, `C:\Program
        # Files\Git\bin\git.exe`) must normalize to the same identity as the
        # POSIX `git` token, or the corroboration this function exists to
        # perform fails OPEN on Windows -- see `_normalize_executable_
        # basename`'s own docstring for why this is exact-basename
        # normalization, never substring matching (`gitk`/`git-foo`/`mygit`
        # are unaffected).
        if _normalize_executable_basename(tok) != "git":
            continue
        # EVERY `git` occurrence is examined, not just the first. A compound
        # command routinely carries an unrelated git invocation ahead of the
        # one under test (`git add -A && git stash`, `git status; git stash
        # -u`); returning on the first occurrence's resolved subcommand would
        # let that earlier invocation mask the real one and wave the sweep
        # through. A non-matching occurrence therefore falls through to the
        # next candidate rather than deciding for the whole command.
        i = idx + 1
        while i < n:
            t = tokens[i]
            if t in _GIT_GLOBAL_OPT_WITH_ARG:
                # Consumes the FOLLOWING token as its value -- not advancing
                # past it resolves the VALUE as the subcommand (`git -C <dir>
                # stash` reading <dir> as the verb).
                i += 2
                continue
            if t.startswith("--") and "=" in t:
                i += 1
                continue
            if t.startswith("-"):
                if t in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                    i += 1
                    continue
                # Unrecognized flag: whether it consumes the next token is
                # unknown, so THIS occurrence's subcommand is unresolvable.
                # Fail CLOSED immediately rather than continuing to a later
                # occurrence -- "might be a real invocation, cannot tell" must
                # not be swept under some other occurrence's clean resolution.
                return True
            if t == subcommand:
                return True
            break
    return False


def _gr_is_revert_segment(seg: str) -> str:
    """Returns the matched verb (one of `_GR_VERBS`) or ""."""
    prev = None
    s = seg
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("$("):
            s = s[2:]
        elif s.startswith("("):
            s = s[1:]
        elif s.startswith("{"):
            s = s[1:]
        elif s.startswith("!"):
            s = s[1:]
        elif s.startswith("`"):
            s = s[1:]
        elif s.startswith("\\"):
            s = s[1:]
    probe = s.replace("'", "").replace('"', "")

    # Bundled `-c` (2026-07-29, EM-run second-pass integration, confirmed
    # live): `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact `-c`
    # token requirement missed it -- `sh -ic "rm -rf ..."` / `sh -ic "git
    # clean -fdx"` / `sh -ic "git <revert-verb>..."` all silently sailed
    # through as prose. Widened to `-[a-zA-Z]*c[a-zA-Z]*` (same tolerance as
    # `_BUNDLED_C_FLAG_RE` elsewhere in this file/package).
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        for verb in _GR_VERBS:
            if re.search(r"\bgit\b.*\b" + verb + r"\b", probe):
                return verb
        return ""

    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        for verb in _GR_VERBS:
            if re.search(r"\bgit\b.*\b" + verb + r"\b", s):
                return verb
        return ""

    for verb in _GR_VERBS:
        if re.search(_GR_BASE_RE + verb + r"\b", probe):
            return verb
    return ""


def _check_destructive_git_revert_full(
    cmd: str,
    session_id: str = "",
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Returns ``(deny_or_none, advisory_or_none)``.

    Split 2026-08-05 (Review: staff-eng, Finding 0 -- CONFINEMENT_DENY
    chain-shadowing): the advisory floor below must NOT return from the
    same function `dispatch.py` registers as a hard-deny at chain position
    5 -- `evaluate_payload_json` returns on the first non-None envelope, so
    an advisory returned there would short-circuit every hard-deny guard
    registered after it (`block-stash-destruction`,
    `block-subagent-stash-creation`, `block-subagent-destructive-action`,
    `block-subagent-commit`, ...), which `GuardBand`'s own docstring
    forbids for CONFINEMENT_DENY ("every entry in this band emits either a
    deny or silent None"). `check_destructive_git_revert` (the registered
    hard-deny) and `check_destructive_git_revert_advisory` (a separate,
    later-registered ADVISORY_REWRITE leg) are both thin wrappers over this
    function.

    Deliberately NOT `functools.lru_cache`d at this level -- module-scope
    memoization would leak stale `_run_git` mock results across unrelated
    test cases that happen to share a `cmd` string in the same interpreter.
    Instead `dispatch.py`'s `_build_guard_chain` memoizes this function's
    result for the lifetime of a single dispatch call (both legs share one
    call, one process-per-invocation), so a dispatch pass that evaluates
    both the hard-deny and advisory legs never re-spawns the underlying
    `git status`/`git rev-parse` calls -- this guard sits on the PreToolUse
    hot path under an end-to-end invocation budget.

    SC-DR-023 guard boundary (2026-09-26, CEPEF-B6): four whole-tree forms --
    pathspec-less `git stash`/`git stash push`, `git checkout .`/`git
    checkout -- .`, `git restore .`, and bare `git reset --hard` (no ref) --
    deny from the command string ALONE, with no `git status` probe and
    regardless of whether the tree is clean. SC-DR-023 asked for only an
    ADVISORY on this leg; this guard is stricter by design (DD3,
    docs/plans/2026-09-26-commit-emit-plane-engine-findings.md) and DoE's
    reply names the difference and asks whether to relax it. Every other
    shape here -- path-scoped forms, `git reset --hard <ref>`, force-flag
    checkout/switch (`-f`/`--force`), and `git clean` (its own check) --
    keeps today's probe-then-deny-on-load-bearing-or-peer-claim behavior,
    unchanged by this boundary. What this guard cannot catch: a whole-tree
    write made through a channel this dispatch never sees at all (a
    subprocess, an IDE action, a GUI git client) -- SC-DR-023 governs the
    Bash/PowerShell tool-call surface, not every way a tree can be reverted.
    """
    if not cmd:
        return None, None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    cmd = _strip_heredoc_bodies(cmd)
    # Dialect-aware Start-Process expansion (C8,
    # pln-the-destructive-core-learns-the-she): this entry's `matchers`
    # already declares `COMMAND_TOOL_NAMES` (dispatch.py) but the regex
    # pipeline below (`_GR_BASE_RE`/`_gr_is_revert_segment`) anchors on a
    # LITERAL `git` token at segment start, so a PowerShell `Start-Process
    # git -ArgumentList 'stash'` invocation evaded detection even though the
    # bare-text `\bgit\b`/`\bstash\b` gates a few lines below both matched --
    # confirmed live, see `docs/reference/guard-tool-name-membership.md` § 3z
    # "Coverage is PARTIAL". Resolved narrowly: for a PowerShell payload
    # only, tokenize via `_dialect.tokenize_command` and run the SAME
    # `expand_start_process_invocations` pass `resolve_segments_for_dialect`
    # already applies for every other converted entry, then rejoin the
    # expanded tokens back into text so the existing regex pipeline below
    # (unchanged, and still exercised byte-for-byte on the BASH leg) sees the
    # target's real argv in command position. A PowerShell parse failure
    # (`tokenize_command` returns `None`, already logged SILENT by that
    # call) leaves `cmd` untouched -- the pre-existing bare-text regex gates
    # below still get a chance at the raw string, matching this function's
    # long-standing behavior for every dialect before this change.
    _gr_dialect = dialect_from_tool_name((hook_payload or {}).get("tool_name"))
    if _gr_dialect is Dialect.POWERSHELL:
        _gr_ps_tokens = tokenize_command(cmd, _gr_dialect, guard_name="destructive-git-revert")
        if _gr_ps_tokens is not None:
            cmd = " ".join(expand_start_process_invocations(_gr_ps_tokens))
    # 2026-07-28): normalize a
    # head-position Windows-exe/case-varied git spelling to the bare `git`
    # token BEFORE verb resolution -- see `_normalize_git_exe_head_to_bare`
    # docstring for why this was a live entrypoint-level gap distinct from
    # (and not closed by) the `_command_really_invokes` corroboration below.
    cmd = _normalize_git_exe_head_to_bare(cmd)

    if not _word_present(r"\bgit\b", cmd):
        return None, None
    if not _word_present(r"\bcheckout\b|\brestore\b|\breset\b|\bstash\b|\bswitch\b", cmd):
        return None, None
    if _override("COORDINATOR_OVERRIDE_GIT_REVERT", payload=hook_payload):
        return None, None

    _gr_note = operator_override_note(
        "COORDINATOR_OVERRIDE_GIT_REVERT", payload=hook_payload, git_root=git_root
    )
    _gr_hint = (
        "If this revert is genuinely intended:\n\n" + _gr_note if _gr_note else ""
    )

    # Advisory floor (2026-08-05): `affected` non-empty but `deny_paths`
    # empty (no load-bearing/peer-claimed path in it) previously fell
    # through `if not deny_paths: continue` with ZERO signal -- see
    # cross-repo/inbox/2026-08-05-coordinator-content-repo-em-unscoped-stash-has-no-
    # main-loop-guard.md. `pending_advisory` accumulates the MOST
    # destructive such non-blocking envelope seen so far (Review:
    # staff-eng, Finding 11 -- `len(affected)` compared across segments,
    # not first-match-wins; the max-selection costs no extra bytes since
    # only one candidate is ever rendered) while the loop keeps scanning
    # every remaining segment; a deny found on ANY later segment (or in
    # the `_shell_c_unwrap_payloads` rescan below) still wins outright and
    # returns immediately, per this function's own deny-precedence
    # contract. `_pending_advisory_affected` tracks the `len(affected)`
    # the current `pending_advisory` was built from, so a later segment
    # with a larger sweep can replace an earlier, smaller one.
    pending_advisory: Optional[Dict[str, Any]] = None
    _pending_advisory_affected = -1

    # Latency defect (2026-08-05, dispatch-latency-bound corpus): `cmd` is
    # loop-invariant across every segment, but `_command_really_invokes`
    # tokenizes the WHOLE command on every call -- calling it from inside
    # the segment loop below made this function O(segments x len(cmd))
    # instead of O(len(cmd)). Resolved lazily, per verb, keyed by dict
    # membership (a legitimate `_command_really_invokes` result of `False`
    # for a verb is a real value in the dict, distinct from that verb's key
    # never having been computed at all -- `dict.__contains__`, not a
    # sentinel, is what tells the two apart here) rather than hoisted
    # eagerly above the loop: most commands never reach a matching-verb
    # segment at all, and an eager hoist would charge every one of THOSE
    # benign commands a tokenize pass they never needed. A fresh local per
    # call to this function is also correct across the `_shell_c_unwrap_
    # payloads` recursion a few lines below -- that recursion re-enters via
    # `check_destructive_git_revert`, a distinct call with its own stack
    # frame and its own `cmd`, so it gets its own independent memo rather
    # than reusing this one's. Widened 2026-08-05 (Review: staff-eng,
    # Finding 3) from a `stash`-only local to a per-verb cache covering all
    # four -- see the MENTION-vs-INVOCATION gate immediately below the verb
    # resolution for why every verb needs it, not stash alone.
    _verb_invocation_confirmed: Dict[str, bool] = {}

    # F0-b-adjacent spawn-count defect (2026-08-07, spawn-storm sweep D6):
    # the `git status --porcelain` oracle below ran once PER SEGMENT even
    # though `git_cwd` is almost always identical across a chained command
    # (`git checkout . && git reset --hard` hits the checkout branch then
    # the reset branch, both against the same working tree) -- the tree
    # cannot mutate mid-dispatch (a single synchronous hook invocation, no
    # concurrent mutator), so re-running the same-cwd status probe on
    # segment 2..N is pure repeated work, the same shape `_new_git_memo`
    # closes for the reset/orphan checks. NOT routed through that shared
    # `_memo_run_git` closure here: its memo drops `extra_env`, and this
    # oracle depends on `LC_ALL=C` for locale-independent porcelain output
    # -- reusing it verbatim would silently vary this check's behavior
    # under a non-C locale. A small local cache keyed on `git_cwd` alone
    # (the args are invariant across every call site below) preserves that
    # env pin while still collapsing the repeat.
    _status_porcelain_cache: Dict[Optional[str], Tuple[int, str]] = {}

    def _memo_status_porcelain(cwd: Optional[str]) -> Tuple[int, str]:
        if cwd not in _status_porcelain_cache:
            _status_porcelain_cache[cwd] = _dc_git_probe._run_git(
                ["--no-optional-locks", "status", "--porcelain"],
                cwd=cwd,
                extra_env={"LC_ALL": "C"},
            )
        return _status_porcelain_cache[cwd]

    # Sibling memo for the `rev-parse --show-toplevel` probe a few hundred
    # lines below, same shape as `_status_porcelain_cache` immediately
    # above: `git_cwd` is loop-invariant across every segment sharing one
    # `-C <dir>` (or none), and the repo root cannot change mid-dispatch, so
    # re-resolving it on segment 2..N of a chained revert command is pure
    # repeated work. Unlike the porcelain probe this call carries no
    # `extra_env`, so it is safe to route through `_run_git`'s plain
    # (cwd,)-only shape rather than needing its own env-pinned spawn.
    _toplevel_cache: Dict[Optional[str], Tuple[int, str]] = {}

    def _memo_show_toplevel(cwd: Optional[str]) -> Tuple[int, str]:
        if cwd not in _toplevel_cache:
            _toplevel_cache[cwd] = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
        return _toplevel_cache[cwd]

    # `_rm_peer_claim_of`'s uncommitted-delta gate, shared across every
    # affected path any segment below checks -- see `_new_dirty_paths_memo`'s
    # own docstring for why this MUST be built once here and threaded
    # through, rather than left for that function's own one-shot fallback
    # (which would spawn `git status` once per affected path per segment).
    _dirty_paths_lookup = _new_dirty_paths_memo()

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        verb = _gr_is_revert_segment(seg)
        if not verb:
            continue

        # MENTION-vs-INVOCATION gate (Review: staff-eng, Finding 3 --
        # widened 2026-08-05 from the stash branch alone to all four verbs).
        # `_split_segments` splits on `;`/`&`/`|` WITHOUT quote awareness, so
        # a `|` inside a quoted operand -- e.g. `grep -i "reset --hard\|git
        # checkout ."` -- manufactures a bogus segment (`git checkout ."`)
        # that `_gr_is_revert_segment`'s free-text classifier above reads as
        # a real invocation. Before the advisory floor (2026-08-05) a bogus
        # segment on checkout/restore/reset was inert whenever `_run_git`
        # found nothing dirty, but a dirty tree now turns that same bogus
        # segment into a live advisory -- the identical class of latent bug
        # Finding 3 names for stash, now reachable on the other three verbs
        # too. Corroborate the free-text verdict against argv position --
        # the same anchored-subcommand discipline `block_subagent_
        # destructive_action` adopted for its own mention-vs-invocation
        # false positives -- and skip the segment when it does not hold up.
        if verb not in _verb_invocation_confirmed:
            _verb_invocation_confirmed[verb] = _command_really_invokes(cmd, verb)
        if not _verb_invocation_confirmed[verb]:
            continue

        c_dir = _extract_git_c_dir(seg)
        # A leading `cd <dir> &&` ahead of this segment's own `git ...`
        # moves the executed command's cwd away from the guard process's --
        # see `_bt_leading_cd_prefix_cwd`'s docstring and state/bug-backlog/
        # 2026-08-27-destructive-git-and-scope-guards-resolve-
        # 295928a71726.yaml (the `cd <fixture> && git checkout -q .` shape
        # that blocked a harmless command on a foreign repo's uncommitted
        # files). `-C <dir>` on THIS segment still wins where present.
        git_cwd = _orphan_c_cwd(
            c_dir, (hook_payload or {}).get("cwd") or git_root
        ) or _bt_leading_cd_prefix_cwd(cmd, (hook_payload or {}).get("cwd") or None)
        after = re.sub(r".*(^|\s)" + verb + r"(\s|$)", " ", seg, count=1)

        affected: List[str] = []
        rc = 0
        out = ""

        if verb in ("checkout", "restore", "switch"):
            toks = after.split()
            dotspec = False
            seen_dashdash = False
            for tk in toks:
                if tk == "--":
                    seen_dashdash = True
                    continue
                if not tk.startswith("-") or seen_dashdash:
                    if tk.rstrip("/") == ".":
                        dotspec = True
            # A force flag with no `--` pathspec separator discards the WHOLE
            # worktree -- `git checkout -f`, and `git checkout -f <branch>`,
            # both throw away every uncommitted modification, which is
            # strictly MORE than the `git checkout .` this leg already denies.
            # Until 2026-08-30 only `dotspec` built an `affected` set, so the
            # larger clobber fell through the guard silently while the smaller
            # one was blocked. `block_subagent_destructive_action` had already
            # been hardened for exactly this shape (its own docstring: "`git
            # checkout -f` -- discards ALL uncommitted work; the pathspec/
            # dashdash checkout deny only covered a strictly smaller
            # clobber"), but that guard engages only after a subagent-identity
            # check, so a main-loop or EM session -- which is what most
            # sessions on a shared worktree are -- reached nothing.
            #
            # Gated on `not seen_dashdash` deliberately: `git checkout -f --
            # <paths>` IS scoped to those paths, and the pre-existing pathspec
            # handling above (including `-- .`) stays the only thing that
            # speaks for it. Force plus an explicit pathspec is narrower than
            # force alone, and must not be widened to whole-tree here.
            force_whole_tree = (
                verb in ("checkout", "switch")
                and not seen_dashdash
                and bool(
                    re.search(r"(^|\s)(--force|-[a-zA-Z]*f[a-zA-Z]*)(\s|$)", after)
                )
            )
            if dotspec or force_whole_tree:
                has_staged = bool(re.search(r"(^|\s)(-[a-zA-Z]*S[a-zA-Z]*|--staged)(\s|$)", after))
                has_worktree = bool(re.search(r"(^|\s)(-[a-zA-Z]*W[a-zA-Z]*|--worktree)(\s|$)", after))
                if verb == "restore" and has_staged and not has_worktree:
                    continue
                # SC-DR-023 (CEPEF-B6, DD3): `git checkout .`/`git checkout
                # -- .` and `git restore .` are two of the four whole-tree
                # forms named in the ruling -- deny from the command string
                # alone, before any `git status` probe, regardless of
                # whether the tree is clean. `switch` and the force-flag
                # leg (`force_whole_tree`) are NOT part of SC-DR-023's four
                # forms and keep today's probe-then-deny-on-risk behavior
                # below, unchanged.
                if dotspec and verb in ("checkout", "restore"):
                    return _alt_record(
                        "deny", "destructive-git-revert",
                        "BLOCKED: 'git %s .' is whole-tree (SC-DR-023). Scope or preserve." % verb,
                        (
                            _offer("git %s -- <your-paths>" % verb, "your paths"),
                            _offer('git stash push -u -m "before-revert" -- <paths>', "the paths"),
                        ),
                        _REVERT_ENV, **_REVERT_KW,
                        em_text=(
                            "BLOCKED: 'git %s .' is whole-tree "
                            "(SC-DR-023).\n\n"
                            "Scope or preserve:\n"
                            "  git %s -- <your-paths>\n"
                            '  git stash push -u -m "before-revert" -- <paths>'
                            % (verb, verb)
                            + ("\n\nOr: %s" % _gr_note if _gr_note else "")
                        ),
                    ), None
                # Name the shape actually seen: `git checkout -f` reaches this
                # oracle too now, and reporting it as `git checkout .` would
                # describe a command the caller never typed.
                shape = "git %s -f" % verb if force_whole_tree and not dotspec else "git %s ." % verb
                rc, out = _memo_status_porcelain(git_cwd)
                if rc == -1:
                    return _alt_record(
                        "deny", "destructive-git-revert",
                        "BLOCKED: '%s' oracle (git status) timed out (2 s) — cannot "
                        "verify the revert is safe. Verify what would be discarded, "
                        "then preserve it and re-run." % (shape,),
                        (
                            _ready("git status --porcelain"),
                            _offer('git stash push -u -m "before-revert" -- <paths>', "the paths"),
                        ),
                        _REVERT_ENV, **_REVERT_KW,
                        em_text=(
                            "BLOCKED: '%s' oracle (git status) timed "
                            "out (2 s) — cannot verify the revert is safe.\n\n"
                            "Safe path first — verify what would be discarded, "
                            "then preserve it:\n"
                            "  git status --porcelain\n"
                            '  git stash push -u -m "before-revert" -- <paths>   '
                            "# make it recoverable"
                            % (shape,)
                            + ("\n\nAfter preserving the files, re-run. Or:\n%s" % _gr_hint if _gr_hint else "")
                        ),
                    ), None
                if rc != 0:
                    continue
                for line in out.splitlines():
                    if not line:
                        continue
                    cols = line[:2]
                    path = line[3:]
                    if " -> " in path:
                        path = path.rsplit(" -> ", 1)[-1]
                    if cols == "??":
                        continue
                    if cols[1:2] != " ":
                        affected.append(path)

        elif verb == "reset":
            if re.search(r"(^|\s)--hard(\s|$)", after):
                # SC-DR-023 (CEPEF-B6, DD3): bare `git reset --hard` (no ref)
                # is one of the four whole-tree forms -- deny from the
                # command string alone, before any `git status` probe.
                # `git reset --hard <ref>` still names a target and stays on
                # today's probe-then-deny path below (the orphan check's own
                # case; unchanged by this boundary).
                _reset_hard_tokens = [
                    tk for tk in after.split() if tk != "--hard" and not tk.startswith("-")
                ]
                if not _reset_hard_tokens:
                    return _alt_record(
                        "deny", "destructive-git-revert",
                        "BLOCKED: 'git reset --hard' is a whole-tree revert "
                        "(SC-DR-023) -- denied from the command alone, with "
                        "no git-status check of what it would discard. Preserve "
                        "it first instead of discarding it: stash, then reset.",
                        (
                            _ready('git stash push -u -m "before-reset"'),
                            _ready("git reset --hard"),
                        ),
                        _REVERT_ENV, **_REVERT_KW,
                        em_text=(
                            "BLOCKED: 'git reset --hard' is a whole-tree revert "
                            "(SC-DR-023) -- denied from the command alone, with "
                            "no git-status check of what it would discard.\n\n"
                            "Preserve it first instead of discarding it -- "
                            "stash, then reset:\n"
                            '  git stash push -u -m "before-reset"\n'
                            "  git reset --hard"
                            + ("\n\nOr: %s" % _gr_note if _gr_note else "")
                        ),
                    ), None
                rc, out = _memo_status_porcelain(git_cwd)
                if rc == -1:
                    return _alt_record(
                        "deny", "destructive-git-revert",
                        "BLOCKED: 'git reset --hard' oracle (git status) "
                        "timed out (2 s) — cannot verify the reset is safe. "
                        "Verify what would be discarded, then preserve it and re-run.",
                        (
                            _ready("git status --porcelain"),
                            _offer('git stash push -u -m "before-reset" -- <paths>', "the paths"),
                        ),
                        _REVERT_ENV, **_REVERT_KW,
                        em_text=(
                            "BLOCKED: 'git reset --hard' oracle (git status) "
                            "timed out (2 s) — cannot verify the reset is "
                            "safe.\n\n"
                            "Safe path first — verify what would be discarded, "
                            "then preserve it:\n"
                            "  git status --porcelain\n"
                            '  git stash push -u -m "before-reset" -- <paths>   '
                            "# make it recoverable"
                            + ("\n\nAfter preserving the files, re-run. Or:\n" + _gr_hint if _gr_hint else "")
                        ),
                    ), None
                if rc != 0:
                    continue
                for line in out.splitlines():
                    if not line:
                        continue
                    cols = line[:2]
                    path = line[3:]
                    if " -> " in path:
                        path = path.rsplit(" -> ", 1)[-1]
                    if cols == "??":
                        continue
                    if cols != "  ":
                        affected.append(path)

        elif verb == "stash":
            # `-u`/`-a` widen a stash to ALSO take untracked files; they do not
            # make it a sweep. A bare `git stash` / `git stash push` already
            # sweeps every tracked modification in the tree -- including every
            # peer session's in-flight edits -- so the write-shape, not the
            # untracked flag, is what this check gates on. Mirrors the
            # subagent-side rule already shipped in
            # `block_subagent_destructive_action` (2026-07-26 "UNSCOPED-STASH
            # GAP CLOSE"): the sweep shapes are bare `stash`, explicit `push`,
            # and the implicit-push flag-only form (`git stash -u`), and a
            # `--`-delimited pathspec is what scopes it back to the caller's
            # own paths.
            has_dashdash = bool(re.search(r"(^|\s)--(\s|$)", after))
            after_toks = after.split()
            first_tok = after_toks[0] if after_toks else ""
            # Non-sweep subcommands: read-only (`list`/`show`), a different
            # write shape entirely (`branch`/`create`/`store` -- these write a
            # stash OBJECT without touching the working tree), or already
            # covered by their own dedicated checks (`pop`/`apply`/`drop`/
            # `clear`).
            #
            # `save` is deliberately NOT excluded: `git stash save [<msg>]` is
            # the pre-2.16 deprecated spelling of `git stash push [-m <msg>]`,
            # with identical working-tree effect -- it sweeps every tracked
            # modification exactly as the bare form does. Grouping it with
            # `create`/`store` reads plausible and is wrong.
            is_sweep_shape = first_tok not in (
                "pop", "apply", "drop", "list", "show", "clear",
                "branch", "create", "store",
            )
            # The mention-vs-invocation gate for this segment's verb
            # ("stash") already ran, unconditionally, right after `verb`
            # was resolved above (Review: staff-eng, Finding 3 -- hoisted
            # 2026-08-05 from a sweep-shape-only, stash-only local check to
            # a per-verb gate covering every segment of every verb before
            # any verb-specific branch runs) -- confirmed live 2026-07-28
            # (a `grep -i "stash -u\|git stash"` bogus segment) and by the
            # rows this dispatch adds to `test_check_destructive_git_revert_
            # stash.py` for reset/checkout/restore over the same fixture,
            # so this branch never needs its own local corroboration.
            if is_sweep_shape and not has_dashdash:
                # SC-DR-023 (CEPEF-B6, DD3): pathspec-less `git stash`/`git
                # stash push` (and its `save` alias) is one of the four
                # whole-tree forms -- deny from the command string alone,
                # before any `git status` probe. A `--`-delimited pathspec
                # (`has_dashdash`) already routes around this branch
                # entirely and stays on today's allowed path.
                return _alt_record(
                    "deny", "destructive-git-revert",
                    "BLOCKED: unscoped 'git stash' is a whole-tree revert "
                    "(SC-DR-023) -- sweeps every session's uncommitted work. Scope it.",
                    (_offer('git stash push -u -m "before-stash" -- <your-paths>', "your paths"),),
                    _REVERT_ENV, **_REVERT_KW,
                    em_text=(
                        "BLOCKED: unscoped 'git stash' is a whole-tree revert "
                        "(SC-DR-023) -- sweeps every session's uncommitted "
                        "work.\n\n"
                        "Scope it instead:\n"
                        '  git stash push -u -m "before-stash" -- <your-paths>'
                        + ("\n\nOr: %s" % _gr_note if _gr_note else "")
                    ),
                ), None

        if not affected:
            continue

        rc_root, out_root = _memo_show_toplevel(git_cwd)
        repo_root = out_root.strip() if rc_root == 0 else ""

        deny_paths: List[Tuple[str, str]] = []
        for ap in affected:
            lb = _is_loadbearing(ap)
            peer_sid = (
                _rm_peer_claim_of(
                    ap if os.path.isabs(ap) else os.path.join(repo_root, ap),
                    repo_root,
                    _dirty_paths_lookup,
                )
                if repo_root
                else ""
            )
            if lb and peer_sid:
                deny_paths.append((ap, "load-bearing, peer-claimed by %s" % peer_sid))
            elif lb:
                deny_paths.append((ap, "load-bearing"))
            elif peer_sid:
                deny_paths.append((ap, "peer-claimed by %s" % peer_sid))

        verb_label = {"reset": "git reset --hard", "stash": "git stash (unscoped)"}.get(verb, "git %s" % verb)
        _scope = (
            _offer("git checkout -- <your-paths>", "your paths"),
            _offer("git restore -- <your-paths>", "your paths"),
        )
        _stash_rev = _offer('git stash push -u -m "before-revert" -- <paths>', "the paths")
        _stash_rst = (_ready('git stash push -u -m "before-reset"'), _ready("git reset --hard"))
        _stash_scope = _offer('git stash push -u -m "before-stash" -- <your-paths>', "your paths")
        if verb in ("checkout", "restore"):
            safe_path = (
                "Did you mean to scope this to your own paths?\n"
                "  git checkout -- <your-paths>\n"
                "  git restore -- <your-paths>\n\n"
                "Preserve first (recoverable stash of everything, including "
                "untracked):\n"
                '  git stash push -u -m "before-revert" -- <paths>'
            )
        elif verb == "reset":
            safe_path = (
                "Preserve it first instead of discarding it -- stash, "
                "then reset:\n"
                '  git stash push -u -m "before-reset"\n'
                "  git reset --hard"
            )
        else:
            safe_path = (
                "Scope the stash to your own paths instead of sweeping every "
                "session's uncommitted work:\n"
                '  git stash push -u -m "before-stash" -- <your-paths>'
            )

        if not deny_paths:
            # Nothing in `affected` is load-bearing or peer-claimed -- no
            # deny warranted, but the caller still gets a non-blocking
            # nudge rather than silence. `safe_path` is reused verbatim
            # (never re-authored) per this floor's own message-size
            # discipline; checkout/restore's two-paragraph form is trimmed
            # to its first paragraph only -- the cue-exempt "Did you mean"
            # offer -- because the full two-paragraph form plus the lead
            # sentence clears MESSAGE_PROSE_CAP_BYTES (measured: 258 > 220)
            # where the trimmed form measures 116. `reset`/`stash` keep
            # their full `safe_path` -- both already clear cap intact.
            #
            # The lead sentence is VERB-CONDITIONED because the hazard is
            # not the same one. A stash IS recoverable (`pop`/`apply` are
            # the EM's own restore path -- see `block_stash_destruction.
            # py`'s "WHY DROP/CLEAR AND NOT POP/APPLY"); its hazard is that
            # it sweeps every session's work in a shared tree, not that the
            # content is gone. `reset --hard`/`checkout .` genuinely leave
            # no recovery path -- EXCEPT for a staged entry: its blob is
            # already in the object database and survives (recoverable via
            # `git fsck --lost-found`), unlike an unstaged/untracked one.
            # `reset --hard`'s own `affected` collection (`cols != "  "`)
            # includes staged entries, so its harm text carries the same
            # qualifier the hard-deny message for this verb below now also
            # states (Review: staff-eng, Finding 4; Review: code-reviewer,
            # Finding 1) -- an advisory that overstates is one the next
            # reader discounts.
            if pending_advisory is None or len(affected) > _pending_advisory_affected:
                offer = _scope if verb in ("checkout", "restore") else _stash_rst if verb == "reset" else (_stash_scope,)
                if verb == "stash":
                    # Finding 12: this exact branch has already confirmed
                    # `repo_root`/`_rm_peer_claim_of` found no peer claim on
                    # any affected path -- "including any peer session's" is
                    # not what was checked here; "unclaimed" is.
                    harm = "sweep %d uncommitted file(s), including any unclaimed peer work"
                elif verb == "reset":
                    harm = (
                        "discard %d uncommitted file(s) (staged ones stay "
                        "recoverable via `git fsck --lost-found`)"
                    )
                else:
                    harm = "discard %d uncommitted file(s) git cannot recover"
                pending_advisory = _alt_record(
                    "advisory", "destructive-git-revert-advisory",
                    "ADVISORY: %s would %s." % (verb_label, harm % len(affected)),
                    offer, _REVERT_ENV, **_REVERT_KW,
                    em_text=(
                        "ADVISORY: %s would %s.\n\n%s"
                        % (
                            verb_label,
                            harm % len(affected),
                            safe_path.split("\n\n", 1)[0] if verb in ("checkout", "restore") else safe_path,
                        )
                    )
                    + ("\n\nOr: %s" % _gr_note if _gr_note else ""),
                )
                _pending_advisory_affected = len(affected)
            continue

        shown_count = len(deny_paths)
        # 3, not the former 8 -- a trim, deliberately not a drop.
        #
        # Every listed path is charged against `MESSAGE_PROSE_CAP_BYTES`:
        # `_message_size._is_diagnostic_echo` excludes what-was-denied prose
        # from exemption, so this message's size tracks the CALLER's path
        # lengths, which are unbounded. Measured on one file: 219 bytes for
        # `state/x.json`, 251 for a realistically-named artifact; twelve
        # files ran 721. No wording change can hold the cap, because the
        # wording was never what breached it.
        #
        # Replacing the list with a bounded reason histogram DOES hold it
        # (measured: 208 worst case at any file count or path length) and is
        # the wrong trade -- 21 tests pin that a denied path is named, and
        # for a peer-claimed path that name is the entire decision-relevant
        # content. The operator can get paths from `git status`; they cannot
        # get "this one belongs to session X" from anywhere else.
        #
        # So: trim to bound the common case, keep the contract, and leave
        # the residual breach visible rather than buying the cap with the
        # message's reason for existing. Closing it properly means the cap
        # counting found-data differently from authored prose, which is
        # `_message_size`'s contract to change, not this call site's.
        _SHOWN_PATH_LIMIT = 3
        shown = "".join("  %s (%s)\n" % (p, r) for p, r in deny_paths[:_SHOWN_PATH_LIMIT])
        # Terse by necessity, not by taste: this trailer is guard-authored
        # prose, so it sits outside the found-data block `_message_size`
        # now discounts (a blank line separates them by construction, and
        # it is not a path entry, so folding it in would disqualify the
        # whole block). The long form spent ~32 bytes of the prose budget
        # restating a limit the reader can see; `+N more` spends 9.
        more = "\n  +%d more" % (shown_count - _SHOWN_PATH_LIMIT) if shown_count > _SHOWN_PATH_LIMIT else ""

        if verb == "reset":
            # `reset`'s own `affected`/`deny_paths` collection (`cols != "  "`)
            # includes staged entries, whose blob survives in the object
            # database (recoverable via `git fsck --lost-found`) unlike an
            # unstaged/untracked one -- this deny text carries the same
            # qualifier the advisory harm text for this verb states above,
            # so the two paths agree on the same hazard class instead of the
            # deny overstating what the advisory just corrected.
            # Offer and override-route are built here rather than reusing the
            # shared `safe_path`/`_GR_OVERRIDE_HINT` pair: this branch is the
            # one under the prose cap by the narrowest margin, and both shared
            # strings spend their bytes on preamble (`_GR_OVERRIDE_HINT` opens
            # with a sentence before naming the variable, where
            # `operator_override_note` is measured tail-exempt).
            deny_reason = (
                "BLOCKED: '%s' discards %d uncommitted file(s). Staged blobs "
                "survive in `git fsck --lost-found`; unstaged and untracked "
                "edits do not:\n%s%s\nPreserve it first."
                % (verb_label, shown_count, shown, more)
            )
            deny_members = _stash_rst
        elif verb == "stash":
            # Verb-conditioned for the same reason the advisory harm text
            # above is: a stash is RECOVERABLE (`pop`/`apply`), so the shared
            # "git CANNOT recover" lead was false on this path -- the same
            # overstatement already corrected one branch up, which the deny
            # had gone on making.
            deny_reason = (
                "BLOCKED: '%s' sweeps %d uncommitted file(s), including work "
                "no session has claimed:\n%s%s\nScope it to your own paths."
                % (verb_label, shown_count, shown, more)
            )
            deny_members = (_stash_scope,)
        else:
            deny_reason = (
                "BLOCKED: '%s' discards %d uncommitted file(s) git cannot "
                "recover — no commit, stash, or reflog holds them:\n%s%s\n"
                "Scope it, or preserve everything first."
                % (verb_label, shown_count, shown, more)
            )
            deny_members = _scope + (_stash_rev,)

        if verb == "reset":
            deny_em = (
                "BLOCKED: '%s' discards %d uncommitted file(s). Staged blobs "
                "survive in `git fsck --lost-found`; unstaged and untracked "
                "edits do not:\n%s%s\n\n"
                "Preserve it first instead:\n"
                '  git stash push -u -m "before-reset"\n'
                "  git reset --hard"
                % (verb_label, shown_count, shown, more)
            )
        elif verb == "stash":
            deny_em = (
                "BLOCKED: '%s' sweeps %d uncommitted file(s), including work "
                "no session has claimed:\n%s%s\n\n"
                "Scope it to your own paths instead:\n"
                '  git stash push -u -m "before-stash" -- <your-paths>'
                % (verb_label, shown_count, shown, more)
            )
        else:
            deny_em = (
                "BLOCKED: '%s' discards %d uncommitted file(s) git cannot "
                "recover — no commit, stash, or reflog holds them:\n%s%s\n\n"
                "Did you mean to scope it?\n"
                "  git checkout -- <your-paths>\n"
                "  git restore -- <your-paths>\n\n"
                "Or preserve everything first instead:\n"
                '  git stash push -u -m "before-revert" -- <paths>'
                % (verb_label, shown_count, shown, more)
            )
        deny_em += "\n\n%s" % _gr_note if _gr_note else ""
        return _alt_record(
            "deny", "destructive-git-revert", deny_reason, deny_members, _REVERT_ENV,
            em_text=deny_em, **_REVERT_KW,
        ), None

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too. The rescan
    # can itself return either envelope kind (it calls this same function) --
    # a deny still wins outright and returns immediately; an advisory only
    # fills `pending_advisory` if a segment above did not already produce
    # one, preserving deny-over-advisory precedence across both loops.
    #
    # This is first-found-wins, NOT the
    # size-ranked "most destructive advisory wins" rule the segment loop
    # above applies within a single scan pass (`len(affected)` compared
    # across segments). That ranking is deliberately scoped to one scan
    # pass only: threading `len(affected)` out of the recursive call here
    # would require widening `_check_destructive_git_revert_full`'s 2-tuple
    # return contract, which three call sites depend on (this recursive
    # call, and the two guard entry points below) -- including
    # `dispatch.py`'s per-dispatch-call memoisation closure and the
    # call-graph resolver in `test_override_route_inventory.py`, which
    # specifically pattern-matches this function's `[0]`/`[1]` subscript
    # shape. Not forced here; escalated instead (see review-integrator
    # report). Deny precedence is unaffected either way -- a deny from the
    # rescan still returns immediately, unconditionally, above.
    for payload in _shell_c_unwrap_payloads(cmd):
        deny_result, advisory_result = _check_destructive_git_revert_full(
            payload, session_id, hook_payload, git_root
        )
        if deny_result is not None:
            return deny_result, None
        if pending_advisory is None:
            pending_advisory = advisory_result
    return None, pending_advisory


def check_destructive_git_revert(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Hard-deny leg only -- registered in `dispatch.py`'s CONFINEMENT_DENY
    band. Never returns the advisory half; see
    `check_destructive_git_revert_advisory` and
    `_check_destructive_git_revert_full`'s docstring for why the two are
    split (Review: staff-eng, Finding 0)."""
    deny, _advisory = _check_destructive_git_revert_full(cmd, session_id, payload, git_root)
    return deny


def check_destructive_git_revert_advisory(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Advisory-only leg -- registered in `dispatch.py`'s ADVISORY_REWRITE
    band, after every CONFINEMENT_DENY hard-deny guard and before
    `offer-git-c`'s rewrite, so it can never shadow a hard deny.
    `dispatch.py`'s `_build_guard_chain` memoizes `_check_destructive_git_
    revert_full`'s result per dispatch call, so calling both this and
    `check_destructive_git_revert` in the same pass never re-spawns the
    underlying `git status`/`git rev-parse` calls."""
    _deny, advisory = _check_destructive_git_revert_full(cmd, session_id, payload, git_root)
    return advisory
