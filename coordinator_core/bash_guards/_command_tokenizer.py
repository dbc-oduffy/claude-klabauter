"""coordinator_core.bash_guards._command_tokenizer -- single canonical home
for the shlex-based command tokenizer/segmenter shared by the bash-guard
fail-closed detectors (``block_subagent_commit.py``,
``block_subagent_destructive_action.py``, ``block_worktree_creation.py``,
``dispatch_checks.py``, ``_sentinel_creation_guard.py``), plus (2026-07-29,
part 2) the boundary-anchored ``token_matches_binary`` identity matcher now
also shared by ``block_subagent_commit.py`` and
``block_reviewer_bash_outside_allowlist.py`` -- see that function's own
docstring for the ``.exe``-blindness bypass its consolidation closes.

Why this module exists (2026-07-28/29, guard-brick incident response):
before this change, ``_normalize_executable_basename``/
``_tokenize_full_command``/``_segments_from_tokens`` existed in TWO
independently-maintained copies -- one in ``block_subagent_commit.py`` (a
deliberate own-module duplicate, see that file's own historical docstring
on ``_extract_first_token`` -- a concurrent-edit-safety choice made for a
DIFFERENT pair of helpers during that chunk's authoring) and one in
``block_subagent_destructive_action.py``, from which FOUR other guard
modules (``block_worktree_creation.py``, ``dispatch_checks.py``,
``_sentinel_creation_guard.py``, and transitively
``block_approval_sentinel_creation.py``/``block_worktree_sentinel_creation.py``
via ``_sentinel_creation_guard``) already imported directly. Cross-module
import of these three primitives was therefore already the DOMINANT pattern,
not a novel choice this change introduces -- ``block_subagent_commit.py``
was the one outlier still hand-maintaining its own copy, and its copy had
already silently drifted: it never received the 2026-07-28 case-folding fix
(``GIT.EXE``/``BASH.EXE`` normalizing to lowercase), which
``block_subagent_destructive_action.py``'s copy did receive. That drift was
found and closed by this change, not merely documented (see
``normalize_executable_basename``'s docstring below).

A dedicated, minimal, rarely-touched module is a deliberate improvement
over leaving the canonical copy inside ``block_subagent_destructive_action.py``
(2400+ lines, under near-daily active edit): four-plus modules already
depend on these three functions' exact signatures; a small stable module
shrinks the blast radius of "someone edited that file for an unrelated
reason" relative to today, where the same dependents reach into the large
file directly. It does NOT change who may import from whom in general --
this module carries no guard policy of its own, only tokenizer plumbing,
so it cannot itself become a policy-drift surface.

Every existing guard module keeps importing under its OWN prior private
name (each guard file does ``from ._command_tokenizer import
normalize_executable_basename as _normalize_executable_basename`` or
re-exports the public name under the private one at module scope) so no
caller's import line, and no existing test that reaches into a guard
module's namespace for these names, needed to change.

CONTRACT PINNED BY ``coordinator_core/bash_guards/tests/
test_shared_command_tokenizer_contract.py`` -- do not change a return
shape here without updating that test; it exists specifically so a
tuple-arity change (the shape of failure that bricked Bash fleet-wide on
2026-07-28, see the two ``cross-repo/inbox/`` memos on the sentinel-guard
crash) fails loudly in this module's own test tier instead of surfacing as
a live ``ValueError`` inside a fail-closed guard.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Tuple

#: Matches a trailing Windows executable-launcher suffix, case-
#: insensitively. `.exe` was the original scope (identical in both prior
#: copies); `.cmd` was added 2026-07-29 part 2 -- evidence-based, not
#: general Windows knowledge: `coordinator/bin/gen-launcher-shim.py` (this
#: project's OWN Windows-launcher generator, the sole producer of launcher
#: twins for `coordinator-safe-commit` and `coordinator-doc-new`) emits a
#: `.cmd` twin for EVERY bin/ entrypoint unconditionally
#: (`generate()`/`render_cmd()`), confirmed on disk
#: (`coordinator/bin/coordinator-safe-commit.cmd`,
#: `coordinator/bin/coordinator-doc-new.cmd`) -- so `.cmd` is the ordinary,
#: load-bearing Windows invocation form for those two binaries, not a
#: hypothetical one (PATHEXT resolves it, and `CreateProcess` cannot exec
#: their POSIX shebang directly). `.bat` is deliberately NOT included: the
#: generator never emits one for any entrypoint (grep confirms zero `.bat`
#: producers in `coordinator/bin/`), so there is no real carrier to
#: recognize. `.ps1` is also deliberately excluded: the generator emits one
#: only opt-in via `--ps1`, and the project's own tripwire doctrine records
#: this as explicitly NOT the convention ("`.ps1` twins are not the
#: convention (3 of 57 entrypoints carry one)" --
#: `docs/wiki/coordinator-tripwires.md` BIN-ENTRYPOINT-NEEDS-CMD-TWIN) --
#: recognizing it here would be scoping to a hypothetical rather than a
#: confirmed carrier, the same mistake `.bat` would be. `git` itself has no
#: `.cmd`/`.bat` twin (Git for Windows ships `git.exe` only) -- `.exe`
#: already covers it.
_WINDOWS_LAUNCHER_SUFFIX_RE = re.compile(r"\.(?:exe|cmd)$", re.IGNORECASE)

#: Matches a token that is ENTIRELY made of `;`/`&`/`|` characters -- the
#: shlex punctuation-token output for a command separator, never a
#: substring match against a word. Identical in both prior copies.
_SEPARATOR_TOKEN_RE = re.compile(r"^[;&|]+$")

#: The left-hand side of a bash fd-duplication redirection (`>&`, `>>&`,
#: `<&`, and their fd-prefixed spellings `2>&`, `1>&`, `0<&`) as `shlex`
#: emits it BEFORE the `&`: `2>&1` lexes as `['2>', '&', '1']` because `&`
#: is in `punctuation_chars`. See `join_redirection_operator_tokens`.
_REDIRECT_DUP_LHS_RE = re.compile(r"^\d*(?:>>?|<)$")

#: The right-hand side of an fd duplication -- a bare fd number, optionally
#: `-`-suffixed (`2>&1`, `2>&-`), or a bare `-` (close). Anything else after
#: a `>&` is a filename, which `shlex` already keeps as its own token and
#: which needs no re-joining.
_REDIRECT_DUP_RHS_RE = re.compile(r"^(?:\d+-?|-)$")

#: SECURITY PROPERTY -- a denial-of-service bound, NOT a tuning knob. Do not
#: raise this to "let a big command through": raising it re-opens the hang
#: described below, and the hang is on the PreToolUse hot path where it
#: stalls the agent's tool call outright.
#:
#: `shlex` tokenization is QUADRATIC in the length of the longest single
#: token. The driver is `shlex.shlex.read_token`'s `self.token += nextchar`:
#: CPython's in-place string-append optimization fires only when the target
#: string's refcount is 1, and an INSTANCE-ATTRIBUTE target is referenced by
#: both the evaluation stack and `self.__dict__`, so every character copies
#: the entire accumulated token. `punctuation_chars` is incidental, not the
#: cause -- the same curve is present without it. Note this is per-TOKEN, not
#: per-command: `echo a b c; ` repeated to 790 KB tokenizes in 0.11 s, while
#: a single 790 KB quoted argument takes 6.5 s, because a double-quoted
#: string of any length is ONE token.
#:
#: Measured on the guard hot path (2026-08-05, `git commit -m "<one huge
#: argument>"` through `dispatch.evaluate_payload_json`, which tokenizes the
#: same command once per consulting guard):
#:
#:     32 KB ->   0.21 s      128 KB ->  2.60 s
#:     64 KB ->   0.53 s      197 KB ->  4.60 s
#:                            3.2 MB -> ~105 s
#:
#: 64 KiB is the largest power-of-two size at which the WORST-CASE shape
#: (whole command in one token) keeps a full guard dispatch under one second
#: -- 128 KiB is already 2.6 s. It is not a round number picked for
#: tidiness: the next size up fails the budget by 2.6x. Headroom against
#: real commands is ample -- the longest command-shaped string literal
#: anywhere in this package's own test corpus is 8,020 characters, so the
#: ceiling sits ~8x above the largest command this project has ever needed
#: to classify.
#:
#: Over-ceiling input is treated exactly as UNPARSEABLE input already is:
#: `tokenize_full_command` returns `None` and `resolve_command_positions`
#: returns a single `UNRESOLVED` entry. Both are pre-existing FAIL-CLOSED
#: signals every caller in this package already handles -- an over-ceiling
#: command can therefore only ever become MORE restricted, never more
#: permissive. Verified call-site by call-site, not assumed; see
#: `tests/test_command_tokenizer_length_ceiling.py`.
_MAX_TOKENIZABLE_COMMAND_CHARS = 65536

#: Stand-in for an `&` that is SOURCE-ADJACENT to a following `>` (bash's
#: `&>file`/`&>>file` combine-redirect). Private-use codepoint: `shlex`
#: treats it as an ordinary word character, so the redirect survives
#: tokenization as ONE token instead of being split at a punctuation `&`.
#: Every token is un-masked immediately after lexing, so no caller ever
#: observes it. See `_mask_adjacent_ampersand_redirects`.
_AMP_REDIRECT_SENTINEL = ""


def normalize_executable_basename(token: str) -> str:
    """Return `token`'s basename normalized for exact-identity comparison:
    strip trailing `/`/`\\` separator noise (a raw pre-tokenization token can
    carry a trailing separator), split on BOTH `/` and `\\` path separators
    (a Windows path may appear in a command string on any host platform),
    strip any trailing run of dots/spaces (NTFS silently strips these at
    resolution time -- `git.exe.`/`git.exe ` both resolve to `git.exe` on a
    real Windows invocation), strip a trailing `.exe` or `.cmd` suffix
    case-insensitively (see `_WINDOWS_LAUNCHER_SUFFIX_RE`'s own comment for
    why exactly these two and not `.bat`/`.ps1`), then lower-case the
    result -- Windows PATH/
    `cmd.exe` resolution is case-insensitive, so `GIT.EXE`, `Git.exe`,
    `BASH.EXE`, `coordinator-safe-commit.cmd`, etc. are real, executable
    invocations that must resolve to the SAME identity as their bare-name
    spelling. `gitk`, `git-foo`, and `mygit` are unaffected -- this only
    strips a recognized suffix/separators and folds case, it never truncates
    or fuzzy-matches a basename, so this cannot make matching sloppy in the
    allow direction.

    This is the canonical, case-folded form. Before this module existed,
    `block_subagent_commit.py` carried its own copy that never received the
    case-folding step, unlike every other guard's copy/import -- consolidating
    onto this single implementation closes that inconsistency rather than
    picking one behavior silently: case-folding only ever ADDS recognized
    matches for a binary name already checked via a lowercase literal
    comparison at call sites that route their primary identity check through
    THIS function (`block_subagent_destructive_action.py`,
    `block_worktree_creation.py`, `dispatch_checks.py`), so unifying onto the
    case-folded form is a strict tightening there, never a new false-negative.
    `block_subagent_commit.py` was a partial exception at `ecad1682`: it
    only ever called this helper from inside its own
    `_normalize_windows_git_argv0` rewrite condition, not from its primary
    identity check (`_token_matches_binary`, at the time an exact `/git`-
    suffix match with no `.exe`-stripping of its own) -- so case-folding
    here changed whether that module rewrote a backslash path, but did not
    by itself make it recognize a `.exe`-suffixed git invocation end-to-end.
    That gap was closed in this same change set's follow-up commits
    (`9d19cae2`/`a93a1af4`), which replaced that module's own
    `_token_matches_binary` with THIS module's canonical
    `token_matches_binary` -- `block_subagent_commit.py` now recognizes a
    `.exe`-suffixed git invocation end-to-end through its primary matcher,
    same as every other caller (see
    `tests/test_shared_command_tokenizer_contract.py`'s
    `TestCaseFoldNowAppliesInCommitGuardsArgv0Rewrite`, and
    `block_subagent_commit.py`'s own `TestTokenMatchesBinaryClosesExeAndCmd
    Bypass` tests asserting through `_has_git_commit`, the primary matcher).
    """
    base = token.rstrip("/\\")
    base = base.rsplit("/", 1)[-1]
    base = base.rsplit("\\", 1)[-1]
    # NTFS/Windows silently strips ALL trailing dots and spaces from a
    # filename at resolution time (in any order, repeatedly) -- `git.exe.`
    # and `git.exe ` both resolve to `git.exe` on a real Windows invocation
    # (code-reviewer Finding 3, 2026-07-29: same OS-normalization axis as
    # the `.exe`/`.cmd` launcher-suffix fix above, one character further
    # along it). `rstrip(" .")` removes any trailing run of either
    # character before the suffix-strip below, symmetric with the
    # `rstrip("/\\")` separator-noise strip two lines up.
    #
    # GUARD (found while reconciling Finding 1/3, before landing): a token
    # that is ENTIRELY dots/spaces (POSIX `.` dot-source, `..` parent-dir)
    # would rstrip to an empty string here -- `.` is a meaningful shell
    # builtin/path token in its own right, not "a real basename with
    # trailing OS-normalization noise", and callers (e.g.
    # `block_subagent_destructive_action.py`'s `_SOURCE_VERBS` check)
    # compare the normalized result against the literal `.` string. Only
    # apply the strip when it leaves a non-empty remainder; otherwise the
    # dots/spaces ARE the whole (meaningful) token and must survive intact.
    dot_stripped = base.rstrip(" .")
    if dot_stripped:
        base = dot_stripped
    base = _WINDOWS_LAUNCHER_SUFFIX_RE.sub("", base)
    return base.lower()


def split_unquoted_newlines(cmd_text: str) -> str:
    """Convert every UNQUOTED newline in `cmd_text` into `;` -- the
    multi-line-Bash-command bypass fix (2026-07-30). `shlex.shlex` with
    `whitespace_split=True` consumes a bare newline as ordinary whitespace
    and never emits it as a token, so `tokenize_full_command` previously
    folded every line of a multi-line command AFTER the first into the
    first line's segment: its command-position head was never resolved, so
    no guard built on this seam (`block_worktree_creation`,
    `block_stash_destruction`, and every other consumer of this module's
    segmentation) ever classified it. Multi-line Bash commands are the
    ordinary case in this fleet, not a corner case, so this was a live,
    routinely-reachable bypass of every one of those guards.

    A character walk tracking quote state, not a regex -- the semantics
    below hinge on knowing whether a given newline sits inside a quote,
    which a regex over raw text cannot answer:
      - a newline inside single quotes is left literal, never converted;
      - a newline inside double quotes is left literal, never converted
        (real shell behavior: a double-quoted string can span lines and
        that literal newline is part of the argument's value) -- UNLESS it
        is immediately preceded by an unescaped backslash, in which case
        real shell semantics treat backslash-newline as a line continuation
        *even inside double quotes* (this is the standard idiom for
        splitting a double-quoted string across source lines without
        embedding a literal newline in the value, e.g.
        ``msg="line one \\`` + newline + ``line two"`` -> ``"line one line
        two"``) -- confirmed empirically against real bash (2026-07-30,
        Review: coordinator:code-reviewer P2 finding): both characters are
        REMOVED, same as the unquoted case below, and the two lines join
        with no separator emitted at all;
      - backslash-CR-LF inside double quotes is a DIFFERENT case, deliberately
        left untouched (both the backslash and the CR stay literal, and
        the LF that follows is then handled by the plain
        "newline-inside-double-quotes-stays-literal" rule above) --
        confirmed empirically against real bash: POSIX/bash's escape rule
        inside double quotes fires only when a backslash is followed by
        one of ``$``, a backtick, ``"``, another backslash, or an actual
        newline (LF); a backslash followed by CR matches none of those, so
        the backslash is NOT consumed as an escape and both characters
        pass through literally, and the CR is not itself the
        line-continuation trigger the LF-only case above is. This
        intentionally diverges from the UNQUOTED branch below, which does
        treat backslash-CR-LF as a continuation for CRLF-authored
        multi-line commands -- the divergence is between quoted and
        unquoted, not a gap in either;
      - an unquoted backslash-newline (real shell line continuation) has
        BOTH characters removed -- the two lines join with no separator
        emitted at all. Converting this pair to `;` instead would split a
        legitimately continued command in two (a backslash-continued
        ``git stash`` / ``drop`` pair is one ``git stash drop`` invocation,
        not ``git stash`` followed by a bare ``drop``);
      - quote-escaping backslashes are tracked throughout (an escaped quote
        character does not toggle quote state) so the state above stays
        correct across the whole walk;
      - `\r\n` behaves as `\n` -- the trailing `\r` is dropped as part of
        consuming the pair, so this does not depend on a caller having
        already CRLF-stripped its input.

    Every unquoted newline this function converts becomes `;`, an existing
    separator this module's own tokenizer/segmenter already treats as an
    always-separate punctuation token -- no new separator concept, just
    routing a newline through the one that already exists.
    """
    out: List[str] = []
    i = 0
    n = len(cmd_text)
    quote: Optional[str] = None
    while i < n:
        c = cmd_text[i]
        if quote is not None:
            # Review: coordinator:code-reviewer P2 -- `\<newline>` (LF only)
            # is a real line continuation even inside double quotes and
            # must be REMOVED like the unquoted case below, confirmed
            # empirically against real bash. `\<CR><LF>` is NOT a
            # continuation (POSIX's in-quote backslash-escape rule only
            # fires before `$`/backtick/`"`/`\`/an actual LF) -- that shape
            # falls through to the literal-copy branch unchanged, and the
            # trailing `\n` is then handled by the "newline inside double
            # quotes stays literal" case a few lines down.
            if c == "\\" and quote == '"' and i + 1 < n and cmd_text[i + 1] == "\n":
                i += 2
                continue
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(cmd_text[i:i + 2])
                i += 2
                continue
            out.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            nxt = cmd_text[i + 1]
            if nxt == "\n":
                i += 2
                continue
            if nxt == "\r" and i + 2 < n and cmd_text[i + 2] == "\n":
                i += 3
                continue
            out.append(cmd_text[i:i + 2])
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "\r" and i + 1 < n and cmd_text[i + 1] == "\n":
            out.append(";")
            i += 2
            continue
        if c == "\n":
            out.append(";")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def exceeds_tokenizable_ceiling(text: str) -> bool:
    """Return `True` when `text` is past `_MAX_TOKENIZABLE_COMMAND_CHARS` and
    must therefore NOT be handed to any `shlex` tokenizer.

    The single seam for the DIRECT `shlex.split`/`shlex.shlex` call sites in
    this package -- the ones that do not route through
    `tokenize_full_command` and so would otherwise never see the ceiling.
    Each such site calls this and takes its own PRE-EXISTING unparseable
    branch (the one an unterminated quote already reaches), which is why
    the ceiling introduces no new fail-direction anywhere.

    The bound is a DENIAL-OF-SERVICE property inherited from this module, not
    a per-call-site tuning knob: see `_MAX_TOKENIZABLE_COMMAND_CHARS`'s own
    comment for the quadratic it bounds. There is exactly one threshold in
    this package and this is the only comparison against it -- a call site
    that re-derives its own number has reintroduced the hang for whatever it
    lets through.

    NEGATIVE SPEC: this is not a "command too long" policy check and must
    never be used to produce a user-facing size verdict. It answers one
    question -- may `shlex` see this string -- and nothing else.
    """
    return len(text) > _MAX_TOKENIZABLE_COMMAND_CHARS


def tokenize_full_command(cmd_text: str) -> Optional[List[str]]:
    """Tokenize `cmd_text` with :mod:`shlex`, treating `;`/`&`/`|` as
    ALWAYS-separate punctuation tokens regardless of surrounding whitespace,
    while still respecting POSIX quoting -- a quoted separator or a quoted
    multi-word argument stays one token; an unquoted separator always
    yields a clean split point.

    Runs `cmd_text` through `split_unquoted_newlines` first (2026-07-30) --
    see that function's own docstring for the bypass this closes. Every
    unquoted newline becomes a `;` before `shlex` ever sees the text, so a
    multi-line command now segments exactly as its semicolon-joined
    equivalent already did; this is the only change from the single-line
    behavior below, which is otherwise unchanged from the two prior copies
    this module ported.

    Returns `None` on `ValueError` (unterminated quote / trailing
    backslash) -- every caller fails CLOSED on this (treats the command as
    the thing it is guarding against), the same fail-direction used
    throughout the bash-guard package for an unparseable segment.

    Returns `None` for the SAME REASON, without attempting to tokenize at
    all, when `cmd_text` exceeds `_MAX_TOKENIZABLE_COMMAND_CHARS` -- see
    that constant's own comment for the quadratic it bounds and why the
    bound is a security property rather than a tuning knob. This branch
    reuses the existing unparseable signal deliberately: it introduces no
    new fail-direction and no new caller contract, so a command past the
    ceiling lands on exactly the fail-closed path an unterminated quote
    already lands on today.
    """
    if exceeds_tokenizable_ceiling(cmd_text):
        return None
    cmd_text = split_unquoted_newlines(cmd_text)
    lex = shlex.shlex(
        _mask_adjacent_ampersand_redirects(cmd_text),
        posix=True,
        punctuation_chars=";&|",
    )
    lex.whitespace_split = True
    try:
        tokens = [
            token.replace(_AMP_REDIRECT_SENTINEL, "&") for token in lex
        ]
    except ValueError:
        return None
    return join_redirection_operator_tokens(tokens)


def _mask_adjacent_ampersand_redirects(cmd_text: str) -> str:
    """Replace every UNQUOTED `&` that is IMMEDIATELY followed by `>` with
    `_AMP_REDIRECT_SENTINEL`, so `shlex` lexes bash's `&>file` combine-
    redirect as part of one word instead of splitting at a punctuation `&`.

    This exists because whitespace is the ONLY thing distinguishing two
    entirely different bash constructs that `shlex` lexes identically:

    - `cmd &>file` -- ONE command, stdout+stderr redirected.
    - `cmd & >file other` -- TWO commands. `&` backgrounds the first; the
      second genuinely executes, with its own output redirected.

    Both lex to `['cmd', '&', '>file', ...]` once `shlex` has discarded
    whitespace, so no post-tokenization rule can tell them apart. Masking
    runs on the RAW text, where the distinction still exists.

    Security note, and the reason this is a mask rather than a token-level
    join: `block_subagent_commit._command_is_single_segment` gates the one
    branch that ALLOWS a `coordinator:git-commit-agent` command through in
    full. Collapsing the spaced two-command form into a single segment let
    `scoped-git-commit -m <subj> -- <path> & >/dev/null git commit -a -m x`
    read as one command and pass that precondition, allowing a bare
    `git commit -a` that never reaches `ceremony.scoped_git_commit`'s
    ownership/sweeping re-validation at all (found 2026-08-04 by security
    audit; introduced same-day by the token-level join this replaces).
    Ambiguity therefore resolves toward SPLITTING -- a false deny costs an
    agent one retry, a false allow costs a shared branch an unscoped commit.

    Quoting is honoured: a `&>` inside single or double quotes, or escaped
    with a backslash, is data and is left alone.
    """
    masked: List[str] = []
    in_single = False
    in_double = False
    i = 0
    total = len(cmd_text)
    while i < total:
        char = cmd_text[i]
        if char == "\\" and not in_single and i + 1 < total:
            masked.append(char)
            masked.append(cmd_text[i + 1])
            i += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif (
            char == "&"
            and not in_single
            and not in_double
            and i + 1 < total
            and cmd_text[i + 1] == ">"
        ):
            masked.append(_AMP_REDIRECT_SENTINEL)
            i += 1
            continue
        masked.append(char)
        i += 1
    return "".join(masked)


def join_redirection_operator_tokens(tokens: List[str]) -> List[str]:
    """Re-join the `&` of a REDIRECTION operator onto its neighbouring
    token, so `segments_from_tokens_*` never mistakes it for a command
    separator.

    `shlex` with `punctuation_chars=";&|"` cannot tell bash's control
    operator `&` from the `&` inside an fd-duplication redirection, so
    `foo -- a.py 2>&1` lexed as `['foo', '--', 'a.py', '2>', '&', '1']` and
    segmented as TWO segments. `2>&1` is not a control operator and starts
    no new command; treating it as one made an ordinary single-command
    invocation with a trailing `2>&1` (the single most common shape an agent
    writes when it wants to see stderr) look compound to every guard in this
    package. Confirmed live 2026-08-04: `block_subagent_commit.py`'s
    `_command_is_single_segment` precondition rejected
    `scoped-git-commit -m <subj> --repo <root> -- <path> <path> 2>&1`, and
    the resulting deny message sent the agent to re-check a pathspec that was
    already correct.

    ONE spelling is re-joined here: `<fd?><redirect-op>` `&` `<fd|->` --
    `2>&1`, `>&2`, `2>&-`, `<&0`. The trailing fd is folded in too, so the
    pathspec extractors downstream see ONE opaque redirection token rather
    than a stray `1` that reads as a path.

    This is safe to decide at token level because its left-hand side must be
    a BARE redirect-operator token (`2>`, `>`, `<`), which `shlex` emits only
    when the operator stands alone -- a real separator can never occupy that
    position, since `cmd 2>` with no target is not a runnable command.

    Bash's OTHER `&`-bearing redirect, `&>file`, is NOT handled here: it is
    indistinguishable at token level from a backgrounding `&` followed by a
    new command that opens with a redirect, and collapsing the two was a live
    guard bypass (see `_mask_adjacent_ampersand_redirects`, which resolves it
    on the raw text where whitespace still exists, before `shlex` runs).

    Joining is monotone: it only ever makes segments LONGER, never hides a
    token from a matcher, so no deny-side matcher in this package can be
    weakened by it. A genuine background/chaining `&` (`a & b`, `a && b`) is
    untouched -- `&&` lexes as one `'&&'` token, and a lone `&` re-joins only
    when a bare redirection operator sits immediately before it.
    """
    joined: List[str] = []
    i = 0
    total = len(tokens)
    while i < total:
        token = tokens[i]
        if token == "&" and joined and _REDIRECT_DUP_LHS_RE.match(joined[-1]):
            merged = joined[-1] + "&"
            i += 1
            if i < total and _REDIRECT_DUP_RHS_RE.match(tokens[i]):
                merged += tokens[i]
                i += 1
            joined[-1] = merged
            continue
        joined.append(token)
        i += 1
    return joined


def segments_from_tokens_with_pipe_flag(
    tokens: List[str],
) -> List[Tuple[List[str], bool]]:
    """Partition a flat token stream (from `tokenize_full_command`) into
    per-segment token lists at each unquoted `;`/`&`/`|` boundary, pairing
    each segment with whether it was immediately preceded by a `|` --
    needed to detect a bare, argument-less interpreter fed via stdin pipe
    (e.g. `echo <b64> | base64 -d | bash`).

    This is the richer of the two prior shapes (originally
    `block_subagent_destructive_action.py`'s `_segments_from_tokens`,
    return type `list[tuple[list[str], bool]]`). Used by every consumer
    that needs pipe-precedes-segment tracking:
    `block_subagent_destructive_action.py`, `block_worktree_creation.py`,
    `_sentinel_creation_guard.py`.
    """
    segments: List[Tuple[List[str], bool]] = []
    current: List[str] = []
    pipe_before = False
    for tok in tokens:
        if _SEPARATOR_TOKEN_RE.match(tok):
            if current:
                segments.append((current, pipe_before))
            current = []
            pipe_before = "|" in tok
            continue
        current.append(tok)
    if current:
        segments.append((current, pipe_before))
    return segments


def token_matches_binary(token: str, binary: str) -> bool:
    """Boundary-anchored, Windows-launcher-aware binary-name match: `token`
    identifies the same executable as `binary` if `token` equals `binary`
    exactly, or `token`'s normalized basename (path-separator-stripped,
    `.exe`/`.cmd`-suffix-stripped, case-folded -- see `normalize_executable_
    basename` and `_WINDOWS_LAUNCHER_SUFFIX_RE`'s own comment for the
    evidence behind exactly these two suffixes) equals `binary.lower()`.

    This is a PATH-SEPARATOR boundary, not a hyphen boundary:
    `evil-coordinator-safe-commit` does NOT match `coordinator-safe-commit`
    (no separator precedes the shared suffix, so the normalized basename of
    the whole token is itself, unchanged) -- and this holds for the `.cmd`
    spelling too: `evil-coordinator-safe-commit.cmd` also does NOT match,
    since suffix-stripping happens on the token's OWN basename, not on the
    binary name being compared against, so it cannot turn a hyphen boundary
    into a separator boundary. `bin/git`, `/usr/bin/git`, `git.exe`,
    `GIT.EXE`, `C:\\Git\\bin\\git.exe`, and `coordinator-safe-commit.cmd`
    all match their respective binary (a path separator or a stripped
    Windows launcher suffix precedes/wraps the identity in each). `gitk`,
    `git-foo`, and `mygit` are unaffected -- normalization only strips a
    recognized separator/suffix, it never truncates or fuzzy-matches a
    basename, so this can never make a match sloppier than the boundary
    check it replaces, only recognize more real spellings of the same
    identity.

    Canonical replacement for two independently-hand-maintained
    `_token_matches_binary` copies (`block_subagent_commit.py`,
    `block_reviewer_bash_outside_allowlist.py`) -- neither stripped a
    `.exe`/`.cmd` suffix, so a `git.exe`/`GIT.EXE`-spelled invocation and a
    `coordinator-safe-commit.cmd`-spelled invocation (both ordinary Windows
    forms -- the latter is this project's OWN generated launcher twin, see
    `coordinator/bin/coordinator-safe-commit.cmd` on disk -- and Windows is
    this project's primary platform) silently bypassed both guards.
    Confirmed empirically (2026-07-29, guard-brick incident response, this
    module's own consolidation): both copies returned `False` for `git.exe`
    against binary `git`, and `block_subagent_commit.py`'s copy separately
    returned `False` for `coordinator-safe-commit.cmd` against binary
    `coordinator-safe-commit`, while both correctly returning `True` for
    bare `git`, `/usr/bin/git`, and `bin/git`.
    """
    return token == binary or normalize_executable_basename(token) == binary.lower()


def segments_from_tokens_simple(tokens: List[str]) -> List[List[str]]:
    """Partition a flat token stream the same way as
    `segments_from_tokens_with_pipe_flag`, but return plain per-segment
    token lists with no pipe-precedes-segment tracking.

    This is the simpler of the two prior shapes (originally
    `block_subagent_commit.py`'s own-module `_segments_from_tokens`, return
    type `List[List[str]]`) -- that module's git-commit gate has no need
    for pipe tracking, so it is not ported; kept as an explicit, separately
    named function rather than silently dropped or silently unified onto
    the richer shape, per this module's own consolidation contract (do not
    pick one caller's behavior for another caller by accident).

    Derived from `segments_from_tokens_with_pipe_flag` (one source of truth
    for the segmentation walk itself; only the pipe flag is dropped here) --
    the two prior copies' segmentation loops were identical modulo that
    flag, verified by inspection before merging them.
    """
    return [seg for seg, _pipe_before in segments_from_tokens_with_pipe_flag(tokens)]


# ---------------------------------------------------------------------------
# M2 additions (2026-07-29) -- resolve-once command-position resolver,
# find_git_segment, and the classified wrapper table.
#
# LIVE. `dispatch.py` resolves every candidate command through
# `resolve_command_positions` before guard dispatch, and `dispatch_checks.py`
# consumes the resolved positions it threads through. New guards should reuse
# this resolver rather than re-rolling a first-segment-only parser.
#
# This is a PROMOTION of behaviour already smeared across five files, not
# new logic invented for this module: `block_worktree_creation.py`,
# `dispatch_checks.py`, `block_subagent_commit.py`,
# `_sentinel_creation_guard.py`, and `block_subagent_destructive_action.py`
# each carry their own near-identical `_skip_wrapper_own_argv`-shaped
# wrapper-argument-consumption walk (`_WRAPPER_ARG_FLAGS`/
# `_TIMEOUT_DURATION_RE`/`_NICE_BARE_NUMERIC_RE`, and in `dispatch_checks.py`
# alone, `_BYPASS_WRAPPER_BOOL_FLAGS` for `timeout`'s boolean flags,
# confirmed via differential execution to be the MOST-correct of the five --
# see that module's own Finding-9 comment). The generic peel below is that
# union: the four-file baseline plus `dispatch_checks.py`'s boolean-flag
# addition. None of those five copies are deleted here -- that is chunk
# M8's job in a later wave; deleting them now would break guards mid-wave.
# ---------------------------------------------------------------------------


class WrapperSemanticClass(Enum):
    """Axis 1 of the classified wrapper table: what a wrapper binary DOES to
    its own argv, independent of whether it is safe to carry into a
    rewrite (that is axis 2 -- see `REWRITE_FACING_WRAPPERS` below, and do
    not conflate the two: collapsing them onto one axis is the specific
    failure this table exists to avoid).

    - ``EXECS_ITS_ARGV`` -- the wrapper execs (or otherwise actually runs)
      the command named in its own remaining argv: `sudo`, `command`,
      `time`, `exec`, `nice`, `nohup`, `ionice`, `timeout`, `stdbuf`, `env`,
      `setsid`, `strace`, `doas`.
    - ``INSPECTS_WITHOUT_EXECING`` -- the wrapper reports ON the named
      command without ever running it: `which`, `type`. Peeling PAST one of
      these would be wrong -- `which git` never runs `git`, so the resolved
      head for that segment is `which` itself, not `git`.
    - ``APPLET_DISPATCHER`` -- `busybox`, whose remaining argv selects an
      APPLET, not necessarily a same-named real binary (this project's own
      evidence: busybox ships no `git` applet at all -- see
      `REWRITE_FACING_WRAPPERS`'s docstring). Peeling past it would assume
      an applet table this module does not have; treated as its own class
      so nothing peels through it by construction.
    """

    EXECS_ITS_ARGV = "execs-its-argv"
    INSPECTS_WITHOUT_EXECING = "inspects-without-execing"
    APPLET_DISPATCHER = "applet-dispatcher"


#: Single source of truth for axis 1. Built from the UNION of every on-disk
#: wrapper-word literal found while promoting this table (six on-disk
#: literals were named as the enumeration target for this task;
#: `test_command_tokenizer_peel_matrix.py`'s
#: `TestWrapperTableEnumeration` records that a SEVENTH was found --
#: `dispatch_checks.py`'s `_BYPASS_WRAPPER_WORDS` -- and that it added no
#: words beyond this union, only a narrower subset of it; see that test and
#: this chunk's own report for the discrepancy). `time` in particular
#: appears in every on-disk allowlist that includes it here -- an earlier
#: draft of a table like this one dropped it, silently narrowing a
#: confinement guard; it is deliberately present.
_WRAPPER_SEMANTIC_CLASS: Dict[str, WrapperSemanticClass] = {
    "sudo": WrapperSemanticClass.EXECS_ITS_ARGV,
    "command": WrapperSemanticClass.EXECS_ITS_ARGV,
    "time": WrapperSemanticClass.EXECS_ITS_ARGV,
    "exec": WrapperSemanticClass.EXECS_ITS_ARGV,
    "nice": WrapperSemanticClass.EXECS_ITS_ARGV,
    "nohup": WrapperSemanticClass.EXECS_ITS_ARGV,
    "ionice": WrapperSemanticClass.EXECS_ITS_ARGV,
    "timeout": WrapperSemanticClass.EXECS_ITS_ARGV,
    "stdbuf": WrapperSemanticClass.EXECS_ITS_ARGV,
    "env": WrapperSemanticClass.EXECS_ITS_ARGV,
    "setsid": WrapperSemanticClass.EXECS_ITS_ARGV,
    "strace": WrapperSemanticClass.EXECS_ITS_ARGV,
    "doas": WrapperSemanticClass.EXECS_ITS_ARGV,
    "which": WrapperSemanticClass.INSPECTS_WITHOUT_EXECING,
    "type": WrapperSemanticClass.INSPECTS_WITHOUT_EXECING,
    "busybox": WrapperSemanticClass.APPLET_DISPATCHER,
}

#: Axis-1 view: every wrapper that actually execs its own argv -- the
#: BAND-APPROPRIATE view for CONFINEMENT matching, which may treat this
#: wider class as suspicious without widening what may be REWRITTEN (that
#: is axis 2, `REWRITE_FACING_WRAPPERS`, a narrower, independently
#: initialized subset -- see its own docstring for why it is not derived
#: from this set).
EXECS_ITS_ARGV_WRAPPERS: FrozenSet[str] = frozenset(
    word for word, cls in _WRAPPER_SEMANTIC_CLASS.items()
    if cls is WrapperSemanticClass.EXECS_ITS_ARGV
)

#: Axis-1 view: wrappers that report on a command without ever running it.
INSPECTS_WITHOUT_EXECING_WRAPPERS: FrozenSet[str] = frozenset(
    word for word, cls in _WRAPPER_SEMANTIC_CLASS.items()
    if cls is WrapperSemanticClass.INSPECTS_WITHOUT_EXECING
)

#: Axis-1 view: applet dispatchers -- argv selects an applet, not
#: necessarily a same-named real binary.
APPLET_DISPATCHER_WRAPPERS: FrozenSet[str] = frozenset(
    word for word, cls in _WRAPPER_SEMANTIC_CLASS.items()
    if cls is WrapperSemanticClass.APPLET_DISPATCHER
)

#: Axis 2 -- REWRITE-FACING membership. A NARROWER subset of
#: `EXECS_ITS_ARGV_WRAPPERS`, explicitly and independently initialized --
#: deliberately NOT `EXECS_ITS_ARGV_WRAPPERS` itself, and NOT computed from
#: it. Initialized VERBATIM to today's `_FIND_WRAPPER_WORDS`
#: (`dispatch_checks.py:2675`): `sudo`, `command`, `time`, `env`, `nice`,
#: `nohup`, `exec`, `timeout`, `stdbuf` -- nine words, no more, no fewer.
#:
#: Only a wrapper in THIS set may be carried into an auto-rewrite (e.g.
#: `check_offer_git_c`'s `-C <dir>` suggestion). `setsid`, `strace`, `doas`,
#: and `ionice` are `EXECS_ITS_ARGV` for CONFINEMENT-matching purposes but
#: are deliberately excluded here: they are real execs-its-argv wrappers,
#: just not yet vetted for rewrite-safety, and a single-axis design would
#: have widened this set automatically the moment they were classified
#: execs-its-argv -- exactly the failure this two-axis table exists to
#: prevent. `which`/`type` (`INSPECTS_WITHOUT_EXECING`) and `busybox`
#: (`APPLET_DISPATCHER`) can never belong here regardless of future
#: widening of `EXECS_ITS_ARGV_WRAPPERS`, for the concrete reasons in
#: `WrapperSemanticClass`'s own docstring: `cd X && which git` rewritten to
#: `which git -C X` never actually runs git (nothing to confine), and
#: `busybox` has no git applet to rewrite into at all.
#:
#: Widen only by explicit future decision -- never automatically because a
#: word is added to `EXECS_ITS_ARGV_WRAPPERS`.
REWRITE_FACING_WRAPPERS: FrozenSet[str] = frozenset(
    {"sudo", "command", "time", "env", "nice", "nohup", "exec", "timeout", "stdbuf"}
)


def wrapper_semantic_class(word: str) -> Optional[WrapperSemanticClass]:
    """Return `word`'s axis-1 `WrapperSemanticClass`, or `None` if `word` is
    not a recognized wrapper at all. `word` should already be basename-
    normalized (see `normalize_executable_basename`) -- this is a plain dict
    lookup, no further normalization is applied here."""
    return _WRAPPER_SEMANTIC_CLASS.get(word)


def is_rewrite_facing_wrapper(word: str) -> bool:
    """Return whether `word` (already basename-normalized) is a member of
    the narrower, explicitly-initialized axis-2 rewrite-facing set -- see
    `REWRITE_FACING_WRAPPERS`'s own docstring for why this is never derived
    from axis 1."""
    return word in REWRITE_FACING_WRAPPERS


#: Ported verbatim (union of the four non-`dispatch_checks.py` copies plus
#: `dispatch_checks.py`'s own richer boolean-flag addition, confirmed via
#: differential execution to be the most-correct of the five --
#: `dispatch_checks.py`'s own Finding-9 comment) from
#: `_skip_wrapper_own_argv`'s supporting tables in all five files named in
#: this section's module-level comment above.
_WRAPPER_ARG_FLAGS: Dict[str, FrozenSet[str]] = {
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    # 2026-07-30: the four wrappers below previously had NO entry, which this
    # function's docstring named as a known limitation and left open. It was a
    # live bypass, not a cosmetic gap -- `sudo -u root git stash drop` and
    # `doas -u root git worktree add ...` both reached their guards and were
    # ALLOWED, because the walk stopped at the first separate-token flag and
    # command-position resolution never reached `git`. Confirmed by direct
    # probe against `block_stash_destruction` and `block_worktree_creation`.
    # `sudo` is the highest-value wrapper to close: it is the one an agent
    # reaches for when a command has just been refused.
    "sudo": frozenset(
        {
            "-u", "--user", "-g", "--group", "-p", "--prompt", "-r", "--role",
            "-t", "--type", "-C", "--close-from", "-T", "--command-timeout",
            "-U", "--other-user", "-h", "--host", "-R", "--chroot",
            "-D", "--chdir",
        }
    ),
    "doas": frozenset({"-u", "-C"}),
    "strace": frozenset(
        {"-e", "-o", "-p", "-s", "-E", "-u", "-a", "-b", "-I", "-P", "-O", "-S"}
    ),
    "setsid": frozenset(),
}
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_NICE_BARE_NUMERIC_RE = re.compile(r"^-\d+$")

#: Only `dispatch_checks.py`'s copy had this -- GNU `timeout`'s boolean
#: long/short flags, which take no value token. Promoted here as part of
#: "the most-correct of the five" per this task's own instruction.
_WRAPPER_BOOL_FLAGS: Dict[str, FrozenSet[str]] = {
    "timeout": frozenset({"--foreground", "--preserve-status", "-v", "--verbose"}),
    # Companion to the 2026-07-30 arg-flag additions above. A value-taking
    # table alone does NOT close the bypass: a VALUELESS flag stops the walk
    # just as hard, and `sudo -E git stash drop` was confirmed to reach allow
    # for exactly that reason. Both tables are required per wrapper.
    "sudo": frozenset(
        {
            "-A", "--askpass", "-b", "--background", "-E", "--preserve-env",
            "-H", "--set-home", "-i", "--login", "-K", "--remove-timestamp",
            "-k", "--reset-timestamp", "-l", "--list", "-n",
            "--non-interactive", "-P", "--preserve-groups", "-S", "--stdin",
            "-s", "--shell", "-v", "--validate", "-B", "--bell",
        }
    ),
    "doas": frozenset({"-L", "-n", "-s"}),
    "setsid": frozenset({"-c", "--ctty", "-f", "--fork", "-w", "--wait"}),
    "strace": frozenset(
        {
            "-f", "-ff", "-c", "-C", "-d", "-h", "-i", "-q", "-qq", "-r",
            "-t", "-tt", "-ttt", "-T", "-v", "-V", "-y", "-yy", "-z", "-Z",
            "-k", "-w",
        }
    ),
}


def _skip_wrapper_own_argv(tokens: List[str], i: int, base: str) -> int:
    """Advance `i` past `base`'s own flag/argument tokens (attached, e.g.
    `-c2`/`-oL`, and separate-token, e.g. `-c 2`), plus `timeout`'s boolean
    flags, one required positional duration operand, and `nice`'s bare-
    numeric niceness operand. `base` must already be basename-normalized.

    Canonical promoted copy -- see this section's module-level comment for
    the five pre-existing hand-maintained duplicates this consolidates.

    A wrapper with NO entry in either table is the bypass shape to watch.
    The walk stops at that wrapper's first separate-token flag, so command
    position never reaches the wrapped binary and every guard built on this
    helper allows the invocation. This was live until 2026-07-30 for
    `sudo`/`doas`/`setsid`/`strace` -- `sudo -u root git stash drop` and the
    `git worktree add` equivalent both reached allow -- and those four now
    have entries. Note that BOTH tables matter: `sudo -E <cmd>` bypassed via
    a valueless flag, which an arg-flag table alone would not have caught.

    RESIDUAL, and the reason this is a table rather than a rule: coverage is
    only as complete as the entries. A wrapper absent from
    `_PASSTHROUGH_WRAPPERS` in the calling guards is not peeled at all, and a
    flag absent from a registered wrapper's two tables still stops the walk.
    Adding a wrapper to a guard's passthrough set without adding its flags
    here re-opens exactly this hole. Prefer extending both tables in the same
    change, and treat an unrecognized wrapper flag as a gap to close rather
    than an acceptable edge -- the caller reaching for a wrapper prefix is
    often one that has just been refused.
    """
    n = len(tokens)
    taking = _WRAPPER_ARG_FLAGS.get(base, frozenset())
    boolean = _WRAPPER_BOOL_FLAGS.get(base, frozenset())
    consumed_duration = False
    while i < n:
        tok = tokens[i]
        if tok in boolean:
            i += 1
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in taking:
            i += 1
            if i < n:
                i += 1
            continue
        if len(tok) > 2 and tok[0] == "-" and tok[1] != "-" and ("-" + tok[1]) in taking:
            i += 1
            continue
        if base == "timeout" and not consumed_duration and _TIMEOUT_DURATION_RE.match(tok):
            i += 1
            consumed_duration = True
            continue
        if base == "nice" and _NICE_BARE_NUMERIC_RE.match(tok):
            i += 1
            continue
        break
    return i


class ResolutionConfidence(Enum):
    """Carried on every `ResolvedCommand` -- REPORTED and consumed by
    nobody in this chunk. That is deliberate: it makes a later
    fail-direction decision expressible without another refactor. Do not
    wire this to any guard verdict from this module.

    - ``RESOLVED`` -- head is a known non-wrapper binary; no peeling
      occurred.
    - ``PEELED`` -- head was a recognized wrapper (or grouping/env prefix);
      peeling occurred and landed on a resolved command.
    - ``UNRESOLVED`` -- head token is unrecognized (a variable expansion
      placeholder, an empty segment after peeling, or similarly
      indeterminate) and, in the case of a non-empty remainder, is followed
      by something that could itself be a command.
    """

    RESOLVED = "resolved"
    PEELED = "peeled"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedCommand:
    """One executed segment's command-position token stream, as resolved by
    `resolve_command_positions`.

    - `tokens` -- the segment's tokens from the resolved command-position
      head onward (grouping markers, leading env assignments, and peeled
      wrapper binaries/their own argv already stripped). Empty if nothing
      resolvable remained (see `ResolutionConfidence.UNRESOLVED`).
    - `raw_tokens` (M5, 2026-07-29) -- this SAME segment's tokens BEFORE
      `_peel_command_position` ran: i.e. exactly what
      `segments_from_tokens_with_pipe_flag` yielded for this segment (or, on
      the unparseable/depth-cap fallback paths, the same single-element
      `[cmd_text]` shape as `tokens` on those paths). Exists so a caller can
      consume this module's segmentation (heredoc-stripping, quote-aware
      splitting, command-substitution/`-c`-payload recursion) WITHOUT also
      inheriting the wide `EXECS_ITS_ARGV_WRAPPERS` peel `tokens` reflects --
      a caller with its OWN narrower wrapper vocabulary (e.g.
      `dispatch_checks.check_no_verify`'s `_BYPASS_WRAPPER_WORDS`, which
      deliberately excludes `setsid`/`strace`/`doas`) walks `raw_tokens`
      itself rather than trusting the wide peel already reflected in
      `tokens`. Using `tokens` for that purpose would silently widen the
      caller's own wrapper coverage the moment this module's wrapper table
      widens -- exactly the single-axis failure `REWRITE_FACING_WRAPPERS`'s
      docstring warns against, one layer up.
    - `head` -- `tokens[0]` verbatim (NOT basename-normalized -- callers
      needing normalized identity call `normalize_executable_basename`
      themselves), or `None` if `tokens` is empty.
    - `confidence` -- see `ResolutionConfidence`.
    - `pipe_before` -- whether this segment was immediately preceded by an
      unquoted `|` (same meaning as
      `segments_from_tokens_with_pipe_flag`'s per-segment flag).
    - `depth` -- recursion depth this segment was discovered at: `0` for a
      segment of the original `cmd_text`, `>0` for one recovered from inside
      a `$( )`/backtick substitution or an interpreter `-c` payload.
    """

    tokens: List[str]
    raw_tokens: List[str]
    head: Optional[str]
    confidence: ResolutionConfidence
    pipe_before: bool
    depth: int


#: Depth cap for BOTH recursion sources below (interpreter `-c` payloads,
#: command substitution) -- a single shared cap, not one per source, so a
#: pathological mix of the two cannot multiply past it. Chosen generously
#: above any real legitimate nesting (a hand-written command substituting
#: into an interpreter substituting into another interpreter is already an
#: edge case) while still bounding recursion on adversarial input.
_MAX_RESOLVE_DEPTH = 4

#: Interpreters whose `-c <string>` argument is executed code, not inert
#: text -- same set used throughout this package's guards
#: (`_C_FLAG_SHELL_INTERPRETERS` in `block_subagent_destructive_action.py`).
_SHELL_INTERPRETERS_WITH_C = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

#: Bundled-or-standalone `-c` short flag, e.g. `-c`, `-ic`, `-ci` -- a
#: shell's CLI parser accepts bundled short flags, so `sh -ic '<payload>'`
#: behaves as `sh -i -c '<payload>'`. Same pattern as
#: `block_subagent_commit.py`'s `_BUNDLED_C_FLAG_RE`.
_BUNDLED_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")

#: A heredoc opener: `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`. Group 1 is
#: the optional quote character (unused beyond matching symmetrically),
#: group 2 is the delimiter word.
_HEREDOC_OPEN_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

#: A token that is entirely a `$NAME`/`${...}` variable reference, or a
#: leftover empty placeholder -- not a literal binary name we can resolve
#: statically. Deliberately narrow: an ordinary word is never mistaken for
#: this, so this can only ever ADD `UNRESOLVED` findings, never suppress a
#: `RESOLVED`/`PEELED` one.
_INDETERMINATE_HEAD_RE = re.compile(r"^\$")


def _strip_heredocs(text: str) -> str:
    """Remove every heredoc BODY (and its terminator line) from `text`,
    leaving the `<<WORD` opener token in place so the command it redirects
    into still tokenizes normally, and inserting an explicit `;` separator
    where the terminator line is removed -- a heredoc terminator implicitly
    ends the command that opened it, and without an explicit separator a
    following command on the next source line would otherwise be
    whitespace-joined into the SAME segment by `tokenize_full_command`
    (which treats a bare newline as ordinary whitespace, not a segment
    boundary).

    Heredoc body text is DATA (redirected stdin), never itself a
    command-position segment to walk over -- an unquoted `;`/`&`/`|`
    inside a heredoc body must not be mistaken for a real segment
    boundary, which is exactly what stripping the body before segmenting
    prevents.

    `<<-WORD` (tab-stripping form) matches the terminator line after
    stripping its own leading tabs, same as real shell behavior. A quoted
    delimiter (`<<'WORD'`) is treated identically to an unquoted one for
    terminator-matching purposes (this resolver has no need for the
    quoted-delimiter distinction of suppressing expansion inside the body,
    since the body is discarded entirely).
    """
    if "<<" not in text:
        return text
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = _HEREDOC_OPEN_RE.match(text, i)
        if not m:
            out.append(text[i])
            i += 1
            continue
        strip_tabs = m.group(0).startswith("<<-")
        marker = m.group(2)
        out.append(text[i:m.end()])
        i = m.end()
        nl = text.find("\n", i)
        if nl == -1:
            # No newline after the opener -- there is no body to strip
            # (single-line command text, the common case for these guards).
            out.append(text[i:])
            i = n
            break
        out.append(text[i:nl + 1])
        i = nl + 1
        terminated = False
        while i < n:
            line_end = text.find("\n", i)
            raw_line = text[i:line_end if line_end != -1 else n]
            check_line = raw_line.lstrip("\t") if strip_tabs else raw_line
            i = (line_end + 1) if line_end != -1 else n
            if check_line == marker:
                terminated = True
                break
        if terminated and i < n:
            out.append(";")
    return "".join(out)


def _extract_command_substitutions(text: str) -> Tuple[str, List[str]]:
    """Single quote-aware, paren-balanced walk over `text` that extracts
    every top-level `$( ... )` and backtick `` `...` `` command substitution
    (nested `$( )` correctly paren-balanced; nested backticks are NOT --
    real shell requires escaping to nest those, which this walk does not
    attempt to unescape, the same scope real shell syntax itself imposes).

    Substitution is recognized inside double-quoted text (real shell
    behavior: only single quotes suppress it) but NOT inside single-quoted
    text. Each recognized substitution's INNER command text is collected
    (for the caller to recursively resolve) and replaced in the returned
    text with a single neutral space -- enough for the outer segment's
    head/argv shape to stay intact for tokenization without the inner
    command text confusing the outer parse.

    Returns `(text_with_placeholders, [inner_command_text, ...])` in
    left-to-right encounter order.
    """
    out: List[str] = []
    subs: List[str] = []
    i = 0
    n = len(text)
    in_sq = False
    while i < n:
        c = text[i]
        if in_sq:
            out.append(c)
            if c == "'":
                in_sq = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "'":
            in_sq = True
            out.append(c)
            i += 1
            continue
        if text.startswith("$(", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            subs.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:j])
            out.append(" ")
            i = j
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j == -1:
                out.append(c)
                i += 1
                continue
            subs.append(text[i + 1:j])
            out.append(" ")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), subs


def _extract_dash_c_payload(argv_after_interpreter: List[str]) -> Optional[str]:
    """Scan `argv_after_interpreter` (the tokens following the interpreter
    binary itself) for a standalone `-c` or a bundled short flag containing
    `c` (e.g. `-ic`, `-ci`), and return the token immediately following it
    (the payload string), or `None` if no such flag/payload is present."""
    n = len(argv_after_interpreter)
    for i, tok in enumerate(argv_after_interpreter):
        is_c_flag = tok == "-c" or (
            tok != "-" and tok.startswith("-") and not tok.startswith("--")
            and _BUNDLED_C_FLAG_RE.match(tok)
        )
        if is_c_flag:
            return argv_after_interpreter[i + 1] if i + 1 < n else None
    return None


def _peel_command_position(seg_tokens: List[str]) -> Tuple[List[str], ResolutionConfidence]:
    """Peel grouping markers (`(`/`{`), leading env assignments, and
    recognized `EXECS_ITS_ARGV` wrapper binaries (plus their own argv) off
    the front of `seg_tokens`, repeating until nothing more strips.

    Stops (does not peel further) at an `INSPECTS_WITHOUT_EXECING` wrapper
    (`which`/`type`) or an `APPLET_DISPATCHER` (`busybox`) -- see
    `WrapperSemanticClass`'s own docstring for why peeling past either
    would be wrong. That wrapper itself becomes the resolved head.
    """
    tokens = list(seg_tokens)
    peeled_anything = False
    while tokens:
        head = tokens[0]
        if head in ("(", "{"):
            tokens = tokens[1:]
            peeled_anything = True
            continue
        if len(head) > 1 and head[0] == "(":
            tokens = [head[1:]] + tokens[1:]
            peeled_anything = True
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            tokens = tokens[1:]
            peeled_anything = True
            continue
        base = normalize_executable_basename(head)
        if base == "env":
            tokens = tokens[1:]
            while tokens and (
                re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
                or tokens[0] in ("-i", "--ignore-environment")
            ):
                tokens = tokens[1:]
            peeled_anything = True
            continue
        cls = wrapper_semantic_class(base)
        if cls is WrapperSemanticClass.EXECS_ITS_ARGV:
            tokens = tokens[1:]
            i = _skip_wrapper_own_argv(tokens, 0, base)
            tokens = tokens[i:]
            peeled_anything = True
            continue
        break

    if not tokens:
        return [], ResolutionConfidence.UNRESOLVED

    head = tokens[0]
    if _INDETERMINATE_HEAD_RE.match(head):
        return tokens, ResolutionConfidence.UNRESOLVED
    return tokens, (ResolutionConfidence.PEELED if peeled_anything else ResolutionConfidence.RESOLVED)


def resolve_command_positions(cmd_text: str, *, _depth: int = 0) -> List[ResolvedCommand]:
    """THE resolve-once entry point: raw command text -> an ordered list of
    `ResolvedCommand`, one per executed segment. Peels ONCE, in one place:
    separators, quoting, `(...)`/`{...;}` grouping, backtick/`$(...)`
    substitution, leading env assignments, wrapper binaries and their own
    argv, and interpreter `-c` payloads (standalone and bundled short
    flags), recursively with a shared depth cap, plus heredocs.

    Order: any command substitutions found in `cmd_text` are resolved FIRST
    (real shell semantics: a substitution's command runs before the outer
    command that consumes its output), in left-to-right encounter order,
    followed by `cmd_text`'s own top-level segments in order; a segment
    whose head is a `-c`-fed shell interpreter has its payload's resolved
    segments appended immediately after that segment's own entry.

    On unparseable input (`tokenize_full_command` returns `None`, e.g. an
    unterminated quote) returns a single `UNRESOLVED` entry carrying the
    raw text as `tokens` -- fails CLOSED (reports maximal uncertainty)
    rather than silently returning nothing, consistent with every other
    fail-direction in this package.

    Past `_MAX_RESOLVE_DEPTH`, returns a single `UNRESOLVED` entry for the
    remaining text rather than recursing further -- bounds recursion on
    adversarial/pathological nesting.

    Past `_MAX_TOKENIZABLE_COMMAND_CHARS`, returns that SAME single
    `UNRESOLVED` entry, short-circuiting BEFORE `_strip_heredocs` and
    `_extract_command_substitutions`. Gating here as well as inside
    `tokenize_full_command` is not redundant: those two walks run ahead of
    tokenization and are themselves linear passes over the whole text with
    per-substitution recursion on top, so on a multi-megabyte command they
    would still cost real time after the tokenizer itself had declined the
    work. This is the SAME fail-closed `UNRESOLVED` shape the unparseable
    path below already returns -- see `_MAX_TOKENIZABLE_COMMAND_CHARS`.
    """
    if exceeds_tokenizable_ceiling(cmd_text):
        return [ResolvedCommand(
            tokens=[cmd_text], raw_tokens=[cmd_text], head=cmd_text or None,
            confidence=ResolutionConfidence.UNRESOLVED, pipe_before=False, depth=_depth,
        )]
    if _depth > _MAX_RESOLVE_DEPTH:
        return [ResolvedCommand(
            tokens=[cmd_text], raw_tokens=[cmd_text], head=cmd_text or None,
            confidence=ResolutionConfidence.UNRESOLVED, pipe_before=False, depth=_depth,
        )]

    results: List[ResolvedCommand] = []

    stripped = _strip_heredocs(cmd_text)
    neutralized, subs = _extract_command_substitutions(stripped)
    for sub_cmd in subs:
        results.extend(resolve_command_positions(sub_cmd, _depth=_depth + 1))

    tokens = tokenize_full_command(neutralized)
    if tokens is None:
        results.append(ResolvedCommand(
            tokens=[cmd_text], raw_tokens=[cmd_text], head=cmd_text or None,
            confidence=ResolutionConfidence.UNRESOLVED, pipe_before=False, depth=_depth,
        ))
        return results

    for seg_tokens, pipe_before in segments_from_tokens_with_pipe_flag(tokens):
        if not seg_tokens:
            continue
        peeled, confidence = _peel_command_position(seg_tokens)
        results.append(ResolvedCommand(
            tokens=peeled, raw_tokens=seg_tokens, head=(peeled[0] if peeled else None),
            confidence=confidence, pipe_before=pipe_before, depth=_depth,
        ))
        if peeled:
            head_base = normalize_executable_basename(peeled[0])
            if head_base in _SHELL_INTERPRETERS_WITH_C:
                payload = _extract_dash_c_payload(peeled[1:])
                if payload is not None:
                    results.extend(resolve_command_positions(payload, _depth=_depth + 1))

    return results


#: General separator-class characters (mirrors `_SEPARATOR_TOKEN_RE`'s
#: `;`/`&`/`|`) used by `find_git_segment`'s own quote-aware walk. A run of
#: one or more of these, unquoted, is one segment boundary -- this
#: subsumes `&&`/`||`/bare `;`/bare `&`/bare `|` alike, the same separator
#: concept the rest of this module already uses for tokenized segmentation.
_GIT_SEGMENT_SEPARATOR_CHARS = ";&|"


def _skip_wrapper_own_argv_text(cmd: str, i: int, base: str) -> int:
    """Text-based sibling of `_skip_wrapper_own_argv` for `find_git_segment`'s
    raw-text walk (which deliberately does not re-tokenize -- see that
    function's own docstring) -- same `_WRAPPER_ARG_FLAGS`/
    `_WRAPPER_BOOL_FLAGS`/`_TIMEOUT_DURATION_RE`/`_NICE_BARE_NUMERIC_RE`
    tables, walking whitespace-separated words in `cmd` starting at `i`
    instead of a pre-split token list. Not quote-aware -- acceptable here
    because a wrapper's OWN flag argument (a niceness value, a duration, an
    I/O buffering mode) is not realistically quoted, and the
    quote-sensitive part of this walk (not mistaking quoted text for a
    segment separator) is handled separately, by the caller's own
    quote-aware terminator scan."""
    n = len(cmd)
    taking = _WRAPPER_ARG_FLAGS.get(base, frozenset())
    boolean = _WRAPPER_BOOL_FLAGS.get(base, frozenset())
    consumed_duration = False
    while i < n:
        while i < n and cmd[i] in " \t":
            i += 1
        m = re.match(r"\S+", cmd[i:])
        if not m:
            break
        tok = m.group(0)
        tok_end = i + len(tok)
        if tok in boolean:
            i = tok_end
            continue
        if tok.startswith("--") and "=" in tok:
            i = tok_end
            continue
        if tok in taking:
            i = tok_end
            while i < n and cmd[i] in " \t":
                i += 1
            m2 = re.match(r"\S+", cmd[i:])
            if m2:
                i += len(m2.group(0))
            continue
        if len(tok) > 2 and tok[0] == "-" and tok[1] != "-" and ("-" + tok[1]) in taking:
            i = tok_end
            continue
        if base == "timeout" and not consumed_duration and _TIMEOUT_DURATION_RE.match(tok):
            i = tok_end
            consumed_duration = True
            continue
        if base == "nice" and _NICE_BARE_NUMERIC_RE.match(tok):
            i = tok_end
            continue
        break
    return i


def find_git_segment(cmd: str) -> Dict[str, str]:
    """Single quote-aware walk over `cmd` locating the FIRST top-level
    segment whose command-position head (after skipping leading whitespace,
    `VAR=value` env assignments, and `EXECS_ITS_ARGV` wrapper words -- same
    peel vocabulary as `_peel_command_position`, applied here directly to
    raw text rather than to a token list, since this function's whole
    purpose is to avoid a second, independently-derived tokenization pass)
    is `git`.

    Returns `{"prefix": ..., "body": ..., "tail": ...}`:
    - `prefix` -- the raw text from the segment's start up to (not
      including) the `git` token: any leading env assignment / wrapper-word
      text, verbatim.
    - `body` -- the raw text from the `git` token through the end of this
      segment (not including the terminating separator run).
    - `tail` -- everything from the terminating separator run to the end of
      `cmd`, INCLUDING the separator itself; empty string if this segment
      runs to the end of `cmd`.

    Returns `{}` if no top-level `git` segment is found at all.

    This is the SINGLE walk that replaces two independently-derived
    `prefix1`/`git_prefix` computations (`check_offer_git_c`'s pre-existing
    two-derivation shape, the exact drift risk this function exists to
    remove -- see this chunk's own brief). Pinned exactly to this signature
    for chunk M6 (a later wave) to consume against.
    """
    n = len(cmd)
    seg_start = 0
    while seg_start <= n:
        i = seg_start
        # Skip leading whitespace, env assignments, and EXECS_ITS_ARGV
        # wrapper words -- same vocabulary as `_peel_command_position`,
        # applied to raw text.
        prev = -1
        while i != prev:
            prev = i
            while i < n and cmd[i] in " \t":
                i += 1
            m_env = re.match(r"[A-Za-z_][A-Za-z0-9_]*=\S*", cmd[i:])
            if m_env and (i + m_env.end() >= n or cmd[i + m_env.end()] in " \t"):
                i += m_env.end()
                continue
            m_word = re.match(r"[A-Za-z0-9_]+", cmd[i:])
            if m_word:
                word = m_word.group(0)
                base = normalize_executable_basename(word)
                if base in EXECS_ITS_ARGV_WRAPPERS and (
                    i + len(word) >= n or cmd[i + len(word)] in " \t"
                ):
                    i += len(word)
                    i = _skip_wrapper_own_argv_text(cmd, i, base)
                    continue
            break

        prefix_start = seg_start
        git_start = i
        is_git = cmd[i:i + 3] == "git" and (i + 3 >= n or cmd[i + 3] in " \t")

        # Walk this segment (quote-aware) to find its terminating separator
        # run (or end of string).
        j = i
        in_sq = in_dq = False
        term_start = -1
        while j < n:
            c = cmd[j]
            if in_sq:
                if c == "'":
                    in_sq = False
                j += 1
                continue
            if in_dq:
                if c == "\\" and j < n - 1:
                    j += 2
                    continue
                if c == '"':
                    in_dq = False
                j += 1
                continue
            if c == "'":
                in_sq = True
                j += 1
                continue
            if c == '"':
                in_dq = True
                j += 1
                continue
            if c in _GIT_SEGMENT_SEPARATOR_CHARS:
                term_start = j
                while j < n and cmd[j] in _GIT_SEGMENT_SEPARATOR_CHARS:
                    j += 1
                break
            j += 1

        if is_git:
            if term_start >= 0:
                body_end = term_start
                tail = cmd[term_start:]
            else:
                body_end = n
                tail = ""
            return {
                "prefix": cmd[prefix_start:git_start],
                "body": cmd[git_start:body_end],
                "tail": tail,
            }

        if term_start < 0:
            break
        seg_start = j

    return {}
