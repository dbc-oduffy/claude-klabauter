"""Shell-text helpers shared by every dispatch_checks guard family: segment splitting,
quote and heredoc stripping, `sh -c` unwrapping, prose-scan comment stripping and
redirection-token predicates.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    _BUNDLED_C_FLAG_RE,
    _extract_command_substitutions as _bt_extract_command_substitutions,
    exceeds_tokenizable_ceiling as _bt_exceeds_tokenizable_ceiling,
    segments_from_tokens_simple as _bt_segments_from_tokens_simple,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._rewrite_support import (
    _BT_REDIRECTION_TOKEN_RE,
    _bt_has_redirection,
    _bt_is_redirection_token,
)


def _join_backslash_newlines(cmd: str) -> str:
    """Join `\\<NL>` line-continuations into a single space (mirrors
    `CMD="${CMD//\\$NL/ }"`)."""
    return cmd.replace("\\\n", " ")


_SHELL_C_WRAPPER_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_MAX_SHELL_C_UNWRAP_DEPTH = 4

def _shell_c_unwrap_single_segment(tokens: List[str]) -> Optional[str]:
    """The single-segment scan `_shell_c_unwrap_payloads` used to run
    against the WHOLE command text (pre-cd-prefix-bypass-fix): find a
    wrapper interpreter (optionally `env`-/assignment-prefixed) at the head
    of `tokens` and return its `-c` payload, or `None` if `tokens` is not a
    wrapper invocation. `tokens` is one caller-supplied segment's token
    list -- this function does no splitting or tokenizing of its own."""
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    if i < len(tokens) and os.path.basename(tokens[i].replace("\\", "/")) == "env":
        i += 1
        while i < len(tokens) and (
            tokens[i] in ("-i", "--ignore-environment")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
        ):
            i += 1
    if i >= len(tokens):
        return None
    head = os.path.basename(tokens[i].replace("\\", "/"))
    head = re.sub(r"\.exe$", "", head, flags=re.IGNORECASE)
    if head not in _SHELL_C_WRAPPER_INTERPRETERS:
        return None
    rest = tokens[i + 1 :]
    c_flag_positions = [j for j in range(len(rest)) if _BUNDLED_C_FLAG_RE.match(rest[j])]
    if not c_flag_positions:
        return None
    idx = c_flag_positions[0]
    if idx + 1 >= len(rest):
        return None
    return rest[idx + 1]


def _shell_c_unwrap_payloads(cmd: str, depth: int = 0) -> List[str]:
    """Return the nested command-text payload(s) of any `sh -c`/`bash -c`/
    `zsh -c`/`dash -c`/`ksh -c` invocation in `cmd` (optionally `env`-/
    assignment-prefixed), so a caller can re-run ITS OWN matcher against the
    ACTUAL executed text too. Fails safe: returns `[]` (nothing extra to
    scan, never a crash or a false deny) on unparseable input or nesting
    past `_MAX_SHELL_C_UNWRAP_DEPTH`.

    Bug fix (2026-08-17, cd-prefix unwrap gap): the wrapper was previously
    looked for ONLY at token position 0 of the whole `cmd` text, on the
    premise that multi-segment chaining ahead of the wrapper was "already
    caught by each caller's own segment loop before this helper is ever
    consulted" -- that premise was false. None of the six call sites
    (`check_no_verify`, `check_destructive_git_orphan`, `check_destructive_
    rm`, `_check_destructive_git_revert_full`, `check_blanket_git_add`,
    `check_runaway_find`) segments `cmd` before calling this helper; each
    hands it the raw command text once. `cd /tmp && sh -c '<payload>'`
    therefore tokenized to `['cd', '/tmp', '&&', 'sh', '-c', '<payload>']`,
    and the position-0 check saw `cd`, never `sh` -- a live bypass of every
    one of those six checks, confirmed via direct probe.

    Now segments `cmd` itself via the same quote-aware tokenizer/segmenter
    every other guard in this package shares (`tokenize_full_command` +
    `segments_from_tokens_simple`, `_command_tokenizer.py`) and looks for
    the wrapper at the head of EACH segment, not just the first. This is
    strictly additive: a bare `sh -c '...'` (no leading segment) still
    matches on its own now-single segment exactly as before, and a quoted
    `;`/`&`/`|` (`echo "a && b"`) is never treated as a segment boundary,
    because the shared tokenizer already respects quoting -- unlike this
    function's PRIOR `shlex.split(cmd, posix=True)` call, which had no
    `punctuation_chars` set and so never split on separators at all
    (relying entirely on position 0 instead). A payload found in ANY
    segment is unwrapped and recursively re-scanned, same as before.

    Bug fix (2026-08-17, command-substitution unwrap gap): `echo $(sh -c
    '<payload>')` and the backtick equivalent still bypassed every caller --
    the shared tokenizer has no `$(...)`-aware grouping (a deliberate scope
    boundary, not fixed here: it is load-bearing for 5+ other guard
    modules), so a wrapper hidden inside a command substitution was never a
    real segment head and never reached `_shell_c_unwrap_single_segment`.
    Fixed WITHOUT touching the shared tokenizer's grouping: the existing
    segment loop below still runs on `cmd` completely unmodified (see its
    own comment for why -- a wrapper's `-c` payload can legitimately CONTAIN
    a `$(...)` a downstream caller still needs verbatim), and a SEPARATE
    pass runs this package's own `_extract_command_substitutions`
    (`_command_tokenizer.py`, already used by `resolve_command_positions`
    for the identical reason) over that same `cmd` to pull out every
    unquoted top-level `$(...)`/backtick span, quote-aware -- `echo
    '$(...)'` (single-quoted, literal data) yields no substitutions at all,
    while `echo "$(...)"` (double-quoted) still yields one, matching real
    shell semantics where only single quotes suppress substitution. Each
    extracted inner command text is recursed into via this SAME function
    (one extra `depth`), so a wrapper nested inside a substitution is
    unwrapped exactly like a wrapper nested inside another wrapper's payload
    already was -- same `_MAX_SHELL_C_UNWRAP_DEPTH` bound, and nested
    `$(...)` spans (paren-balanced by `_extract_command_substitutions`)
    unwrap one level per recursion rather than all at once. Each `subs`
    entry is a strictly-smaller substring of `cmd`, but NOT necessarily
    disjoint from the segment loop's own view of `cmd`: a double-quoted
    wrapper `-c` payload that itself contains a `$(...)` gets that inner
    substitution discovered TWICE -- once when the segment loop's own
    `_shell_c_unwrap_single_segment` recurses into the whole `-c` payload
    text (which still contains the `$(...)` verbatim, per the comment
    below), and again here when the substitution walk finds the same
    `$(...)` independently in `cmd`. This is harmless (bounded by
    `_MAX_SHELL_C_UNWRAP_DEPTH`, and it makes this function OVER-scan a
    payload, never under-scan one) but it is real double-work, not a
    partition -- see the fuller note further down where this was found and
    the comment corrected (n04, this file).
    """
    if depth > _MAX_SHELL_C_UNWRAP_DEPTH:
        return []
    if _bt_exceeds_tokenizable_ceiling(cmd):
        # DoS bound inherited from `_command_tokenizer`. NOT redundant with
        # `tokenize_full_command`'s own internal ceiling check below now
        # that `_extract_command_substitutions` also runs over `cmd` before
        # this function returns: that helper is a manual scan with no
        # ceiling of its own, so this explicit gate is what stops it (and
        # not just the tokenizer call) from ever seeing an over-ceiling `cmd`.
        return []
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return []
    payloads: List[str] = []
    for seg_tokens in _bt_segments_from_tokens_simple(tokens):
        payload = _shell_c_unwrap_single_segment(seg_tokens)
        if payload is None:
            continue
        payloads.append(payload)
        payloads.extend(_shell_c_unwrap_payloads(payload, depth + 1))
    # Command substitutions: scanned on the UNTOUCHED `cmd`, never on a
    # neutralized copy fed to the segment loop above. An earlier version of
    # this fix tokenized a neutralized `cmd` (each `$(...)`/backtick span
    # collapsed to one space) for BOTH the segment loop and the substitution
    # walk, on the theory that this kept the two scans a disjoint partition
    # of `cmd`. That neutralization is wrong for THIS loop specifically: a
    # wrapper's own `-c` payload can legitimately CONTAIN a `$(...)` a
    # downstream caller still needs to see verbatim (`/bin/sh -c "git reset
    # --hard $(echo HEAD~3)"` -- `check_destructive_git_orphan`'s own CHECK 1
    # denies a subshell-resolved reset target on sight, but only if the
    # unwrapped payload it re-scans still HAS the `$(...)` in it; neutralizing
    # it first silently downgraded the target to the "no bare token found"
    # default of `HEAD`, turning a hard deny into a false allow). So the
    # segment loop above always sees `cmd` verbatim, preserving every
    # wrapper payload's real text unmodified.
    #
    # The substitution walk below is a SEPARATE, ADDITIONAL pass over that
    # same `cmd` text, not a second consumer of the segment loop's already-
    # tokenized `tokens` -- needed because `tokenize_full_command` has no
    # `$(...)`-aware grouping (a shared-tokenizer scope boundary, not fixed
    # here), so a BARE, unquoted `$(sh -c '...')` glues `$(` onto the
    # wrapper's own name (`'$(sh'` as one token) and the segment loop above
    # can never see `sh` at a segment head to begin with -- confirmed via
    # `tokenize_full_command("echo $(sh -c '...')")` returning a single
    # `['echo', '$(sh', '-c', "...)"]` segment, head `echo`, no wrapper
    # match. `_extract_command_substitutions` is quote-aware exactly like
    # the segment loop's own tokenizer (single-quoted `$(...)` is literal
    # data and yields no substitution; double-quoted and bare `$(...)` both
    # do, matching real shell semantics), so each extracted inner command
    # text is recursed into via this SAME function at `depth + 1`. This
    # scans `cmd`'s bytes a second time at THIS level (once for segments,
    # once for substitutions) -- a bounded constant-factor duplication of
    # the CURRENT level's work, not a per-level multiplier. Each `subs`
    # entry is a strictly-smaller substring of `cmd`, but NOT provably
    # disjoint from what the segment loop above already recursed into: a
    # double-quoted wrapper `-c` payload containing its own `$(...)` is
    # recursed into once by the segment loop (the whole payload, `$(...)`
    # and all -- see the comment above on why it is left verbatim) and
    # again here (the `$(...)` alone, found independently by this
    # substitution walk). Confirmed live (2026-08, n04): harmless -- still
    # bounded by `_MAX_SHELL_C_UNWRAP_DEPTH`, and the duplication makes this
    # function OVER-scan such a payload, never under-scan one -- but it is
    # real overlapping work, not a clean partition, so total work across the
    # bounded recursion is linear in `cmd`'s length times the depth bound
    # with a bounded constant-factor overhead, never exponential.
    _, subs = _bt_extract_command_substitutions(cmd)
    for sub in subs:
        payloads.extend(_shell_c_unwrap_payloads(sub, depth + 1))
    return payloads


def _strip_q(t: str) -> str:
    """Strip ONE layer of surrounding single/double quotes (mirrors
    `_rm_strip_q`/`_orphan_strip_q`/`_find_strip_q`)."""
    if len(t) >= 2:
        if t[0] == '"' and t[-1] == '"':
            return t[1:-1]
        if t[0] == "'" and t[-1] == "'":
            return t[1:-1]
    # bash impl strips independently from each end (asymmetric quote pairs
    # tolerated) -- replicate that rather than requiring symmetric quotes.
    if t.endswith('"'):
        t = t[:-1]
    if t.startswith('"'):
        t = t[1:]
    if t.endswith("'"):
        t = t[:-1]
    if t.startswith("'"):
        t = t[1:]
    return t


def _split_segments(cmd: str) -> List[str]:
    """Split on runs of `;`, `&`, `|` (mirrors `sed -E 's/[;&|]+/\\n/g'`)."""
    return re.split(r"[;&|]+", cmd)


def _strip_ws_quoted_spans(s: str) -> str:
    """Port of `_orphan_strip_ws_quoted_spans` -- delete whitespace-containing
    quoted spans, single pass, own-quote pairing (no open/close ambiguity)."""
    out: List[str] = []
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if c in ("\"", "'"):
            q = c
            j = i + 1
            span_start = j
            found = False
            while j < n:
                if s[j] == q:
                    found = True
                    break
                j += 1
            if found:
                span = s[span_start:j]
                if not re.search(r"[ \t\n]", span):
                    out.append(q + span + q)
                i = j + 1
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


_HEREDOC_INTRO_RE = re.compile(r"<<-?('[^']*'|\"[^\"]*\"|[A-Za-z0-9_.+-]+)")


def _strip_heredoc_bodies(cmd: str) -> str:
    """Port of the awk heredoc-body-stripping state machine shared verbatim
    across block-destructive-rm.sh / -git-clean.sh / -git-revert.sh. Fail-safe:
    on an unterminated heredoc, returns ``cmd`` UNCHANGED (mirrors the bash
    `[[ rc -eq 0 ]] && CMD="$STRIPPED"` discard-on-failure)."""
    lines = cmd.split("\n")
    out_lines: List[str] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
            out_lines.append(line)
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
            # suppressed (heredoc body line) -- not appended
    if in_hd:
        # Unterminated heredoc -- fail-safe, discard the strip.
        return cmd
    return "\n".join(out_lines)


_HEREDOC_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "eval"})

# Interpreters that CAN spawn a subprocess (and so could shell out to a real
# `git push --force`/`git branch -D`) but usually don't -- a heredoc body fed
# to one of these is only prose-stripped if it shows NO sign of doing so (see
# `_heredoc_body_has_spawn_indicator`). Found via the 2026-07-2x coordinator
# probe (probe_guard.py cases D/E): a `python3 - <<'PY'` / `node <<'JS'` body
# that calls `subprocess.run([...])` / `execSync(...)` was being blanket-
# stripped as prose alongside the genuinely-prose case (a python heredoc that
# only WRITES a doc discussing force-pushing) -- both were "not a shell
# interpreter" under the original binary classification. This repo's own
# bash-to-Python porting campaign makes "Python heredoc that shells out to
# git" a growing shape, not a curiosity -- it needs its own middle tier.
_HEREDOC_SCRIPTABLE_INTERPRETERS = frozenset({
    "python", "python3", "python2", "perl", "ruby", "node", "nodejs", "php",
})

# Conservative substring scan (not a single monolithic regex) over a
# scriptable-interpreter heredoc body -- any hit means the body might spawn a
# subprocess, so it stays VISIBLE to CHECK 2/3 rather than being stripped as
# prose. Matched case-insensitively against the whole body. Covers the
# process-spawning constructs of the languages in
# `_HEREDOC_SCRIPTABLE_INTERPRETERS`: Python (subprocess, os.system,
# os.popen, commands.), Perl/Ruby (system(, exec(, backtick command
# substitution, qx(/qx{/qx/, %x(/%x{, Open3), Node (child_process, exec(,
# execSync, spawn(, spawnSync), PHP (system(, exec(, shell_exec(,
# proc_open(, backticks). A body with none of these tokens is prose (writes
# a file, prints text, etc.) and is stripped exactly like a non-scriptable
# prose heredoc.
_SPAWN_INDICATOR_TOKENS = (
    "subprocess", "os.system", "os.popen", "commands.", "shell_exec(",
    "proc_open(", "system(", "exec(", "execsync", "spawnsync", "spawn(",
    "child_process", "qx(", "qx{", "qx/", "open3", "%x(", "%x{",
)

#: Backtick is a spawn indicator in exactly the languages where it MEANS
#: command substitution. Perl/Ruby/PHP execute `` `cmd` ``; Python and Node
#: do not -- a backtick there is a syntax error, never a subprocess. Keeping
#: it unconditional made a markdown code span inside a Python heredoc's
#: string literal (`` `git reset --hard` `` written as documentation) read as
#: a spawn, holding the whole body visible to CHECK 1/2/3, which then denied
#: the prose. Negative spec: this narrows the indicator by INTERPRETER, never
#: by body content -- every Perl/Ruby/PHP backtick deny is retained.
#: Reported by coordinator-content-repo, cross-repo memo
#: `2026-08-19-coordinator-content-repo-em-check1-hazard-prose-false-positive-reproduced-and-bounded.md`.
_BACKTICK_SUBSTITUTION_INTERPRETERS = frozenset({"perl", "ruby", "php"})


def _heredoc_body_has_spawn_indicator(
    body_lines: List[str], interpreter: Optional[str] = None
) -> bool:
    """True if `body_lines` (a scriptable-interpreter heredoc body) contains
    any token from `_SPAWN_INDICATOR_TOKENS`, case-insensitively. A single
    substring scan over the joined body is intentionally coarse (a
    conservative token scan, not a language parser) -- false positives (a
    body that merely MENTIONS "subprocess" in a comment/string) only cost a
    body staying visible to CHECK 2/3's existing regex scan, which is
    itself tolerant of prose (see CHECK 1's oracle-verification precedent);
    they can never cause a false ALLOW.

    `interpreter` is the resolved command word of the introducing line (see
    `_heredoc_intro_command_word`). A backtick counts as a spawn indicator
    only for `_BACKTICK_SUBSTITUTION_INTERPRETERS`; an unknown/absent
    interpreter fails CLOSED and keeps counting it."""
    text = "\n".join(body_lines).lower()
    if any(tok in text for tok in _SPAWN_INDICATOR_TOKENS):
        return True
    if interpreter is None or interpreter not in _HEREDOC_SCRIPTABLE_INTERPRETERS:
        return "`" in text
    return interpreter in _BACKTICK_SUBSTITUTION_INTERPRETERS and "`" in text


def _line_has_shell_in_command_position(line: str) -> bool:
    """True if ANY segment of `line` (split on `;`/`&`/`|`, mirroring
    `_split_segments`) has a shell interpreter (bash/sh/zsh/dash/ksh, or
    `eval`) as its leading command word (after skipping wrapper/env
    tokens). Covers a shell invoked ANYWHERE on the heredoc-introducing
    line, not just as the line's own first word -- `cat <<'EOF' | bash`,
    `sudo bash <<'EOF'`, `foo && bash <<'EOF'` all hit this. (2026-07-2x
    coordinator probe case G: a heredoc piped into `bash` after a `cat`
    prefix is not prose -- the pipe target executes it.)

    Fails closed PER SEGMENT: an unparseable segment, or one with no
    resolvable command word, counts as a hit for THAT segment (so the
    overall scan returns True) -- an ambiguous line is never classified as
    definitively non-shell.
    """
    for seg in _split_segments(line):
        seg = seg.strip()
        if not seg:
            continue
        if _bt_exceeds_tokenizable_ceiling(seg):
            # DoS bound inherited from `_command_tokenizer`, not a local
            # tuning knob -- same fail-closed `True` an unparseable segment
            # already takes.
            return True
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            return True
        if tokens and all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) for t in tokens):
            # A bare assignment segment ahead of a cat heredoc runs nothing and
            # cannot be the heredoc's consumer. A command substitution in its
            # value is still caught: `_split_segments` does not split inside
            # it, and CHECK 2/3 scan the visible line regardless.
            continue
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in _RM_WRAPPER_WORDS:
                i += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
                i += 1
                continue
            break
        if i >= len(tokens):
            return True
        cmd_tok = os.path.basename(tokens[i])
        if cmd_tok in _HEREDOC_SHELL_INTERPRETERS:
            return True
    return False


def _classify_heredoc_intro(prefix: str, full_line: str = "") -> str:
    """Classifies a heredoc by what its introducing line (the text UP TO the
    ``<<`` operator) invokes. Returns one of:

      - ``"shell"`` -- a shell interpreter (bash/sh/zsh/dash/ksh, or `eval`)
        that will itself EXECUTE the heredoc body as further commands (e.g.
        ``bash <<'EOF' ... EOF``). ALWAYS visible to CHECK 2/3 -- a real
        forcing push/branch-delete written inside it actually runs.
      - ``"scriptable"`` -- an interpreter that CAN spawn a subprocess but
        usually doesn't (python/python3/python2/perl/ruby/node/nodejs/php).
        Visible to CHECK 2/3 only if the body itself contains a spawn
        indicator (see `_heredoc_body_has_spawn_indicator`); otherwise
        treated as prose and stripped.
      - ``"prose"`` -- a RECOGNIZABLE command word that is neither a shell
        nor a scriptable interpreter (e.g. `git`, `cat`, `tee`) -- the
        heredoc body is data for that command (a commit message, a file's
        contents), not something that gets interpreted/executed. Always
        treated as prose and stripped -- this is the `git commit -F -
        <<'MSG'` / `cat > file <<'EOF'` case.
      - ``"unknown"`` -- an unparseable prefix, or a prefix with NO
        resolvable command word (e.g. bare `<<'EOF'`, or only wrapper/env
        tokens) -- genuinely ambiguous, not a recognized non-executing
        command. Fails CLOSED to ALWAYS visible, identical to `"shell"` --
        this can only widen what stays visible, never narrow it into a
        false ALLOW.
    """
    if _line_has_shell_in_command_position(full_line if full_line else prefix):
        return "shell"
    cmd_tok = _heredoc_intro_command_word(prefix)
    if cmd_tok is None:
        return "unknown"
    if cmd_tok in _HEREDOC_SHELL_INTERPRETERS:
        return "shell"
    if cmd_tok in _HEREDOC_SCRIPTABLE_INTERPRETERS:
        return "scriptable"
    return "prose"


def _heredoc_intro_command_word(prefix: str) -> Optional[str]:
    """Resolve the command word a heredoc-introducing `prefix` invokes (its
    basename, wrapper words and `VAR=` assignments skipped), or `None` when
    that cannot be determined -- an unparseable prefix, one over the
    tokenizable ceiling, or one with no command word at all.

    Extracted from `_classify_heredoc_intro` so the resolved interpreter is
    available to `_heredoc_body_has_spawn_indicator`'s backtick arm as well
    as to the classification itself. `None` is the fail-CLOSED value at both
    call sites: `"unknown"` classification, and backtick-counts-as-spawn."""
    if _bt_exceeds_tokenizable_ceiling(prefix):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `None` is the documented always-visible fail-closed
        # answer an unparseable prefix already lands on.
        return None
    try:
        tokens = shlex.split(prefix, posix=True)
    except ValueError:
        return None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _RM_WRAPPER_WORDS:
            i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    return os.path.basename(tokens[i])


_QUOTE_SENTINEL_SQ = ""
_QUOTE_SENTINEL_DQ = ""


def _protect_line_quotes(line: str) -> str:
    """Replace `'`/`"` with Private-Use-Area sentinel chars so a downstream
    whole-string pass (`_strip_ws_quoted_spans`) cannot quote-pair across
    this line and delete a multi-word quoted span it contains (e.g. a
    Python/Node source string literal like `'git push origin main
    --force'`). Reversed by `_restore_protected_quotes` immediately after
    that pass runs. Only applied to heredoc-body lines CHECK 2/3 must keep
    visible (see `_strip_heredoc_bodies_for_prose_scan`) -- never to prose
    lines, which are discarded outright, and never to any other check in
    this file (`_strip_ws_quoted_spans` has exactly one call site)."""
    return line.replace("'", _QUOTE_SENTINEL_SQ).replace('"', _QUOTE_SENTINEL_DQ)


def _restore_protected_quotes(s: str) -> str:
    return s.replace(_QUOTE_SENTINEL_SQ, "'").replace(_QUOTE_SENTINEL_DQ, '"')


def _strip_shell_comments_for_prose_scan(cmd: str) -> str:
    """Drop shell `#` comments from `cmd` before
    `check_destructive_git_orphan` pattern-matches it, so prose in a comment
    can never trip a check that is supposed to match what the command DOES.
    Same contract as `_seg_excluding_freetext_operands`, one layer out:
    a comment is text the shell never executes, so removing it can only
    NARROW what CHECK 1/2/3 see, never widen it into a false ALLOW.

    Negative spec: this is NOT a heredoc fix. The reported case
    (`python -c "print(1)"  # the doc mentions `` `git reset --hard` ``)
    carries no heredoc at all -- CHECK 1 builds `after` as everything
    following the first `reset` token, comments included, so a markdown code
    span in a trailing comment read as a command substitution.
    `_seg_resolved_git_subcommand`'s docstring already names the mirror-image
    hazard (`git push origin main --force # git stash push` suppressing
    CHECK 2 via a comment); stripping comments strengthens that walk rather
    than competing with it.

    A `#` opens a comment only at word start (line start or after
    whitespace) and outside quoting -- `${x#y}`, `foo#bar` and a `#` inside
    a quoted span are all left alone. Quote state is tracked per line and
    over the Private-Use-Area sentinels `_protect_line_quotes` substitutes,
    since kept heredoc-body lines reach this function still protected.

    Fails closed per line, never open: a line whose quoting does not close
    (unterminated quote, or one over the tokenizable ceiling) is returned
    UNCHANGED, so the caller's raw regex scan runs exactly as before --
    still over-blocking, never under-blocking.
    """
    single = ("'", _QUOTE_SENTINEL_SQ)
    double = ('"', _QUOTE_SENTINEL_DQ)
    out: List[str] = []
    for line in cmd.split("\n"):
        if "#" not in line:
            out.append(line)
            continue
        if _bt_exceeds_tokenizable_ceiling(line):
            # DoS bound inherited from `_command_tokenizer` -- same
            # unchanged-line fail-closed branch an unterminated quote takes.
            out.append(line)
            continue
        in_sq = in_dq = False
        cut = None
        prev_ws = True
        for idx, ch in enumerate(line):
            if ch == "\\" and not in_sq:
                prev_ws = False
                continue
            if ch in single and not in_dq:
                in_sq = not in_sq
            elif ch in double and not in_sq:
                in_dq = not in_dq
            elif ch == "#" and not in_sq and not in_dq and prev_ws:
                cut = idx
                break
            prev_ws = ch.isspace()
        if in_sq or in_dq:
            out.append(line)
            continue
        out.append(line if cut is None else line[:cut].rstrip())
    return "\n".join(out)


def _strip_heredoc_bodies_for_prose_scan(cmd: str) -> str:
    """Variant of `_strip_heredoc_bodies` used ONLY by
    `check_destructive_git_orphan` (CHECK 2's force-push scan and CHECK 3's
    force-delete-branch scan, which share its "match on what the command
    DOES, never what free text SAYS" contract -- see
    `_seg_excluding_freetext_operands`). Does NOT change the unconditional
    `_strip_heredoc_bodies` behavior already relied on by
    check_destructive_rm / check_destructive_git_clean /
    check_destructive_git_revert.

    A heredoc body is prose (a commit message written via `git commit -F -
    <<'MSG'`, a file authored via `python3 - <<'PY'` that merely WRITES
    text, etc.) and gets stripped UNLESS `_classify_heredoc_intro` says its
    introducing line either (a) invokes a shell interpreter that will
    itself EXECUTE that body as further commands (``"shell"``/``"unknown"``
    -- an unrecognized command word fails closed the same way), or (b)
    invokes a scriptable interpreter (python/perl/ruby/node/php) AND the
    body itself contains a spawn indicator (``"scriptable"`` +
    `_heredoc_body_has_spawn_indicator` -- e.g. a `python3 - <<'PY'` body
    that calls `subprocess.run(['git', 'push', ..., '--force'])`). A
    recognized non-executing consumer (``"prose"`` -- `git`, `cat`, `tee`,
    etc.) is always stripped. Either way a KEPT heredoc's body is left
    UNSTRIPPED (visible to the caller's existing regex scan) whenever there
    is genuine reason to think it might execute a real destructive command.

    Kept body lines are additionally quote-PROTECTED (see
    `_protect_line_quotes`) before being returned: the caller
    (`check_destructive_git_orphan`) runs `_strip_ws_quoted_spans` right
    after this function, and that scanner deletes any whitespace-containing
    quoted span wholesale -- which would otherwise erase a Python/Node
    source-string literal like `'git push origin main --force'` before
    CHECK 2 ever sees it (this was an actual regression: probe case E, a
    `node <<'JS'` body calling `execSync('git push ... --force')`, ALLOWed
    because its one multi-word quoted arg was deleted). The caller restores
    real quotes via `_restore_protected_quotes` after `_strip_ws_quoted_spans`
    runs, so downstream segment-splitting / `_seg_excluding_freetext_operands`
    see ordinary quotes again.

    Fails closed exactly like `_strip_heredoc_bodies`: an unterminated
    heredoc returns `cmd` UNCHANGED (the pre-fix raw scan runs as before --
    still over-blocking, never under-blocking). This can only NARROW what
    CHECK 2/3 see (by removing genuine non-executing prose), never widen it.
    """
    lines = cmd.split("\n")
    out_lines: List[str] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    hd_class = "unknown"
    hd_interp: Optional[str] = None
    pending_body_lines: List[str] = []
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
                hd_class = _classify_heredoc_intro(line[: m.start()], line)
                hd_interp = _heredoc_intro_command_word(line[: m.start()])
                pending_body_lines = []
            out_lines.append(line)
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
                if hd_class == "scriptable":
                    if _heredoc_body_has_spawn_indicator(pending_body_lines, hd_interp):
                        out_lines.extend(_protect_line_quotes(bl) for bl in pending_body_lines)
                        out_lines.append(line)
                    # else: prose -- buffered body lines AND terminator both
                    # suppressed, matching `_strip_heredoc_bodies`'s own
                    # terminator handling.
                elif hd_class == "prose":
                    pass  # buffered body lines AND terminator suppressed
                else:
                    # "shell" or "unknown" -- always visible; body lines
                    # were already appended immediately below (protected)
                    # as they were read, so only the terminator remains.
                    out_lines.append(line)
                pending_body_lines = []
            elif hd_class == "scriptable":
                # Buffer RAW (unprotected) -- decision (strip vs. keep) is
                # deferred until the terminator is found and the WHOLE body
                # can be scanned for a spawn indicator.
                pending_body_lines.append(line)
            elif hd_class == "prose":
                pass  # suppressed immediately -- decision doesn't depend on content
            else:
                # "shell" or "unknown" -- kept, protected immediately.
                out_lines.append(_protect_line_quotes(line))
    if in_hd:
        return cmd
    return "\n".join(out_lines)


#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): a
#: heredoc-fed shell (`bash <<'EOF' ... git commit --no-verify ... EOF`) was
#: never unwrapped by `check_no_verify` -- the function only flattens real
#: newlines to spaces (`flat`, below) and then segment-splits on `;`/`&`/`|`,
#: so the heredoc's own intro/terminator lines collapse into the SAME
#: segment as the surrounding `bash <<'EOF' ... EOF` text, with `bash` (not
#: `git`) sitting at command position -- the body text is invisible to this
#: guard even though it is executed for real once the terminator is read.
#: This is the mirror-image of `_shell_c_unwrap_payloads` (which already
#: unwraps `sh -c '...'`/`bash -c "..."` for this exact guard, added the
#: same day) for the heredoc-fed-shell shape instead of the `-c` shape --
#: reusing the EXISTING heredoc-classification machinery
#: (`_HEREDOC_INTRO_RE`, `_classify_heredoc_intro`,
#: `_heredoc_body_has_spawn_indicator`) that `_strip_heredoc_bodies_for_
#: prose_scan` already relies on, rather than inventing new heredoc parsing.
#: That function DROPS a non-executing heredoc body from a scan; this one is
#: the opposite direction -- it RETURNS an executing heredoc's body FOR the
#: caller to re-scan, exactly as `_shell_c_unwrap_payloads` returns a
#: `sh -c` payload for the caller to re-scan.
#:
#: Runs on `cmd` (real newlines intact), never on `flat` (newlines already
#: collapsed to spaces) -- the heredoc intro/terminator boundary detection
#: needs the actual line structure `flat` has already destroyed.
_MAX_HEREDOC_UNWRAP_DEPTH = 4


def _heredoc_shell_payloads(cmd: str, depth: int = 0) -> List[str]:
    """Return the body-text payload(s) of any heredoc in `cmd` whose
    introducing line will itself EXECUTE that body as further shell/script
    commands: a `"shell"`/`"unknown"`-classified intro (`bash <<'EOF' ...`,
    fails closed identically to `_classify_heredoc_intro`'s own posture), or
    a `"scriptable"`-classified intro (`python3 - <<'PY' ...`) whose body
    contains a spawn indicator (`_heredoc_body_has_spawn_indicator` --
    `subprocess.run([...])` etc.). A `"prose"` heredoc (`git commit -F -
    <<'MSG'`, `cat > file <<'EOF'`) or a spawn-indicator-free scriptable one
    yields nothing -- that body is data, not executed shell text.

    Fails safe: an unterminated heredoc, or nesting past
    `_MAX_HEREDOC_UNWRAP_DEPTH`, yields `[]` (nothing extra to scan, never a
    crash, never a false deny) -- the caller's own scan of the introducing
    line is unaffected either way, and this can only ADD payloads to scan,
    never remove visibility the caller already had.
    """
    if depth > _MAX_HEREDOC_UNWRAP_DEPTH:
        return []
    lines = cmd.split("\n")
    payloads: List[str] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    hd_class = "unknown"
    hd_interp: Optional[str] = None
    pending_body_lines: List[str] = []
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
                hd_class = _classify_heredoc_intro(line[: m.start()], line)
                hd_interp = _heredoc_intro_command_word(line[: m.start()])
                pending_body_lines = []
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
                if hd_class in ("shell", "unknown"):
                    payloads.append("\n".join(pending_body_lines))
                elif hd_class == "scriptable" and _heredoc_body_has_spawn_indicator(
                    pending_body_lines, hd_interp
                ):
                    payloads.append("\n".join(pending_body_lines))
                pending_body_lines = []
            else:
                pending_body_lines.append(line)
    if in_hd:
        return []
    nested: List[str] = []
    for payload in payloads:
        nested.extend(_heredoc_shell_payloads(payload, depth + 1))
    return payloads + nested


_RM_WRAPPER_WORDS = {
    "sudo", "command", "time", "exec", "nice", "nohup", "env",
    "ionice", "timeout", "stdbuf", "which", "type",
}


def _word_present(pattern: str, cmd: str) -> bool:
    """`pattern` found in `cmd` OR in `cmd` with quote characters removed.

    Every guard in this file gates on a cheap raw-text word scan before doing
    real work. The shell resolves `'g''it' stash` to `git stash`, so those
    scans matched nothing and the guards returned before their own (correct,
    quote-aware) analysis ran: measured 2026-08-29, `git stash` refused while
    `'g''it' stash` was ALLOWED, same for git clean/checkout/reset and rm.

    WIDENING ONLY. These gates decide solely whether to LOOK; every guard's
    downstream analysis still resolves segments, verbs and targets for itself,
    so a word that appears only after quote-stripping cannot produce a refusal
    on its own. `_rm_is_rm_segment` already read segments this way -- the gates
    in front of the guards did not."""
    if re.search(pattern, cmd):
        return True
    unquoted = cmd.replace("'", "").replace('"', "")
    return unquoted != cmd and bool(re.search(pattern, unquoted))


def _bt_is_bare_redirection_token(tok: str) -> bool:
    """`True` if `tok` is a redirection operator with nothing glued after
    it (`>`, `>>`, `2>`, `<`, `<<`, `&>`), as opposed to a glued form
    (`>/dev/null`, `2>&1`, `<<'MSG'`) where the target is part of the same
    token. Derived from `_BT_REDIRECTION_TOKEN_RE`'s match LENGTH against
    the token's own length -- not a literal string list -- so a future
    change to that regex stays automatically consistent here (Finding 1,
    2026-08-05 cockpit-incident follow-up review): `_bt_tokenize_full_command`
    uses `punctuation_chars=";&|"`, which excludes `>`/`<`, so a whitespace-
    separated redirection (`> /dev/null`, `2> file`, `< file`) tokenizes as
    TWO tokens -- the bare operator, then its target as a separate plain
    token. A bare operator token consumes nothing of its target itself;
    the caller must additionally skip the NEXT token as the redirect
    destination, or that target token survives into `operands` and reads
    an unscoped `git commit ... > /dev/null` as explicitly scoped."""
    m = _BT_REDIRECTION_TOKEN_RE.match(tok)
    return bool(m) and m.end() == len(tok)
