"""coordinator_core.bash_guards._dc_rewrites -- BX-16 rewrite/advisory band, multiprobe banner shim and check_runaway_find.

Folded: guard family 12-18 of the
``dispatch_checks`` split: the BX-16 rewrite/advisory band
(``check_find_exec_rewrite``, ``check_grep_via_bash_rewrite``,
``check_sed_range_read_advise``, ``check_cat_heredoc_write_advise``,
``check_heredoc_repo_write_advise``) and the multiprobe banner shim
(``check_multiprobe_banner_rewrite``), with their grep-dialect and
heredoc-target helpers.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.

Folded: guard family 7 of the
``dispatch_checks`` split: ``check_runaway_find`` (block-runaway-find.sh) and
its leading env-var / wrapper-word strip helpers.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any, Dict, List, Optional
from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._dc_git_probe import _deny
from coordinator_core.bash_guards._dc_shell_text import (
    _HEREDOC_INTRO_RE,
    _bt_has_redirection,
    _classify_heredoc_intro,
    _join_backslash_newlines,
    _shell_c_unwrap_payloads,
    _split_segments,
    _strip_q,
    _word_present,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._rewrite_support import (
    _advisory,
    _allow_rewrite,
    _bt_python3_invocation,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _BT_Shape,
    classify_command as _bt_classify_command,
)


# ---------------------------------------------------------------------------
# 12-16. BX-16 -- generalising the offer-git-c rewrite seam from cd-over-git
# to bash-over-op. DoE docs/plans/2026-07-29-windows-viability-stop-the-
# spawn-storms.md, row BX-16.
#
# check_offer_git_c (above) is the template: auto-rewrite when a faithful
# translation is derivable, else deny-with-suggestion. Every check below
# reuses that TWO-tier subset of the template (auto-rewrite / advise-with-
# suggestion) and deliberately drops the THIRD (deny) tier: this row's own
# scope is the rewrite VOCABULARY, not Windows deny policy -- that belongs
# to BX-6/BX-7/BX-8's own platform-gated guards, which consume the targets
# named here for their "did you mean" messages (AC-3/AC-4/AC-5). A check in
# this section therefore only ever returns an auto-rewrite (allow +
# updatedInput), an advisory (allow + additionalContext, command
# unchanged), or None -- never `_deny`.
#
# Detection is tokenizer-based throughout (`_shape_classifier.classify_
# command` for the two measured fork-tax shapes this row translates;
# `_command_tokenizer.tokenize_full_command` directly for the two shapes
# outside that classifier's five, sed-range and cat-heredoc) -- never regex
# over raw command text, per the plan's "Never regex for shape detection"
# rule. `_has_banner_marker`-style single-token scans are not needed here
# since none of these five shapes hinge on free-text pattern matching.
# ---------------------------------------------------------------------------


def check_find_exec_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 1 (flagship) -- `find ... -exec <binary> {} ;` forks ONE
    CHILD PROCESS PER MATCH, which is the exact mechanism behind the
    founding incident's 879-process stall on Windows (DoE
    ``state/plan-sidecars/2026-07-28-bash-tax-negative-space.md``).

    Two shapes reach this check. The `-exec` family is parsed by
    `_bt_parse_find_exec_segment`, which requires a literal `-exec` token.
    The `for f in $(find ...); do <binary> "$f"; done` sibling carries no
    such token and is parsed separately by `_bt_parse_for_loop_find` (C5)
    -- ONE canonical shape, not a general loop-body parser: general
    command-substitution and loop-body rewriting stays
    `guard_grep_via_bash._substitutable_rewrite`'s job, and this does not
    duplicate it.

    Auto-rewrites to a single `python3 -c` process (zero per-match forks)
    when the exec'd verb is translatable (rm/cat/wc -l); otherwise advises
    with a generic os.walk skeleton rather than guessing at an arbitrary
    binary's semantics. Never denies -- see this section's module comment.

    shell-doc-ok: the spelling above is the real bash command shape this
    check matches; re-rendering it would leave the docstring unable to name
    what it detects.
    """
    from coordinator_core.bash_guards.find_exec_rewrite import (
        check_find_exec_rewrite as _impl,
    )

    return _impl(cmd, session_id, payload, git_root)


#: grep-family short flags this rewrite considers "substitutable residue"
#: (BX-16 body's own phrase) -- a plain recursive/case-insensitive/count/
#: filenames-only/line-numbered/extended-regex/fixed-string search. A long
#: option or an unrecognized short flag (context lines, invert-match, etc.)
#: falls through to `None` -- that residue is BX-6's to police, not this
#: row's to guess a translation for.
#:
#: `E`/`F` (uppercase ONLY, matching real grep's own spelling of
#: `-E`/`-F`) are recognized here only to steer dialect selection below
#: (`_bt_grep_dialect`) -- they do not themselves change the generated
#: Python source the way `i`/`l`/`c` do. Deliberately NOT lowercase `e`/`f`:
#: real grep's own lowercase `-e PATTERN` (specify a pattern operand) and
#: `-f FILE` (read patterns from a file) are unrelated flags that TAKE AN
#: ARGUMENT -- treating them as dialect flags here would silently consume a
#: real grep operand as if it were a bare boolean switch. Membership is
#: checked case-sensitively BEFORE `ch.lower()` folds the recognized
#: uppercase `E`/`F` into the lowercase `"e"`/`"f"` dialect-flag entries
#: `_bt_grep_dialect` reads.
#:
#: Review: code-reviewer (Finding 4) -- `w` (whole-word match) is
#: deliberately NOT in this set. Neither `_bt_grep_python_rewrite` nor
#: `_bt_build_generator_lines`'s `"grep"` kind ever wraps the translated
#: pattern in a word-boundary (`\b...\b`), so accepting `-w` here while
#: silently dropping its semantics would make `grep -w foo` over-match
#: (`foobar` too) with no error signal -- exactly the "confident wrong
#: answer" class this row exists to refuse rather than guess at. Per this
#: file's own convention (refuse rather than guess), an unrecognized `-w`
#: now falls through to the advisory/refuse path like any other
#: untranslated flag instead of being silently accepted and dropped.
_GREP_SUBSTITUTABLE_SHORT_FLAGS = frozenset({"r", "R", "n", "i", "l", "c", "E", "F"})

#: A POSIX bracket-expression class/collating-symbol/equivalence-class
#: (`[[:alpha:]]`, `[.ch.]`, `[=a=]`) has no Python `re` equivalent at all --
#: any pattern containing one is refused outright, in every dialect.
_GREP_POSIX_BRACKET_CLASS_RE = re.compile(r"\[[:.=][a-zA-Z]*[:.=]\]")

#: Backslash-escapes that mean the identical literal character in BOTH a
#: POSIX ERE (egrep/`grep -E`/ripgrep's default dialect) and Python `re`.
#: Any OTHER escape (`\+`, `\d`, `\<`, ...) is dialect-ambiguous or a
#: Perl/Python-only shorthand absent from POSIX ERE -- refused, not guessed.
_GREP_EXTENDED_SAFE_ESCAPES = frozenset(".*^$[](){}?+|\\")

#: Same idea for POSIX BRE (bare `grep`, no `-E`/`-F`) -- BRE's own
#: metacharacter set is narrower (`+ ? ( ) { } |` are GNU *extensions* whose
#: escaped-vs-bare meaning flips relative to Python `re`; see
#: `_bt_grep_pattern_safe_for_dialect`'s own `dialect == "basic"` branch,
#: which refuses bare occurrences of that set directly rather than via this
#: escape whitelist).
_GREP_BASIC_SAFE_ESCAPES = frozenset(".*^$[]\\")


def _bt_grep_dialect(binary_token: str, flags: set) -> Optional[str]:
    """Classify a grep-family invocation's REGEX DIALECT -- "fixed"
    (`-F`/`fgrep`: no metacharacters at all), "extended" (`-E`/`egrep`, and
    ripgrep's own default -- Rust regex is ERE/Perl-like, not POSIX BRE), or
    "basic" (bare `grep`, POSIX BRE, the founding-incident dialect: `\\|`
    `\\+` `\\?` `\\{ \\}` are GNU-extension operators, and bare
    `| + ? { }` are ordinary literal characters -- the OPPOSITE of Python
    `re`, which is why compiling a BRE pattern as-is silently inverted this
    dispatch's own regression case, see the module-level negative-spec note
    on `check_grep_via_bash_rewrite`).

    Returns `None` for the `-E -F`/`-F -E` combination -- real grep lets the
    LAST flag on the command line win, and this function has no ordering
    information (`flags` is an unordered set), so guessing which one "wins"
    would be exactly the guess this row's whole convention refuses to make.
    """
    has_f = "f" in flags
    has_e = "e" in flags
    if has_f and has_e:
        return None
    if has_f or _bt_token_matches_binary(binary_token, "fgrep"):
        return "fixed"
    if has_e or _bt_token_matches_binary(binary_token, "egrep"):
        return "extended"
    if _bt_token_matches_binary(binary_token, "rg"):
        return "extended"
    return "basic"


def _bt_grep_pattern_safe_for_dialect(pattern: str, dialect: str, safe_escapes: frozenset) -> bool:
    """Bracket-aware scan over `pattern`: `True` only if every construct in
    it means the IDENTICAL thing under `dialect` and under Python `re`.
    Refuses (returns `False`) rather than guesses at anything ambiguous:

    - a POSIX bracket-expression class anywhere in the pattern (checked by
      the caller via `_GREP_POSIX_BRACKET_CLASS_RE` before this scan even
      starts -- not re-checked here);
    - a backslash appearing INSIDE a `[...]` bracket expression -- POSIX
      bracket expressions have no escape mechanism at all (backslash is an
      ordinary bracket member), while Python `re` treats it as an escape
      inside a character class too (`[\\.]` means two different things);
    - an escape of any character not in `safe_escapes` (a dialect-specific
      GNU/Perl shorthand like `\\d`, `\\w`, `\\<`, or a BRE/ERE-extension
      operator whose escaped-vs-bare meaning is dialect-dependent);
    - (basic dialect only) a BARE occurrence of `+ ? ( ) { } |` outside a
      bracket expression -- literal in BRE, a metacharacter in Python `re`;
    - (basic dialect only) a BARE `^` anywhere except the pattern's very
      first character, or a bare `$` anywhere except its very last
      character -- Review: code-reviewer (Finding 5) -- POSIX BRE treats
      `^`/`$` as anchors ONLY at those positions and as ordinary literal
      characters everywhere else, while Python `re` treats a bare `^`/`$`
      as an anchor UNCONDITIONALLY regardless of position. `grep -n 'a^b'`/
      `grep -n 'a$b'` (BRE: the literal substrings `a^b`/`a$b`) used to be
      classified "safe" and compiled unchanged under Python `re`, producing
      a pattern that can never match anything -- the identical silent-
      dialect-conflation failure this function already refuses for BRE
      alternation, just on the anchor-literalness axis instead.
    """
    in_bracket = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if in_bracket:
            if c == "\\":
                return False
            if c == "]":
                in_bracket = False
            i += 1
            continue
        if c == "\\":
            if i + 1 >= n or pattern[i + 1] not in safe_escapes:
                return False
            i += 2
            continue
        if dialect == "basic":
            if c in "+?(){}|":
                return False
            if c == "^" and i != 0:
                return False
            if c == "$" and i != n - 1:
                return False
        if c == "[":
            in_bracket = True
        i += 1
    return True


def _bt_grep_translate_pattern(pattern: str, dialect: str) -> Optional[str]:
    """Return a Python `re`-equivalent SOURCE STRING for `pattern` under
    `dialect`, or `None` if no translation is provably faithful -- this
    module's "refuse rather than guess" convention (see the `find -exec`
    unknown-verb case, above), applied to the regex-dialect axis instead of
    the exec'd-verb axis.

    `dialect == "fixed"` (`-F`/`fgrep`) is unconditionally translatable via
    `re.escape` -- there are no metacharacters to misinterpret when the
    ENTIRE pattern is a literal string, which is exactly what makes this the
    one dialect this function never has to refuse.
    """
    if dialect == "fixed":
        return re.escape(pattern)
    if _GREP_POSIX_BRACKET_CLASS_RE.search(pattern):
        return None
    safe_escapes = _GREP_EXTENDED_SAFE_ESCAPES if dialect == "extended" else _GREP_BASIC_SAFE_ESCAPES
    if not _bt_grep_pattern_safe_for_dialect(pattern, dialect, safe_escapes):
        return None
    return pattern


def _bt_grep_flags_and_operands(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """`tokens` is one already-tokenized segment whose first token is a
    grep-family binary. Returns
    ``{"flags": {...short flag chars...}, "pattern": str, "targets": [...]}``,
    or `None` if a long option (`--foo`) or an unrecognized short flag makes
    this non-substitutable, OR if the pattern's own regex dialect
    (`_bt_grep_dialect`) cannot be PROVABLY translated into the Python `re`
    syntax the generated rewrite compiles it with (`_bt_grep_translate_
    pattern`) -- the returned `"pattern"` is already the translated Python
    `re` source, not the raw operand, so every caller of this function
    (`_bt_grep_python_rewrite`, `_bt_build_generator_lines`'s `"grep"` kind)
    gets dialect-safety for free without needing its own translation step.

    Negative-spec: this function used to hand the RAW grep-family pattern
    straight through to `json.dumps(...)` in both callers, which then
    compiled it as Python `re` unconditionally -- silently WRONG for the
    default (POSIX BRE) dialect, where `\\|`/`\\+`/`\\?`/`\\{ \\}` are
    operators and bare `| + ? { }` are literals, the OPPOSITE of Python
    `re`. Hit live: `grep -n "^| AC-3 \\|^| AC-4 \\|^| AC-5 " <file>`
    (BRE alternation) silently became a single-character-class-free
    catch-some-lines-none pattern under Python `re`'s reading and matched
    every line of a 4000-line file instead of six.

    Also declines (returns `None`) on a shell redirection operator
    (`_bt_has_redirection`) among the operand tokens -- see that helper's
    own docstring for the live incident this guards against (a redirection
    token silently taken as a grep target/pattern instead of being
    recognized as shell syntax). Shared by both callers of this function
    (`check_grep_via_bash_rewrite`'s standalone-grep rewrite and
    `_bt_build_generator_lines`'s `"grep"` kind via `check_head_tail_
    plumbing_rewrite`), so both get the same protection from one fix.
    """
    if _bt_has_redirection(tokens[1:]):
        return None
    flags: set = set()
    operands: List[str] = []
    for tok in tokens[1:]:
        if tok.startswith("--"):
            return None
        if tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch not in _GREP_SUBSTITUTABLE_SHORT_FLAGS:
                    return None
                flags.add(ch.lower())
            continue
        operands.append(tok)
    if not operands:
        return None
    pattern, targets = operands[0], operands[1:]
    dialect = _bt_grep_dialect(tokens[0], flags)
    if dialect is None:
        return None
    translated = _bt_grep_translate_pattern(pattern, dialect)
    if translated is None:
        return None
    return {"flags": flags, "pattern": translated, "targets": targets or ["."]}


def _bt_grep_python_rewrite(parsed: Dict[str, Any]) -> str:
    """Translate a parsed grep-family invocation into a single `python3 -c`
    `os.walk`/`re` one-liner. Honors `-i` (case-insensitive), `-l`
    (filenames only), `-c` (count only); the default output shape mirrors
    `grep -n` (`path:lineno:line`)."""
    flags = parsed["flags"]
    body_lines = [
        "import os, re",
        "pat = re.compile(%s%s)"
        % (json.dumps(parsed["pattern"]), ", re.IGNORECASE" if "i" in flags else ""),
        "targets = %s" % json.dumps(parsed["targets"]),
        "for base in targets:",
        "    walk = os.walk(base) if os.path.isdir(base) else "
        '[(os.path.dirname(base) or ".", [], [os.path.basename(base)])]',
        "    for root, dirs, files in walk:",
        "        for fn in files:",
        "            p = os.path.join(root, fn)",
        "            try:",
        '                with open(p, encoding="utf-8", errors="replace") as fh:',
        "                    lines = fh.readlines()",
        "            except OSError:",
        "                continue",
        "            hits = [(i + 1, ln) for i, ln in enumerate(lines) if pat.search(ln)]",
        "            if not hits:",
        "                continue",
    ]
    if "l" in flags:
        body_lines.append("            print(p)")
    elif "c" in flags:
        body_lines.append('            print(p + ":" + str(len(hits)))')
    else:
        body_lines.append("            for lineno, ln in hits:")
        body_lines.append(
            '                print(p + ":" + str(lineno) + ":" + ln.rstrip())'
        )
    return "%s -c %s" % (_bt_python3_invocation(), shlex.quote("\n".join(body_lines)))


_GREP_FAMILY_BINARIES_BT = ("grep", "egrep", "fgrep", "rg")


def check_grep_via_bash_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 2 -- `grep`-via-Bash (50.9% of measured forks, the
    single largest shape). Auto-rewrites a "substitutable residue" grep
    invocation (see `_GREP_SUBSTITUTABLE_SHORT_FLAGS`) into a single
    `python3 -c os.walk`/`re` process; falls through to `None` for anything
    piped, composed, or carrying an unrecognized flag -- BX-6 owns the
    Windows deny policy for THAT residue, this row only owns the rewrite
    target. Never denies.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_GREP_VIA_BASH", payload=payload):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if not classification.has_shape(_BT_Shape.GREP_VIA_BASH):
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    if len(segments) != 1:
        return None  # a composed/piped command is left to BX-6, not rewritten here
    tokens, pipe_before = segments[0]
    if pipe_before or not tokens:
        return None  # `... | grep ...` -- the upstream command already did work
    if not any(_bt_token_matches_binary(tokens[0], b) for b in _GREP_FAMILY_BINARIES_BT):
        return None
    parsed = _bt_grep_flags_and_operands(tokens)
    if parsed is None:
        return None
    _grep_note = operator_override_note(
        "COORDINATOR_ALLOW_GREP_VIA_BASH", payload=payload, git_root=git_root
    )
    return _allow_rewrite(
        _bt_grep_python_rewrite(parsed),
        # Portability claim, not a fork-count one: GNU and BSD grep disagree
        # on where `^` binds inside an anchored alternation (e.g.
        # `grep -E '^status:|^deployment_state:'`), so the same pattern can
        # match a different line set depending on host grep dialect. That is
        # exactly the case regex_translate.py's compile_spec refuses to
        # translate ("regex dialect not provably translatable"), so it is
        # also exactly the case that still reaches this rewrite in practice
        # once the in-process answerer has already shadowed everything it
        # can safely answer. The python3 rewrite evaluates the pattern with
        # Python's own `re` engine, so behavior is identical on every host.
        (
            "Auto-rewritten: 'grep' via Bash spawns a process, and GNU/BSD "
            "grep disagree on anchored-alternation matches. The python3 "
            "rewrite uses Python's own regex engine, identical on every host."
        )
        + (" %s" % _grep_note if _grep_note else ""),
    )


def check_sed_range_read_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 3 -- `sed -n 'A,Bp' FILE` reads a line range via a
    subprocess; the Read tool does the identical paged read natively, no
    subprocess. Advisory only -- a Bash-to-Bash `updatedInput` cannot name a
    harness tool call, so this can only ever suggest, never rewrite.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_SED_RANGE", payload=payload):
        return None
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(tokens)
    if len(segments) != 1:
        return None
    seg_tokens, pipe_before = segments[0]
    if pipe_before or not seg_tokens:
        return None
    if not _bt_token_matches_binary(seg_tokens[0], "sed"):
        return None
    rest = [t for t in seg_tokens[1:] if t != "-n"]
    if "-n" not in seg_tokens[1:] or not rest:
        return None
    script = rest[0]
    file_arg = rest[1] if len(rest) > 1 else None
    parts = script.split(",")
    if len(parts) != 2:
        return None
    start_s, tail = parts[0], parts[1]
    if not (tail.endswith("p") and start_s.isdigit() and tail[:-1].isdigit()):
        return None
    start, end = int(start_s), int(tail[:-1])
    if end < start:
        return None
    _sed_note = operator_override_note(
        "COORDINATOR_ALLOW_SED_RANGE", payload=payload, git_root=git_root
    )
    return _advisory(
        (
            "Advisory: 'sed -n \"%s\"' reads a line range via a subprocess -- "
            "the Read tool does the same paged read natively: Read(%s, "
            "offset=%d, limit=%d)."
            % (script, file_arg or "<file>", start, end - start + 1)
        )
        + (" %s" % _sed_note if _sed_note else "")
    )


def _heredoc_target_outside_any_session_repo(
    target: str, payload: Optional[Dict[str, Any]]
) -> bool:
    """True when ``target`` has no enclosing git repo, or its enclosing repo
    is the user's home directory (a dotfiles-style repo no session commits
    to). Pure filesystem walk, no git spawn; fails open (False) on any error
    so the advisory still fires."""
    try:
        t = os.path.expanduser(target)
        if not t or any(c in t for c in "$`*?"):
            return False
        cwd = (payload or {}).get("cwd") if isinstance(payload, dict) else None
        base = cwd if isinstance(cwd, str) and cwd else os.getcwd()
        path = os.path.realpath(t if os.path.isabs(t) else os.path.join(base, t))
        home = os.path.realpath(os.path.expanduser("~"))
        cur = os.path.dirname(path)
        while True:
            if os.path.exists(os.path.join(cur, ".git")):
                return cur == home
            parent = os.path.dirname(cur)
            if parent == cur:
                return True
            cur = parent
    except Exception:  # noqa: BLE001 -- fail open: keep the advisory
        return False


def check_cat_heredoc_write_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 4 -- `cat > FILE <<'EOF' ... EOF` writes a file via a
    subprocess heredoc. Advisory only, for the same reason as the sed check
    above -- but the fact it states is a recording one, not a tool
    preference: since 2026-08-30 this write is recorded and claimed for the
    session's commit the same as any other (DR-258 § Amendment 2026-08-30),
    so the message names what stays true regardless of which tool wrote the
    file (the redirect target is what's recorded; a path the command does
    not name is not), mirroring `check_heredoc_repo_write_advise`'s register
    rather than nudging toward the Write tool.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_CAT_HEREDOC", payload=payload):
        return None
    if "<<" not in cmd or not _word_present(r"\bcat\b", cmd):
        return None
    intro, _, _ = cmd.partition("<<")
    tokens = _bt_tokenize_full_command(intro)
    if not tokens:
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(tokens)
    if not segments:
        return None
    seg_tokens, pipe_before = segments[-1]
    if pipe_before or not seg_tokens:
        return None
    if not _bt_token_matches_binary(seg_tokens[0], "cat"):
        return None
    redir = ">" if ">" in seg_tokens else (">>" if ">>" in seg_tokens else None)
    if redir is None:
        return None
    redir_idx = seg_tokens.index(redir)
    if redir_idx + 1 >= len(seg_tokens):
        return None
    target = seg_tokens[redir_idx + 1]
    if _heredoc_target_outside_any_session_repo(target, payload):
        return None
    _cat_note = operator_override_note(
        "COORDINATOR_ALLOW_CAT_HEREDOC", payload=payload, git_root=git_root
    )
    return _advisory(
        (
            "Advisory: this heredoc writes `%s`. This write is recorded; if "
            "the path is inside this session's repo it will be in this "
            "session's commit (DR-258 § Amendment 2026-08-30); a path this "
            "command does not name is not."
            % target
        )
        + (" %s" % _cat_note if _cat_note else "")
    )


#: A literal-string open()/Path(...).write_text()/write_bytes() call inside a
#: scriptable-interpreter heredoc body (the same "scriptable" classification
#: `check_cat_heredoc_write_advise`'s neighbours use -- see
#: `_HEREDOC_SCRIPTABLE_INTERPRETERS`). Three shapes, each captured with the
#: (quote, path, quote) group at a fixed position so a caller can reject any
#: match whose path group contains an f-string/format placeholder:
#:   1. ``open(<path>, <mode>)`` where mode starts with w/a/x (not a bare 'r').
#:   2. ``pathlib.Path(<path>).write_text(``/``.write_bytes(``
#:   3. ``Path(<path>).write_text(``/``.write_bytes(`` (the un-prefixed import
#:      shape, e.g. ``from pathlib import Path``).
#: A quote prefixed by an f/F/rb/br/etc. string-prefix letter is excluded at
#: the call site (checked via the character immediately before the opening
#: quote), not in the regex itself -- Python's string-prefix alphabet is
#: small but not fixed-width, and a lookbehind can't express "zero or more
#: prefix letters, but not zero-plus-anything-else" cleanly. Same for a path
#: containing ``{`` (an f-string placeholder that slipped through because the
#: opening quote itself wasn't prefixed, e.g. a `.format()`-style literal
#: reused as an f-string body) -- rejected post-match, not in-regex.
_HEREDOC_WRITE_TARGET_RE = re.compile(
    r"""
    (?:
        \bopen\s*\(\s*
        (?P<q1>['"])(?P<path1>[^'"]*)(?P=q1)
        \s*,\s*
        (?P<q1m>['"])(?P<mode1>[a-zA-Z+]+)(?P=q1m)
    )
    |
    (?:
        (?:pathlib\.)?Path\s*\(\s*
        (?P<q2>['"])(?P<path2>[^'"]*)(?P=q2)
        \s*\)\s*\.\s*write_(?:text|bytes)\s*\(
    )
    """,
    re.VERBOSE,
)

#: Scratch/temp roots a heredoc write into is the CORRECT use of a heredoc
#: (ephemeral working files, never repo content) -- never advised. Mirrors
#: the scratchpad shape this session's own harness uses
#: (``…/AppData/Local/Temp/claude/…``), plus the bare OS temp dirs.
_HEREDOC_SCRATCH_ROOT_MARKERS = ("/appdata/local/temp/claude/",)


def _heredoc_write_target_is_scratch(abs_path: str) -> bool:
    """True if `abs_path` (already absolute, forward-slash-normalized by the
    caller) resolves under a scratch/temp root: the coordinator scratchpad
    shape, or the live ``$TEMP``/``$TMP``/``%TEMP%``/``%TMP%`` env var's own
    directory. Env vars are read fresh per call (never cached at module
    scope) for the same reason `_override` reads fresh -- a test that
    monkeypatches ``os.environ`` must see it take effect on the very next
    call, not a stale import-time snapshot.

    Deliberately does NOT treat a bare ``/tmp`` literal as scratch on its
    own: this function's sole production caller
    (`check_heredoc_repo_write_advise`) only reaches it AFTER confirming
    `candidate` already resolves inside `git_root` via `_paths_match_prefix`
    -- so a bare-/tmp check here was never distinguishing "scratch write
    outside the repo" from "the repo checkout itself lives under /tmp"
    (a real pytest `tmp_path` fixture, or a real cloud-container checkout),
    and always misfired in the latter case, permanently silencing a real
    in-repo write advisory. TF-20260923-bb-081."""
    norm = abs_path.replace("\\", "/").lower()
    if any(marker in norm for marker in _HEREDOC_SCRATCH_ROOT_MARKERS):
        return True
    for env_var in ("TEMP", "TMP"):
        val = os.environ.get(env_var)
        if not val:
            continue
        val_norm = os.path.normcase(os.path.normpath(val))
        if _paths_match_prefix(val_norm, abs_path):
            return True
    return False


def _heredoc_write_target_is_claude_memory(abs_path: str) -> bool:
    """True if `abs_path` is a file under ``<claude-config>/projects/<slug>/memory/``
    -- Claude auto-memory, never part of a session's commit. The config dir is
    ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``, read fresh per call; matching is
    pure string arithmetic, separator- and case-insensitive."""
    target = abs_path.replace("\\", "/").lower()
    roots = [os.environ.get("CLAUDE_CONFIG_DIR"), os.path.join(os.path.expanduser("~"), ".claude")]
    for root in roots:
        if not root:
            continue
        root_n = os.path.normpath(root).replace("\\", "/").lower().rstrip("/")
        prefix = root_n + "/projects/"
        if target.startswith(prefix):
            parts = target[len(prefix):].split("/")
            if len(parts) >= 3 and parts[1] == "memory":
                return True
    return False


def _paths_match_prefix(root: str, target: str) -> bool:
    """True if normalized `target` is `root` itself or lives underneath it --
    pure `os.path` string arithmetic, no `stat`/`samefile` (the target of a
    write-in-progress heredoc usually does not exist on disk yet, so
    `os.path.samefile` would raise for the common case). Case-insensitive
    (`os.path.normcase`) so this behaves identically on Windows, where the
    guard's own repo (`claude-klabauter`, "Windows is first-class" per this
    repo's CLAUDE.md) actually runs."""
    root_n = os.path.normcase(os.path.normpath(root))
    target_n = os.path.normcase(os.path.normpath(target))
    return target_n == root_n or target_n.startswith(root_n + os.sep)


def _heredoc_scriptable_bodies(cmd: str) -> List[List[str]]:
    """Yield the RAW body-line lists of every heredoc in `cmd` whose intro
    classifies as `"scriptable"` (`_classify_heredoc_intro`) -- the identical
    classification walk `_strip_heredoc_bodies_for_prose_scan` performs, but
    collecting bodies for a write-pattern content scan instead of a
    strip/keep decision. Does not itself decide spawn-indicator visibility
    (`_heredoc_body_has_spawn_indicator` is a DIFFERENT question -- whether
    the body might shell out -- orthogonal to whether it writes a file).
    Fails closed by yielding nothing on an unterminated heredoc, matching
    every sibling heredoc walker in this module."""
    lines = cmd.split("\n")
    out: List[List[str]] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    hd_class = "unknown"
    pending: List[str] = []
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
                pending = []
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
                if hd_class == "scriptable" and pending:
                    out.append(pending)
                pending = []
            elif hd_class == "scriptable":
                pending.append(line)
    return out


def check_heredoc_repo_write_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16-family sibling of `check_cat_heredoc_write_advise`, for the
    write a scriptable-interpreter heredoc performs (`python3 - <<'PY' ...
    PY`, a generator, a formatter). The PostToolUse touch-tracker
    (`coordinator_core/hooks/track_touched_files.py::_handler`, matcher
    `Write|Edit|MultiEdit|NotebookEdit` ONLY -- a ratified permanent limit,
    DR-258, NOT touched or widened by this guard) still never sees that
    write. What it no longer implies is a dropped file: since 2026-08-30
    `bash_guards.write_claim_record.record_write_claims` appends a claim from
    THIS SAME PreToolUse call for every path the command literally names, so
    `safe-commit-offer` carries them (DR-258 section "Amendment 2026-08-30").
    The residue this guard's message still names is the path a heredoc
    COMPUTES at runtime and never spells out -- unclaimable by construction,
    and deliberately so.

    Advisory only, and silence-biased: it states what is and is not recorded
    for the path it resolved, and stays silent on anything it cannot resolve
    to a literal repo-relative path rather than risk a false positive on
    every python heredoc.

    Detection is pure path arithmetic on `git_root` (handed in by the
    caller, never derived here) -- NO subprocess, NO `git status`/`git
    ls-files`. **What production actually hands in is the payload's `cwd`**,
    not a resolved git root: this module never resolves one (F0 -- see
    `dispatch`'s module docstring), so the containment test is "under the
    invoking cwd," and a repo-relative write from a subdirectory resolves
    against that subdirectory. That is narrower than "inside the repo" and
    the difference is a SILENCE, never a false advisory, which is the bias
    this guard is built around. `git_root` empty/None means "cannot classify," which fails
    CLOSED to silence (the mirror image of every hard-deny in this module
    failing closed to DENY): a false silence here costs nothing but a missed
    advisory; a false advisory would be noise on every unrelated heredoc.

    Known-uncovered sibling case, NOT this guard's job: a PowerShell
    here-string (`@'...'@`) has the identical invisibility to the touch
    tracker, but `matchers=("Bash",)` (deliberate, see this function's
    `GuardEntry` registration) makes a Bash-matched guard inert under the
    PowerShell tool. PowerShell here-string syntax is different enough
    (`@'`/`'@` delimiters, no interpreter-intro line to classify) that
    covering it is separate work, not a gap in this guard's own logic.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_HEREDOC_REPO_WRITE", payload=payload):
        return None
    if not git_root:
        return None
    if "<<" not in cmd:
        return None
    bodies = _heredoc_scriptable_bodies(cmd)
    if not bodies:
        return None
    git_root_abs = os.path.normpath(git_root)
    for body_lines in bodies:
        body = "\n".join(body_lines)
        for m in _HEREDOC_WRITE_TARGET_RE.finditer(body):
            if m.group("path1") is not None:
                path = m.group("path1")
                mode = m.group("mode1")
                if not any(c in mode for c in ("w", "a", "x")):
                    continue  # read-only mode ('r', 'rb') -- not a write
                q_idx = m.start("q1")
            else:
                path = m.group("path2")
                q_idx = m.start("q2")
            if not path:
                continue
            if "{" in path or "%" in path:
                continue  # likely an interpolation placeholder, not a literal
            # Reject an f-string/raw/byte-prefixed quote -- the character(s)
            # immediately before the opening quote, if they look like a
            # Python string-prefix letter run, mean `path` is a template,
            # not the literal text it appears to be.
            pre = body[:q_idx]
            pre_word = re.search(r"([A-Za-z]{1,2})$", pre)
            if pre_word and set(pre_word.group(1).lower()) <= set("frbu"):
                continue
            target = path.replace("\\", "/")
            if os.path.isabs(target) or re.match(r"^[A-Za-z]:/", target):
                candidate = os.path.normpath(target)
            else:
                candidate = os.path.normpath(os.path.join(git_root_abs, target))
            if not _paths_match_prefix(git_root_abs, candidate):
                continue  # outside git_root -- stay silent
            rel = os.path.relpath(candidate, git_root_abs).replace("\\", "/")
            if rel == ".git" or rel.startswith(".git/"):
                continue
            if _heredoc_write_target_is_scratch(candidate):
                continue
            if _heredoc_write_target_is_claude_memory(candidate):
                continue
            _hd_note = operator_override_note(
                "COORDINATOR_ALLOW_HEREDOC_REPO_WRITE", payload=payload, git_root=git_root
            )
            return _advisory(
                (
                    "Advisory: this heredoc writes `%s`, a path inside this repo. "
                    "This write is recorded and will be in this session's commit "
                    "(DR-258 § Amendment 2026-08-30); a path this command does not "
                    "name is not."
                    % rel
                )
                + (" %s" % _hd_note if _hd_note else "")
            )
    return None


# ---------------------------------------------------------------------------
# 17-18. BX-7/BX-8's missing rewrite targets -- closing the two-shape gap in
# BX-16's vocabulary. DoE docs/plans/2026-07-29-windows-viability-stop-the-
# spawn-storms.md, row BX-16 (this dispatch's own remit): `_shape_classifier
# .Shape` names FIVE measured fork-tax shapes, and two of them (MULTI_PROBE_
# BANNER at 40.1% of forks, HEAD_TAIL_PLUMBING at 25%) had no rewrite target
# in this section until now -- BX-7 and BX-8 name exactly this gap as the
# reason they are not yet unblocked.
#
# Same three-tier-minus-one shape as every check above: auto-rewrite
# (allow + updatedInput) when every stage of the command is one of this
# function's recognized, translatable forms; advise (allow +
# additionalContext, command unchanged) when any stage is not; NEVER deny --
# see this section's own module comment above (checks 12-16) for why: this
# row owns the rewrite VOCABULARY, Windows deny policy belongs to BX-7/BX-8's
# own platform-gated guards, which consume the targets these two checks name.
# ---------------------------------------------------------------------------

def check_multiprobe_banner_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 6: rewrite a multi-probe session-facts banner chain to one
    `python3 -c` process, or None. Body lives in `multiprobe_banner_rewrite`."""
    from coordinator_core.bash_guards.multiprobe_banner_rewrite import (
        check_multiprobe_banner_rewrite as _impl,
    )

    return _impl(cmd, session_id, payload, git_root)


# ---------------------------------------------------------------------------
# 7. check_runaway_find -- block-runaway-find.sh
# ---------------------------------------------------------------------------

def _find_is_root_anchor(tok: str) -> bool:
    t = _strip_q(tok)
    if re.match(r"^/+$", t):
        return True
    if re.match(r"^[A-Za-z]:[/\\]*$", t):
        return True
    s = t
    while s.endswith("/") and len(s) > 1:
        s = s[:-1]
    if re.match(r"^/[A-Za-z]$", s):
        return True
    if re.match(r"^/mnt/[A-Za-z]$", s):
        return True
    if re.match(r"^/cygdrive/[A-Za-z]$", s):
        return True
    # Bare home-directory anchor -- `find ~ -name x` / `find ~/ -name x`
    # (the trailing-slash strip loop above already reduces `~/` to `~`) is a
    # whole-home-directory scan, the identical runaway shape this guard
    # exists to catch for `/`. A DEEPER anchor (`~/subdir`) is deliberately
    # NOT matched here -- only the bare home token is "the whole tree", same
    # asymmetry as `/mnt/<X>` (root of one drive) vs `/mnt/<X>/subdir` (not
    # caught).
    #
    # NOTE: `$HOME` is deliberately NOT handled here, and never will be --
    # `check_runaway_find`'s own caller-side loop already bails (never
    # guesses) on ANY token containing `$` before this function is even
    # reached (the `if any(c in stripped for c in ("$", "*", "?", "[",
    # "`")): break` line), because `$HOME` is a caller-environment variable
    # this guard cannot resolve without expanding it itself -- and `$HOME`
    # can be legitimately overridden to something that is NOT the real home
    # directory. Adding a literal `"$HOME"` string match here would be dead
    # code (unreachable past that earlier bail) AND would contradict this
    # file's own established never-guess-at-expansion precedent for
    # `$`-prefixed tokens.
    if s == "~":
        return True
    return False


_FIND_WRAPPER_WORDS = {"sudo", "command", "time", "env", "nice", "nohup", "exec", "timeout", "stdbuf"}

_ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_WRAPPER_WORD_RE = re.compile(r"(" + "|".join(_FIND_WRAPPER_WORDS) + r")\s+")

#: `nice`'s own niceness argument, sitting between `nice` and the real
#: command it wraps -- the bare-numeric form (`nice -19 <cmd>`), the
#: separate-token `-n`/`--adjustment` form (`nice -n 19 <cmd>`), and the
#: attached-value short-flag form (`nice -n19 <cmd>`). Mirrors
#: `dispatch_checks.py`'s own `_BYPASS_WRAPPER_ARG_FLAGS`/`_skip_wrapper_
#: own_argv` and `_sentinel_creation_guard.py`'s `_NICE_BARE_NUMERIC_RE` --
#: same argument shape, same wrapper, ported to this function's index-based
#: (not token-list-based) walk. Consumed as a WHOLE match including its
#: trailing whitespace, symmetric with how `_WRAPPER_WORD_RE` itself
#: consumes the wrapper word's own trailing whitespace.
_NICE_NICENESS_ARG_RE = re.compile(r"(?:-n\s+\d+|--adjustment\s+\d+|-n\d+|-\d+)\s+")


def _skip_leading_env_and_wrappers_idx(buf: str, i: int) -> int:
    """Advance index `i` into `buf`, past leading whitespace, environment-
    variable assignment tokens (`FOO=bar`), and wrapper words
    (`_FIND_WRAPPER_WORDS`), repeating until nothing more strips. Index-based
    so callers that need a position into the ORIGINAL buffer (not a copied
    suffix) -- e.g. `_offer_awk_parse`'s quote-aware scan -- can use it
    directly. `_strip_leading_env_and_wrappers` is the string-returning
    wrapper for callers that only need the stripped remainder.

    Single shared implementation for what used to be two independently-
    drifting copies of this strip-loop (`_find_is_find_segment` and
    `check_offer_git_c`'s `cd`/`git` detection) -- the divergence between
    those two copies is exactly how a leading `FOO=1 cd ... && git ...`
    evaded `check_offer_git_c` while an equivalent prefix was already
    handled by `_find_is_find_segment`. Don't grow a third copy.
    """
    n = len(buf)
    prev = -1
    while i != prev:
        prev = i
        while i < n and buf[i] in " \t":
            i += 1
        # BRACE-GROUPING FIX (2026-07-29, EM-run second-pass integration,
        # confirmed live): `{ find / -delete; }` was never peeled here,
        # same class as `_seg_has_git_bypass_flag`'s brace-peel fix above --
        # bash requires a space after `{` (a reserved word), so it always
        # surfaces as its own leading token/character run.
        if i < n and buf[i] == "{" and (i + 1 >= n or buf[i + 1] in (" ", "\t")):
            i += 1
            continue
        # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
        # confirmed live): `( find / -delete )` was never peeled here either
        # -- same class as the brace fix directly above, `(` always
        # surfaces as its own leading token/character run when
        # whitespace-separated (the tested shape), and this function had no
        # tolerance for it.
        if i < n and buf[i] == "(" and (i + 1 >= n or buf[i + 1] in (" ", "\t")):
            i += 1
            continue
        m = _ENV_ASSIGN_RE.match(buf, i)
        if m:
            after = m.end()
            if after < n and buf[after] in (" ", "\t"):
                i = after
                continue
        m2 = _WRAPPER_WORD_RE.match(buf, i)
        if m2:
            i = m2.end()
            # NICE-BARE-NUMERIC FIX (2026-07-29, EM-run confinement-corpus
            # pass, confirmed live -- the Staff Engineer staff-eng review Finding 3):
            # `nice -19 find / -delete` was never recognized as wrapping a
            # `find` invocation -- `_WRAPPER_WORD_RE` only ever consumed the
            # bare word `nice` (plus its trailing whitespace), leaving
            # `-19` sitting at what this function treats as command
            # position; `_find_is_find_segment`'s `^find` anchor then never
            # matched, and the whole-disk `find` sailed through unguarded.
            # This is the single highest-value cell in the corpus: reaching
            # for `nice` is exactly the load-conscious behaviour this guard
            # exists to reward, not punish. Consume nice's own niceness
            # argument here, immediately after its wrapper word, the same
            # way `_skip_wrapper_own_argv` does for the tokenized guards
            # elsewhere in this file.
            if m2.group(1) == "nice":
                m3 = _NICE_NICENESS_ARG_RE.match(buf, i)
                if m3:
                    i = m3.end()
            continue
    return i


def _strip_leading_env_and_wrappers(seg: str) -> str:
    """String-returning wrapper over `_skip_leading_env_and_wrappers_idx` --
    the stripped remainder is always a literal suffix of `seg` (every step
    only drops a prefix), so `seg[len(seg) - len(result):]` recovers the
    exact consumed prefix text when a caller needs it verbatim."""
    return seg[_skip_leading_env_and_wrappers_idx(seg, 0):]


def _find_is_find_segment(seg: str) -> bool:
    s = _strip_leading_env_and_wrappers(seg)
    return bool(re.match(r"^(/\S*/)?find(\s|$)", s))


def check_runaway_find(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)

    if not _word_present(r"\bfind\b", cmd):
        return None

    find_override = _override("COORDINATOR_ALLOW_FIND_ROOT", payload=payload)

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        if not _find_is_find_segment(seg):
            continue

        toks = seg.split()
        anchor_tok = ""
        seen_find = False
        skip_next = False
        for tok in toks:
            if skip_next:
                skip_next = False
                continue
            if not seen_find:
                if tok == "find" or tok.endswith("/find"):
                    seen_find = True
                continue
            if tok in ("-H", "-L", "-P"):
                continue
            if tok == "-D":
                skip_next = True
                continue
            if tok.startswith("-O"):
                continue
            if tok.startswith("-") or tok in ("(", "!", ","):
                break
            stripped = _strip_q(tok)
            if any(c in stripped for c in ("$", "*", "?", "[", "`")):
                break
            if _find_is_root_anchor(tok):
                anchor_tok = stripped
            break

        if anchor_tok:
            if find_override:
                continue
            return _deny(
                "BLOCKED: 'find' anchored at '%s' scans the whole disk — "
                "hangs for tens of CPU-minutes and orphans on exit.\n\n"
                "Use instead:\n"
                "  find . -maxdepth 3 -name '<pattern>'" % anchor_tok
            )

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    #: Same shadowing hazard as `check_destructive_rm`'s unwrap loop -- see the
    #: note there. The loop variable is a command string; the guard payload is
    #: this function's own parameter and is forwarded explicitly (C14c).
    for unwrapped_cmd in _shell_c_unwrap_payloads(cmd):
        result = check_runaway_find(unwrapped_cmd, session_id, payload=payload)
        if result is not None:
            return result
    return None
