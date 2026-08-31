"""coordinator_core.bash_guards.guard_no_optional_locks -- auto-rewrites a
lock-acquiring ``git status``/``git diff`` invocation to its behavior-
preserving ``git --no-optional-locks``-flagged equivalent, prompt-free.

Subject: fleet-wide `.git/index.lock` contention -- ~18 concurrent EM
sessions run against shared trees, most of them in this repo, and
`git status`/`git diff` are the two commands agents reach for constantly
that take the worktree lock for no operational reason (they only refresh
cached stat data, then write it back). Doctrine prose cannot hold this line
-- `git status` is too ingrained in the model -- so the enforcement is
mechanical, same posture as ``guard_offer_git_c.check_offer_git_c``'s
``cd``-prefix rewrite, which this module follows for shape, message format,
and tokenization.

MEASURED EVIDENCE (operator's Windows box, git 2.55.0.windows.3):
``git status`` creates ``.git/index.lock``; ``git --no-optional-locks
status`` does not (reproduced 4/4 via FileSystemWatcher against a positive
control, corroborated by ``.git/index`` mtime). Output and exit code are
byte-identical with and without the flag -- the flag only suppresses
write-back of refreshed stat data, not the refresh itself, so this rewrite
is behavior-preserving and safe to apply prompt-free.

THE FLAG IS PRE-SUBCOMMAND ONLY. ``git --no-optional-locks status`` works;
``git status --no-optional-locks`` exits 129 ("error: unknown option"), no
output. This module inserts the flag strictly between the resolved git
GLOBAL-OPTION span (``git``, any ``-C <path>``/``-c ...``/etc.) and the
resolved SUBCOMMAND token -- never after it. See
``test_no_optional_locks_rewrite.py``'s
``TestFlagLandsPreSubcommand`` for the explicit pin.

NOT rewritten (evidence: same measurement pass found these do not take the
worktree lock at all, so rewriting them would be a no-op flag with no
lock-contention benefit and pure risk of an unfamiliar exit-129 shape if
this module's subcommand-arg heuristic is ever wrong):
  - ``git diff --cached`` / ``--staged`` (any spelling/position).
  - ``git ls-files -m`` (not a targeted subcommand at all -- see
    ``_REWRITE_SUBCOMMANDS``).
  - A ref-to-ref ``git diff`` (``git diff HEAD:<f> stash@{0}:<f>``,
    ``git diff <sha>..HEAD``) -- detected heuristically (see
    ``_diff_args_are_ref_shaped``) rather than resolved against a live
    repository, because this guard runs on the PreToolUse hot path with no
    repository access.

Reuses the package's shared git-global-flag vocabulary
(``_GIT_GLOBAL_OPT_WITH_ARG``/``_GIT_GLOBAL_OPT_NO_ARG_SIMPLE`` --
``dispatch_checks.py`` already lists ``--no-optional-locks`` among them, see
that module's own comment on ``_GIT_GLOBAL_OPT_NO_ARG_SIMPLE``) rather than
hand-rolling a second one, and the package's shared quote-aware tokenizer
(``tokenize_full_command``/``token_matches_binary``) to DECIDE whether and
where to rewrite -- same reuse posture ``guard_offer_git_c.py``'s own
docstring calls out for its own segmentation.

REWRITING ITSELF IS SURGICAL TEXT INSERTION, not token-list-and-rejoin
reconstruction (see ``check_git_no_optional_locks``'s own docstring for the
live corruption its prior reconstruction shape produced). Locating the raw
character offset to insert at needs a SECOND, offset-tracking scan of the
original text (``_raw_token_spans``) -- `shlex` itself exposes no reliable
mid-token stream position, so this is a hand-rolled scanner mirroring the
same grammar, not a "fresh regex over raw command text" of the kind this
paragraph used to disclaim. Its output is never trusted unverified: every
insertion is gated on this scanner's token VALUES matching
``tokenize_full_command``'s own output exactly (see
``check_git_no_optional_locks``), so a grammar divergence between the two
can only ever suppress a rewrite, never mis-place one.

Spec backlink: docs/plans/2026-08-07-git-index-lock-contention-campaign.md [DEAD-CITATION: plan file never committed to this repo]
(fleet-wide lock-contention campaign) -- this guard is the mechanical leg of
that campaign; doctrine prose alone was assessed insufficient (see this
module's own opening paragraph).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards.dispatch_checks import (
    _GIT_GLOBAL_OPT_NO_ARG_SIMPLE,
    _GIT_GLOBAL_OPT_WITH_ARG,
    _allow_rewrite,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._command_tokenizer import (
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
    _REDIRECT_DUP_LHS_RE as _bt_REDIRECT_DUP_LHS_RE,
    _REDIRECT_DUP_RHS_RE as _bt_REDIRECT_DUP_RHS_RE,
)
from coordinator_core.bash_guards._dialect import dialect_from_tool_name
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command

#: The two subcommands this guard rewrites -- the two lock-acquiring
#: commands agents reach for constantly (see module docstring). Deliberately
#: closed and small: widening this set is a future decision, not something
#: this guard infers from a subcommand's name.
_REWRITE_SUBCOMMANDS = frozenset({"status", "diff"})

#: A `git diff` argument spelling this guard treats as "already excluded
#: from the worktree lock" -- rewriting these would be a no-op flag, not a
#: contention fix (see module docstring's NOT-rewritten list).
_DIFF_STAGED_FLAGS = frozenset({"--cached", "--staged"})

#: `git diff` spellings that must NOT be rewritten for the OPPOSITE reason
#: to `_DIFF_STAGED_FLAGS`: not because the flag would be inert, but because
#: `--no-optional-locks` actively breaks what the caller is doing.
#:
#: `--quiet` is the phantom-clearing probe. An ordinary `git diff` refreshes
#: the index stat-cache and WRITES IT BACK, which is how a stat-cache
#: phantom (a file git believes is dirty because its mtime moved while its
#: content did not) heals itself. `--no-optional-locks` suppresses exactly
#: that write-back. Rewrite a `--quiet` probe and the phantom it is probing
#: for can never clear: every subsequent probe re-reads dirty, forever.
#: `commit_gates`' own EOL-phantom probe path runs straight through here.
#:
#: Reported as item 1 of DoE-claude's 2026-08-12 six-defect bundle and
#: re-verified 2026-08-31 (`state/audits/2026-08-31-the-six-defect-bundle-
#: reverified.md`) -- the one item of that bundle's five that reproduced.
#: DoE fixed the same mechanism on their own side by excluding
#: `git_native.diff_quiet`, pinned by their
#: `test_phantom_clearing_readers_keep_the_optional_lock`.
#:
#: Kept as its own set rather than folded into `_DIFF_STAGED_FLAGS`: the two
#: answer different questions ("would the flag do nothing?" vs "would the
#: flag do harm?"), and a future reader widening one must not silently
#: inherit the other's rationale.
_DIFF_PHANTOM_CLEARING_FLAGS = frozenset({"--quiet"})

#: Token characters that make a shlex punctuation token a command separator
#: -- identical set to `guard_offer_git_c._OFFER_SEP_TOKEN_CHARS`, not
#: imported from there because that name is that module's own private
#: implementation detail, not a shared export.
_SEP_TOKEN_CHARS = frozenset(";&|")


def _diff_args_are_ref_shaped(args: List[str]) -> bool:
    """Return ``True`` when any non-flag `git diff` argument looks like a
    ref-to-ref or ref:path comparison (``<sha>..HEAD``, ``HEAD:file``,
    ``stash@{0}:file``) rather than an ordinary worktree-vs-index diff.

    Heuristic, not a real ref resolution (this guard has no repository
    access on the PreToolUse hot path) -- a flag token (leading ``-``) is
    never inspected, since a flag's OWN spelling cannot itself be a ref."""
    for arg in args:
        if arg.startswith("-"):
            continue
        if ".." in arg or "@{" in arg or ":" in arg:
            return True
    return False


def _resolve_git_global_span(tokens: List[str]) -> Optional[Tuple[int, bool]]:
    """``tokens[0]`` is already confirmed to be a `git` invocation. Walk
    past git's own global options (the package's shared
    ``_GIT_GLOBAL_OPT_WITH_ARG``/``_GIT_GLOBAL_OPT_NO_ARG_SIMPLE``
    vocabulary -- reused, not re-enumerated, per this module's own
    docstring) to the SUBCOMMAND token. Returns
    ``(subcommand_index, already_has_no_optional_locks)``, or ``None`` when
    the walk runs out of tokens before finding one, or hits an
    unrecognized flag -- the same fail-closed "unknown shape, do not guess"
    contract ``dispatch_checks._seg_resolved_git_subcommand`` already uses
    for this exact walk shape, one call site over."""
    i = 1
    n = len(tokens)
    has_nol = False
    while i < n:
        tok = tokens[i]
        if tok == "--no-optional-locks":
            has_nol = True
            i += 1
            continue
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            return None
        return (i, has_nol)
    return None


def _rewrite_insertion_index(seg_tokens: List[str]) -> Optional[int]:
    """Return the SEGMENT-LOCAL index the ``--no-optional-locks`` flag must
    land before, or ``None`` when this segment is not itself a bare `git
    status`/`git diff` invocation this guard rewrites -- already carries
    ``--no-optional-locks`` (idempotence), or is a `git diff` shape this
    module's own NOT-rewritten list excludes.

    Pure DECISION logic, deliberately separated from reconstruction (there
    is none any more -- see ``check_git_no_optional_locks``'s own docstring
    for why token-list-and-rejoin reconstruction was removed): the caller
    turns this index into a raw character offset via the parallel span list
    it already holds, and inserts text there. This function never sees or
    needs an offset itself."""
    if not seg_tokens or not _bt_token_matches_binary(seg_tokens[0], "git"):
        return None

    resolved = _resolve_git_global_span(seg_tokens)
    if resolved is None:
        return None
    sub_idx, has_nol = resolved
    subcommand = seg_tokens[sub_idx]
    if subcommand not in _REWRITE_SUBCOMMANDS:
        return None
    if has_nol:
        return None

    if subcommand == "diff":
        args = seg_tokens[sub_idx + 1:]
        if any(a in _DIFF_STAGED_FLAGS for a in args):
            return None
        if any(a in _DIFF_PHANTOM_CLEARING_FLAGS for a in args):
            return None
        if _diff_args_are_ref_shaped(args):
            return None

    return sub_idx


def _is_unmasked_sep_char(text: str, i: int) -> bool:
    """`text[i]` is a live ``;``/``&``/``|`` separator character -- `True`
    for every `_SEP_TOKEN_CHARS` member EXCEPT an `&` immediately followed
    by `>`, the left half of bash's `&>`/`&>>` combine-redirect, which must
    stay part of the surrounding WORD rather than start a new segment (real
    shell semantics: `cmd &>file` is ONE command with combined-stream
    redirection, not `cmd` backgrounded followed by a bare `>file`).
    Deliberately the same distinction `_command_tokenizer.
    _mask_adjacent_ampersand_redirects` makes by masking the raw text before
    handing it to `shlex` -- this checks the same condition inline instead,
    since `_raw_token_spans` below scans the raw text itself and has no
    separate un-masking step to run afterward."""
    c = text[i]
    if c not in _SEP_TOKEN_CHARS:
        return False
    if c == "&" and i + 1 < len(text) and text[i + 1] == ">":
        return False
    return True


def _raw_token_spans(text: str) -> Optional[List[Tuple[str, int, int]]]:
    """Quote-aware scan of raw command TEXT into ``(value, start, end)``
    triples -- the offset-tracking twin of `tokenize_full_command`'s
    value-only token stream, hand-rolled rather than instrumenting `shlex`
    itself (`shlex.shlex` buffers pushback internally, so its stream
    position mid-token is not a reliable offset source). Mirrors the same
    grammar `tokenize_full_command` applies over `text` directly: POSIX
    single/double-quote handling, an unquoted backslash escaping exactly the
    next character, `;`/`&`/`|` as always-separate punctuation runs (via
    `_is_unmasked_sep_char`), and the `&>`-combine-redirect exception that
    keeps such an `&` inside its surrounding word.

    Returns ``None`` on an unterminated quote or a trailing unescaped
    backslash -- the same unparseable signal `tokenize_full_command` reports
    via its own `ValueError` catch, so `check_git_no_optional_locks` needs
    no separate fail-closed branch for this scanner's version of the same
    failure.

    Deliberately NOT a general reimplementation of the shared tokenizer: it
    does not model `preserve_windows_backslashes=True` (this guard never
    passes it) or the unquoted-newline-to-`;` conversion
    `split_unquoted_newlines` performs (the caller bails before ever
    reaching this function when `text` contains a raw newline -- see
    `check_git_no_optional_locks`'s own header comment). Its output is never
    trusted on faith either way: the caller diffs this function's token
    VALUES against `tokenize_full_command`'s own output and bails on any
    mismatch, rather than risk an insertion offset computed from a grammar
    deviation neither implementation anticipated."""
    spans: List[Tuple[str, int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in (" ", "\t"):
            i += 1
            continue
        if _is_unmasked_sep_char(text, i):
            start = i
            while i < n and _is_unmasked_sep_char(text, i):
                i += 1
            spans.append((text[start:i], start, i))
            continue

        start = i
        value_chars: List[str] = []
        while i < n:
            ch = text[i]
            if ch in (" ", "\t"):
                break
            if _is_unmasked_sep_char(text, i):
                break
            if ch == "'":
                j = text.find("'", i + 1)
                if j == -1:
                    return None
                value_chars.append(text[i + 1:j])
                i = j + 1
                continue
            if ch == '"':
                i += 1
                while i < n and text[i] != '"':
                    if (
                        text[i] == "\\"
                        and i + 1 < n
                        and text[i + 1] in ('"', "\\", "$", "`")
                    ):
                        value_chars.append(text[i + 1])
                        i += 2
                        continue
                    value_chars.append(text[i])
                    i += 1
                if i >= n:
                    return None
                i += 1
                continue
            if ch == "\\":
                if i + 1 >= n:
                    return None
                value_chars.append(text[i + 1])
                i += 2
                continue
            value_chars.append(ch)
            i += 1
        spans.append(("".join(value_chars), start, i))
    return spans


def _join_dup_redirect_spans(
    spans: List[Tuple[str, int, int]],
) -> List[Tuple[str, int, int]]:
    """Span-tracking twin of `tokenize_full_command`'s own
    `join_redirection_operator_tokens`: re-joins the `&` of an fd-duplication
    redirect (`2>&1`, `>&2`, `2>&-`, `<&0`) onto its neighbouring span, so
    the value list this produces stays in exact parity with the decision
    tokenizer's own post-join output -- without this step, ANY command
    containing that idiom would fail the value-parity check in
    `check_git_no_optional_locks` and lose its rewrite for no reason other
    than this function not having done the same merge. Same regexes
    (`_bt_REDIRECT_DUP_LHS_RE`/`_bt_REDIRECT_DUP_RHS_RE`), same merge rule --
    only the bookkeeping differs, since each merged entry here must also
    carry a combined character span."""
    joined: List[Tuple[str, int, int]] = []
    i = 0
    total = len(spans)
    while i < total:
        value, start, end = spans[i]
        if value == "&" and joined and _bt_REDIRECT_DUP_LHS_RE.match(joined[-1][0]):
            prev_value, prev_start, _prev_end = joined[-1]
            merged_value = prev_value + "&"
            merged_end = end
            i += 1
            if i < total and _bt_REDIRECT_DUP_RHS_RE.match(spans[i][0]):
                merged_value += spans[i][0]
                merged_end = spans[i][2]
                i += 1
            joined[-1] = (merged_value, prev_start, merged_end)
            continue
        joined.append((value, start, end))
        i += 1
    return joined


def check_git_no_optional_locks(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[dict]:
    """PreToolUse ADVISORY_REWRITE-band guard: auto-rewrites every top-level
    `git status`/bare `git diff` segment in ``cmd`` to insert
    ``--no-optional-locks`` immediately before the subcommand, prompt-free
    (no deny, no advisory-only offer -- see module docstring for why this
    campaign's mechanism has to be mechanical). Returns ``None`` (allow,
    unchanged) when nothing in ``cmd`` needs rewriting.

    SURGICAL TEXT INSERTION, not token-list-and-rejoin reconstruction. The
    prior shape tokenized the whole command, then rebuilt EVERY segment
    (rewritten or not) via ``" ".join(shlex.quote(t) for t in tokens)`` --
    which silently corrupts any segment containing a shell construct that
    does not round-trip through bare-word requoting: a redirect operator
    (`>`, `>>`, `<`, `2>`) gets quoted into a literal filename argument, and
    a `$`/backtick expansion gets single-quoted into inert text. Confirmed
    live: ``git -C <path> status --porcelain | sed 's/^...//' >
    /tmp/out.txt; echo "RC=$?"`` came back with `sed: >: No such file or
    directory` and a literal `RC=$?` on stdout, because the untouched `sed`
    and `echo` segments were reconstructed right alongside the rewritten
    `git status` one.

    This function instead decides WHERE to insert using the package's
    shared tokenizer (`tokenize_full_command`, unchanged), then finds the
    RAW CHARACTER OFFSET of that insertion point via `_raw_token_spans` --
    a quote-aware scanner over the ORIGINAL text that mirrors the same
    grammar -- and slices `--no-optional-locks ` directly into `cmd` at
    those offsets. Every byte outside an insertion point is copied from the
    original string verbatim; nothing is ever re-quoted or rejoined, so a
    redirect, an expansion, or any other construct in an untouched segment
    (or even in the REWRITTEN segment's own trailing arguments) survives
    byte-for-byte.

    Fails CLOSED (returns ``None``, no rewrite) rather than guess, in three
    situations that would otherwise offset the insertion into the wrong
    place: `cmd` contains a raw newline (`split_unquoted_newlines` converts
    it before the decision tokenizer ever runs, so a raw-text offset would
    no longer line up with the decision token it was computed for --
    correctness over rewrite coverage for multi-line commands, a
    deliberately accepted residual, see this guard's dispatch report);
    `_raw_token_spans` hits an unparseable shape of its own; or its token
    VALUES do not match `tokenize_full_command`'s output exactly, meaning
    this scanner's independently-written grammar has diverged from the
    shared tokenizer's on some construct neither implementation
    anticipated -- an offset computed against a mismatched token stream
    cannot be trusted to land in the right place."""
    if not cmd or "git" not in cmd:
        return None
    if _override("COORDINATOR_ALLOW_OPTIONAL_LOCKS"):
        return None

    cmd = _crlf_strip(cmd)
    if "\n" in cmd:
        return None

    # Shape precedence: a command whose PRIMARY shape is MULTI_PROBE_BANNER
    # is already owned by the `multiprobe-banner`/`multiprobe-banner-
    # rewrite` guards (dispatch.py), whose remedy (collapsing every probe,
    # including any bare `git status`, into one process) strictly subsumes
    # this guard's own single-flag insertion -- rewriting just the `git
    # status` segment here would offer a weaker fix AND short-circuit the
    # dispatch chain before the banner guards (registered in a later band,
    # see dispatch.py's `GuardBand` ordering) ever run. Mirrors the same
    # shape-precedence deferral `guard_multiprobe_banner.check` itself
    # already honors against `Shape.GREP_VIA_BASH` (AC-7). Dialect-aware
    # (payload's `tool_name`) so this also defers correctly under
    # PowerShell, where `dialect_from_tool_name` resolves a dialect this
    # guard's own bash-only tokenizer above cannot classify shapes for.
    dialect = dialect_from_tool_name(
        (payload or {}).get("tool_name") if isinstance(payload, dict) else None
    )
    if dialect is not None:
        classification = classify_command(cmd, dialect=dialect)
        if (
            classification.primary is not None
            and classification.primary.shape is Shape.MULTI_PROBE_BANNER
        ):
            return None

    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return None

    spans = _raw_token_spans(cmd)
    if spans is None:
        return None
    spans = _join_dup_redirect_spans(spans)

    if [value for value, _start, _end in spans] != tokens:
        return None

    groups: List[List[Tuple[str, int, int]]] = []
    current: List[Tuple[str, int, int]] = []
    for value, start, end in spans:
        if value and set(value) <= _SEP_TOKEN_CHARS:
            groups.append(current)
            current = []
            continue
        current.append((value, start, end))
    groups.append(current)

    insertion_offsets: List[int] = []
    rewritten_segments: List[str] = []
    for seg_spans in groups:
        if not seg_spans:
            continue
        seg_tokens = [value for value, _start, _end in seg_spans]
        sub_idx = _rewrite_insertion_index(seg_tokens)
        if sub_idx is None:
            continue
        insertion_offsets.append(seg_spans[sub_idx][1])
        rewritten_segments.append(" ".join(seg_tokens))

    if not insertion_offsets:
        return None

    pieces: List[str] = []
    prev_end = 0
    for offset in sorted(insertion_offsets):
        pieces.append(cmd[prev_end:offset])
        pieces.append("--no-optional-locks ")
        prev_end = offset
    pieces.append(cmd[prev_end:])
    new_cmd = "".join(pieces)

    note = (
        "Auto-rewritten: %s -> '--no-optional-locks' inserted before the "
        "subcommand on %d segment(s) -- avoids the shared-tree "
        "`.git/index.lock` acquisition."
        % (
            "; ".join("'%s'" % s for s in rewritten_segments),
            len(rewritten_segments),
        )
    ) + " " + operator_override_note("COORDINATOR_ALLOW_OPTIONAL_LOCKS", payload=payload)
    return _allow_rewrite(new_cmd, note)
