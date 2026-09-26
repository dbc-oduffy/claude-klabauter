"""coordinator_core.hooks.guard_doctrine_surface_bash_write — PreToolUse
(Bash|PowerShell) op: close the Bash escape from the C7 doctrine admission
gate (`coordinator_core.hooks.check_claude_md_size`, registered on
Write|Edit|MultiEdit only).

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/guard-doctrine-surface-bash-
write.py` (1748 lines). Shape, per the W4-C1 verdict: command/native-door,
`hooks.<name>` op, payload-dict-in/response-out -- stdin JSON read and
`sys.exit()` are replaced with `register_op`'s contract and this package's
own `deny`/`no_advisory` builders, same pattern as every sibling guard this
row lands. `is_denied_bash_write(cmd)` -- the pure predicate -- and every
helper it composes are UNCHANGED, byte-faithful ports; only
`_governed_identifiers()`'s source (`coordinator_core.hooks.claude_md_ledger.
GOVERNED_AUTHORING_SURFACES`, already landed this same chunk) and the
message-envelope import path change.

THE DEFECT THIS CLOSES: the admission gate that governs
`_claude_md_ledger.GOVERNED_AUTHORING_SURFACES` (unjustified growth of
always-on doctrine) only ever sees the Write/Edit/MultiEdit tool names. A
shell redirect (`cat > file`, `printf >> file`, `tee`), an interpreter
payload (`python3 -c "open(p,'w').write(...)"`, `sed -i`, `perl -i`), or a
heredoc writes a governed surface with the admission gate never firing at
all. This hook is the mirror-image guard on the Bash surface.

DETECTION STRATEGY, summarized (see DoE-claude's own source for the full
worked-example prose this port trims for size -- every numbered point below
is a distinct, load-bearing carve-out, unchanged in this port):

  1. Whole-command PREFILTER (`_mentions_governed_identifier`, deliberately
     unbounded substring match) -- if no governed identifier is mentioned
     anywhere, ALLOW immediately.
  2. TOP-LEVEL SEGMENTATION (`_split_top_level_segments`) at `;`, `&&`,
     `||`, `|`, newline -- outside quotes/parens -- so an unrelated stage in
     the same command cannot leak a marker into an unrelated mention.
  3. PER-SEGMENT SINK CLASSIFICATION -- a segment that mentions a governed
     identifier AND carries a write marker (`_has_write_marker_for_point3`)
     or an indirection marker (`_has_indirection_marker`) denies.
  4. CROSS-SEGMENT VARIABLE-ASSIGNMENT INDIRECTION
     (`_assignment_indirection_reaches_a_write`) -- a governed path bound to
     a variable, later dereferenced by a write elsewhere in the command.
  5. FAIL-CLOSED on any indirection this hook cannot reliably parse.
  6. READ-SHAPE CARVE-OUT for a mentioning segment with no write/indirection
     marker (`cat`, `grep`, `git show`, etc).
  7. GIT CONTENT-SAFE SUBCOMMAND CARVE-OUT (`_is_git_read_shape`) plus the
     `ceremony.scoped_git_commit` wrapper-family mirror
     (`_is_commit_wrapper_read_shape`) -- a commit message mentioning a
     governed name is not a write to it.
  8. GIT CONTENT-MUTATING SUBCOMMANDS (`_is_git_content_mutation`) are
     themselves a write marker (`git checkout HEAD~5 -- CLAUDE.md`).
  9. `coordinator_core.session.claude_md_grant` CLI CARVE-OUT
     (`_is_claude_md_grant_read_shape`) -- its write target is its own grant
     record, never the governed file.
  10. XARGS-PIPE INDIRECTION (`_has_xargs_pipe_indirection`) -- a piped
      identifier substituted into a later segment's write at runtime.
  11. INTERPRETER-INVOCATION READ-SHAPE (`_is_interpreter_read_shape`) -- a
      governed name appearing only inside a quoted interpreter-payload
      string literal, with no write/exec marker anywhere in the segment.

NEGATIVE-SPECS (unchanged, deliberate, accepted over-denial in the safe
direction): a segment mentioning a governed surface alongside an unrelated
write marker in the SAME segment still denies (this hook does not parse
*which* argument a redirect targets within one segment); an interpreter
payload writing an unrelated destination whose STRING ARGUMENT happens to
quote a governed filename as prose still denies for the identical reason --
fixed via doctrine (the deny message names Edit as the correct persist path
for an already-provisioned destination), not detection, because both
targeted detection fixes evaluated (stripping quoted spans before the
identifier check; call-argument-position parsing) were rejected as unsound.
Two narrower shapes with a reliably-distinguishable mechanism ARE fixed:
`_has_write_marker_for_point3`'s bare-redirect-target-token narrowing, and
heredoc-body detection for a Python triple-quoted assignment mistaken for a
live shell variable assignment.

Contract: `no_advisory()` on allow (silent), `deny()` on block. Fires only
on `tool_name in _COMMAND_TOOL_NAMES` (`"Bash"` or `"PowerShell"`). Any
missing/non-string `tool_input.command` fails OPEN -- there is no command to
classify at all in that case, distinct from "a command was extracted but its
target could not be classified" (point 5, fails CLOSED).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

import re

from coordinator_core.hooks._envelope import deny, no_advisory, payload_of
from coordinator_core.hooks.claude_md_ledger import GOVERNED_AUTHORING_SURFACES
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#doctrine-surface-bash-write-guard-carve-outs-and-remedies"
)

_COMMAND_TOOL_NAMES = ("Bash", "PowerShell")


def _governed_identifiers() -> "list[str]":
    """Every full repo-relative path AND bare basename from
    `GOVERNED_AUTHORING_SURFACES`, de-duplicated, longest-first (so a
    full-path match is checked before its own basename would also match --
    order doesn't change the verdict here since this is a pure membership
    test, but longest-first keeps the reported "matched" identifier the
    more specific one when this list is used for diagnostics)."""
    identifiers: "set[str]" = set()
    for surface in GOVERNED_AUTHORING_SURFACES:
        identifiers.add(surface)
        identifiers.add(surface.rsplit("/", 1)[-1])
    return sorted(identifiers, key=len, reverse=True)


_GOVERNED_IDENTIFIERS = _governed_identifiers()

#: Case-folded mirror of `_GOVERNED_IDENTIFIERS` -- both macOS and Windows
#: governed file as `CLAUDE.md` on either platform. The case-SENSITIVE list
_GOVERNED_IDENTIFIERS_LOWER = tuple(identifier.lower() for identifier in _GOVERNED_IDENTIFIERS)

#: Path-segment-boundary-anchored mirror of `_GOVERNED_IDENTIFIERS_LOWER` --
_GOVERNED_IDENTIFIER_PATTERNS = tuple(
    re.compile(r"(?<![A-Za-z0-9])" + re.escape(identifier) + r"(?![A-Za-z0-9])")
    for identifier in _GOVERNED_IDENTIFIERS_LOWER
)

_SAFE_REDIRECT_RE = re.compile(r"\d?>&\d|>>?\s*/dev/null")
_BARE_REDIRECT_RE = re.compile(r">>?")


def _has_redirect_marker(text: str) -> bool:
    stripped = _SAFE_REDIRECT_RE.sub("", text)
    return bool(_BARE_REDIRECT_RE.search(stripped))


_TEE_RE = re.compile(r"\btee\b")
_SED_INPLACE_RE = re.compile(r"\bsed\b.{0,120}?(-i\b|--in-place\b)", re.DOTALL)
_PERL_INPLACE_RE = re.compile(r"\bperl\b.{0,120}?-i\b", re.DOTALL)
#: A copying/truncating command name counts only in COMMAND POSITION -- at the
_CP_MV_RE = re.compile(
    r"(?:^|[|&;(]|\|\||&&)\s*(?:\w+=\S*\s+)*(?:sudo\s+|command\s+|env\s+)*"
    r"\b(cp|mv|install|dd|truncate)\b"
)
_WRITE_MODE_OPEN_RE = re.compile(r"open\([^)]*['\"][wax]['\"]")
_WRITE_METHOD_RE = re.compile(r"\.write(_text|_bytes)?\(")

_EX_ED_RE = re.compile(r"\b(ex|ed)\b")
_PATCH_RSYNC_RE = re.compile(r"\b(patch|rsync)\b")
_CURL_OUTPUT_RE = re.compile(r"\bcurl\b.{0,200}?(-o\b|--output\b)", re.DOTALL)
_WGET_OUTPUT_RE = re.compile(r"\bwget\b.{0,200}?(-O\b|--output-document\b)", re.DOTALL)
_SED_WRITE_SCRIPT_RE = re.compile(r"\bsed\b.{0,200}?\bw\s+\S", re.DOTALL)


def _redirect_target_token(segment: str) -> "str | None":
    masked = _SAFE_REDIRECT_RE.sub(lambda m: " " * len(m.group(0)), segment)
    last = None
    for match in _BARE_REDIRECT_RE.finditer(masked):
        last = match
    if last is None:
        return None
    rest = segment[last.end() :].lstrip()
    if not rest:
        return None
    if rest[0] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        return rest[1:end] if end != -1 else rest[1:]
    end = 0
    while end < len(rest) and not rest[end].isspace():
        end += 1
    return rest[:end]


def _has_write_marker_for_point3(segment: str) -> bool:
    """Point 3's write-marker check, narrowed for the BARE-REDIRECT shape
    only: a redirect counts as evidence of a governed write iff EITHER the
    identifier mention survives `_strip_quoted_spans` (a bare operand) OR
    the redirect's own target token names a governed identifier. When
    neither holds, the redirect itself is set aside and every OTHER write
    marker (`tee`, `sed -i`, `cp`/`mv`, `open(...,'w')`, `curl -o`, ...) is
    still checked at its existing broad, unnarrowed, fail-closed scope."""
    if not _has_redirect_marker(segment):
        return _has_write_marker(segment)
    target = _redirect_target_token(segment)
    target_mentions = bool(target) and _mentions_governed_identifier(target)
    bare_mention = _mentions_governed_identifier(_strip_quoted_spans(segment))
    if target_mentions or bare_mention:
        return True
    without_redirect = _BARE_REDIRECT_RE.sub(" ", _SAFE_REDIRECT_RE.sub(" ", segment))
    return _has_write_marker(without_redirect)


def _has_write_marker(text: str) -> bool:
    if _has_redirect_marker(text):
        return True
    for pattern in (
        _TEE_RE,
        _SED_INPLACE_RE,
        _PERL_INPLACE_RE,
        _CP_MV_RE,
        _WRITE_MODE_OPEN_RE,
        _WRITE_METHOD_RE,
        _EX_ED_RE,
        _PATCH_RSYNC_RE,
        _CURL_OUTPUT_RE,
        _WGET_OUTPUT_RE,
        _SED_WRITE_SCRIPT_RE,
    ):
        if pattern.search(text):
            return True
    return False


_INTERPRETER_RE = re.compile(r"\b(python3?|perl|ruby|node)\b")
_SHELL_DASH_C_RE = re.compile(r"\b(sh|bash|zsh)\b.{0,40}?-c\b", re.DOTALL)
_EVAL_RE = re.compile(r"\beval\b")
_XARGS_RE = re.compile(r"\bxargs\b")
_CMD_SUBST_RE = re.compile(r"\$\(|`")

_INDIRECTION_PATTERNS = (
    _INTERPRETER_RE,
    _SHELL_DASH_C_RE,
    _EVAL_RE,
    _XARGS_RE,
    _CMD_SUBST_RE,
)


def _has_indirection_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INDIRECTION_PATTERNS)


def _mentions_governed_identifier(text: str) -> bool:
    """Whole-command PREFILTER: does this text mention a governed surface at
    all? Deliberately UNBOUNDED (plain substring). A false positive here
    costs nothing: the segment still has to clear a write-marker check
    downstream. A false negative here is total."""
    lowered = text.lower()
    return any(identifier in lowered for identifier in _GOVERNED_IDENTIFIERS_LOWER)


def _names_governed_identifier(text: str) -> bool:
    """Path-segment-BOUNDARY-anchored test: does this text name a governed
    surface as its own path component, rather than merely contain the
    characters inside a longer basename (`dotclaude.md`)? Precision half of
    the pair above; never a prefilter."""
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _GOVERNED_IDENTIFIER_PATTERNS)


#: strict subset of `_INDIRECTION_PATTERNS` -- deliberately excludes bare
_CODE_EXECUTION_PATTERNS = (
    _INTERPRETER_RE,
    _SHELL_DASH_C_RE,
    _EVAL_RE,
    _XARGS_RE,
)


def _has_code_execution_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CODE_EXECUTION_PATTERNS)


_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredoc_bodies(text: str) -> str:
    """`text` with the BODY of every heredoc removed, keeping the command
    line that introduces it. A heredoc body is inert data -- it is fed to a
    command's stdin, and no amount of `>` or quoting inside it can write a
    file. Only the introducing command line can.

    UNDELIMITED (single-line) FALLBACK: an agent-emitted command frequently
    arrives as ONE LINE with every newline collapsed to a space -- the
    introducing `<<'TERM'` marker and the terminator token then share a
    line with no newline to delimit the body between them. When the
    terminator cannot be found on a line of its own, this function falls
    back to locating it as a whitespace-bounded TOKEN later in the same
    line and strips the span between the marker and that token. If even
    that token cannot be found (a genuinely unterminated heredoc), nothing
    is stripped and the line is kept whole."""
    lines = text.split("\n")
    out: "list[str]" = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = _HEREDOC_START_RE.search(line)
        idx += 1
        if not match:
            out.append(line)
            continue
        terminator = match.group(2)
        scan = idx
        while scan < len(lines) and lines[scan].strip() != terminator:
            scan += 1
        if scan < len(lines):
            out.append(line)
            out.append(lines[scan])
            idx = scan + 1
            continue
        term_token_re = re.compile(r"(?<!\S)" + re.escape(terminator) + r"(?!\S)")
        term_match = term_token_re.search(line, match.end())
        if term_match is None:
            out.append(line)
            continue
        out.append(line[: match.end()])
        out.append(line[term_match.start() :])
    return "\n".join(out)


_STDIN_PROGRAM_RE = re.compile(
    r"(?:^|[;&|]|\s)(?:python3?|perl|ruby|node)\s+-(?=\s|$)"
    r"|(?:^|[;&|]|\s)(?:bash|sh)\s+-s(?=\s|$)"
    r"|(?:^|[;&|]|\s)(?:bash|sh)\s*(?=<<)"
)


def _stdin_program_heredoc_bodies(text: str) -> "list[str]":
    """The BODY of every heredoc whose introducing line makes stdin the
    program (`_STDIN_PROGRAM_RE`) -- for these, and only these, the body IS
    the executed program, so it is returned for live scanning rather than
    treated as inert data."""
    bodies: "list[str]" = []
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = _HEREDOC_START_RE.search(line)
        idx += 1
        if not match:
            continue
        terminator = match.group(2)
        scan = idx
        while scan < len(lines) and lines[scan].strip() != terminator:
            scan += 1
        body = "\n".join(lines[idx:scan]) if scan < len(lines) else ""
        if scan < len(lines):
            idx = scan + 1
        if body and _STDIN_PROGRAM_RE.search(line[: match.start()] + " "):
            bodies.append(body)
    return bodies


_PAYLOAD_ASSIGN_RE = re.compile(
    r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:('''|\"\"\")([\s\S]*?)\2"
    r"|(['\"])([^'\"]*)\4|([^\s;&|()]+))"
)


def _has_stdin_program_var_write(cmd: str) -> bool:
    """True iff a stdin-as-program heredoc body binds a governed doctrine
    surface to a name and then writes THROUGH that name. Closes the seam
    between point 3 and point 4: point 3 denies only when the identifier
    and the write marker share one segment; point 4 catches the
    assign-then-dereference shape but runs on heredoc-STRIPPED text so a
    body never reaches it. A payload that binds the path on one line and
    writes through the binding on another therefore satisfied neither."""
    for body in _stdin_program_heredoc_bodies(cmd):
        for match in _PAYLOAD_ASSIGN_RE.finditer(body):
            name = match.group(1)
            value = (
                match.group(3) or match.group(5) or match.group(6) or ""
            ).strip().replace("\\", "/")
            if " " in value:
                continue
            if not _mentions_governed_identifier(value.rsplit("/", 1)[-1]):
                continue
            deref = re.compile(
                r"open\s*\(\s*%s\s*,\s*['\"][wax]"
                r"|%s\s*,\s*['\"][wax]"
                r"|%s\s*\)\s*\.\s*write"
                r"|>\s*\$?\{?%s\}?\b" % ((re.escape(name),) * 4)
            )
            if deref.search(body):
                return True
    return False


def _copy_command_substitution(text: str, start: int) -> "tuple[str, int]":
    n = len(text)
    if text[start] == "`":
        end = start + 1
        while end < n and text[end] != "`":
            end += 1
        end = min(end + 1, n)
        return text[start:end], end - start
    depth = 0
    i = start
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return text[start:i], i - start


def _strip_quoted_spans(text: str) -> str:
    out: "list[str]" = []
    quote: "str | None" = None
    prev = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote and prev != "\\":
                quote = None
                out.append(ch)
                prev = ch
                i += 1
                continue
            if ch == "`" or text[i : i + 2] == "$(":
                span, consumed = _copy_command_substitution(text, i)
                out.append(span)
                prev = span[-1] if span else prev
                i += consumed
                continue
            prev = ch
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev = ch
            i += 1
            continue
        out.append(ch)
        prev = ch
        i += 1
    return "".join(out)


_GIT_CONTENT_SAFE_SUBCOMMANDS = frozenset(
    {
        "commit",
        "add",
        "log",
        "show",
        "diff",
        "status",
        "cat-file",
        "tag",
        "notes",
        "rev-parse",
        "ls-files",
        "blame",
    }
)

_GIT_CONTENT_MUTATING_SUBCOMMANDS = frozenset(
    {
        "checkout",
        "restore",
        "apply",
        "reset",
        "clean",
        "rm",
        "mv",
        "stash",
        "switch",
        "revert",
        "cherry-pick",
        "merge",
        "rebase",
        "pull",
    }
)

_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _git_subcommand(segment: str) -> "str | None":
    tokens = segment.split()
    idx = 0
    while idx < len(tokens) and tokens[idx] != "git":
        if not _ASSIGN_RE.match(tokens[idx]):
            return None
        idx += 1
    if idx >= len(tokens):
        return None
    idx += 1
    while idx < len(tokens):
        token = tokens[idx]
        if not token.startswith("-"):
            return token
        if token in _GIT_VALUE_OPTS:
            idx += 2
            continue
        idx += 1
    return None


#: module docstring point 7's "SCOPED-COMMIT WRAPPER RECOGNITION". Exact-
_COMMIT_WRAPPER_BASENAMES = frozenset(
    {
        "scoped-git-commit",
        "coordinator-safe-commit",
        "spinoff-deliverable-and-commit",
    }
)

_CMD_SUFFIX_RE = re.compile(r"\.cmd$", re.IGNORECASE)


def _segment_command_token(segment: str) -> "str | None":
    tokens = segment.split()
    for token in tokens:
        if _ASSIGN_RE.match(token):
            continue
        return token
    return None


def _command_token_basename(token: str) -> str:
    """The final `/`-separated component of `token`, with a single layer of
    surrounding quotes stripped first -- so
    `"$COORDINATOR_SETTINGS_HOME/bin/scoped-git-commit"` and
    `"${VAR:-default}/bin/scoped-git-commit"` both resolve to
    `scoped-git-commit` regardless of what the variable expansion itself
    looks like, since only the text after the LAST `/` is ever the binary
    name."""
    stripped = token.strip("'\"")
    return stripped.rsplit("/", 1)[-1]


def _is_commit_wrapper_command(segment: str) -> bool:
    """True iff `segment`'s command token is one of
    `_COMMIT_WRAPPER_BASENAMES`, invoked bare, by absolute/relative path,
    via a `$VAR`/`${VAR:-default}` prefix, or with a case-insensitive
    `.cmd` suffix."""
    token = _segment_command_token(segment)
    if token is None:
        return False
    basename = _command_token_basename(token)
    basename = _CMD_SUFFIX_RE.sub("", basename)
    return basename.lower() in _COMMIT_WRAPPER_BASENAMES


def _is_commit_wrapper_read_shape(segment: str) -> bool:
    if not _is_commit_wrapper_command(segment):
        return False
    without_heredocs = _strip_heredoc_bodies(segment)
    if _has_write_marker(_strip_quoted_spans(without_heredocs)):
        return False
    return not _has_code_execution_marker(without_heredocs)


def _is_git_content_mutation(segment: str) -> bool:
    return _git_subcommand(segment) in _GIT_CONTENT_MUTATING_SUBCOMMANDS


def _is_git_read_shape(segment: str) -> bool:
    if _git_subcommand(segment) not in _GIT_CONTENT_SAFE_SUBCOMMANDS:
        return False
    without_heredocs = _strip_heredoc_bodies(segment)
    if _has_write_marker(_strip_quoted_spans(without_heredocs)):
        return False
    return not _has_code_execution_marker(without_heredocs)


_CLAUDE_MD_GRANT_MODULE = "coordinator_core.session.claude_md_grant"

_PYTHON_BASENAMES = frozenset({"python", "python3"})


def _python_dash_m_module(segment: str) -> "str | None":
    tokens = segment.split()
    idx = 0
    while idx < len(tokens) and _ASSIGN_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None
    if _command_token_basename(tokens[idx]) not in _PYTHON_BASENAMES:
        return None
    idx += 1
    while idx < len(tokens):
        if tokens[idx] == "-m" and idx + 1 < len(tokens):
            return tokens[idx + 1].strip("'\"")
        idx += 1
    return None


def _is_claude_md_grant_invocation(segment: str) -> bool:
    return _python_dash_m_module(segment) == _CLAUDE_MD_GRANT_MODULE


def _is_claude_md_grant_read_shape(segment: str) -> bool:
    if not _is_claude_md_grant_invocation(segment):
        return False
    without_heredocs = _strip_heredoc_bodies(segment)
    return not _has_write_marker(_strip_quoted_spans(without_heredocs))


_ASSIGN_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=")


def _split_top_level_segments(cmd: str) -> "list[str]":
    segments: "list[str]" = []
    current: "list[str]" = []
    quote: "str | None" = None
    paren_depth = 0
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            current.append(ch)
            if ch == quote and cmd[i - 1] != "\\":
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            current.append(ch)
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            current.append(ch)
            i += 1
            continue
        if paren_depth == 0:
            if cmd[i : i + 2] in ("&&", "||"):
                segments.append("".join(current))
                current = []
                i += 2
                continue
            if ch in (";", "|", "\n"):
                segments.append("".join(current))
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _has_var_assignment_indirection(segments: "list[str]") -> bool:
    for segment in segments:
        if _ASSIGN_RE.match(segment) and _mentions_governed_identifier(segment):
            return True
    return False


_ASSIGN_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=")
_VAR_DEREF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _governed_bound_variables(segments: "list[str]") -> "set[str]":
    bound: "set[str]" = set()
    for _ in range(len(segments) + 1):
        changed = False
        for segment in segments:
            match = _ASSIGN_NAME_RE.match(segment)
            if not match:
                continue
            name = match.group(1)
            if name in bound:
                continue
            value = segment[match.end() :]
            if _mentions_governed_identifier(segment) or any(
                deref in bound for deref in _VAR_DEREF_RE.findall(value)
            ):
                bound.add(name)
                changed = True
        if not changed:
            break
    return bound


def _assignment_indirection_reaches_a_write(segments: "list[str]") -> bool:
    """Point 4's by-SINK narrowing: a REDIRECT counts as evidence of a
    governed write only when its own target names one -- when the target
    dereferences a variable bound to a governed path (`> $p`, `>
    "${p}"`) or names a governed identifier outright. FAIL-CLOSED
    everywhere else, deliberately -- a segment carrying any OTHER write
    marker (`tee`, `cp`/`mv`, `sed -i`, an interpreter payload, `xargs`)
    keeps point 4's original broad behaviour, because this hook cannot
    cheaply tell `tee $p` from `tee /tmp/x`."""
    bound = _governed_bound_variables(segments)
    for segment in segments:
        if not _has_write_marker(segment):
            continue
        if not _has_redirect_marker(segment):
            return True
        without_redirect = _BARE_REDIRECT_RE.sub(
            " ", _SAFE_REDIRECT_RE.sub(" ", segment)
        )
        if _has_write_marker(without_redirect):
            return True
        target = _redirect_target_token(segment)
        if not target:
            return True
        if _mentions_governed_identifier(target):
            return True
        if any(deref in bound for deref in _VAR_DEREF_RE.findall(target)):
            return True
    return False


def _has_xargs_pipe_indirection(segments: "list[str]") -> bool:
    """Point 10: `xargs` reads the governed identifier from stdin AT
    RUNTIME and substitutes it into a DIFFERENT segment's argument -- the
    segment carrying the actual write marker or interpreter never contains
    the literal identifier string, so `_mentions_governed_identifier` is
    False for that segment and it is skipped before any marker is even
    checked. Mirrors point 4's WHOLE-COMMAND scope: any segment containing
    bare `xargs` in a command that mentions a governed identifier ANYWHERE
    is denied outright -- deliberate over-denial in the safe direction."""
    if not any(_mentions_governed_identifier(segment) for segment in segments):
        return False
    return any(_XARGS_RE.search(segment) for segment in segments)


_OS_EXEC_RE = re.compile(
    r"\bos\.system\(|\bsubprocess\.(run|call|Popen|check_call|check_output)\("
    r"|shell\s*=\s*True|\beval\(|\bexec\("
)


def _has_os_exec_marker(text: str) -> bool:
    return bool(_OS_EXEC_RE.search(text))


_OPEN_CALL_RE = re.compile(r"\bopen\s*\(")
_WRITE_MODE_RE = re.compile(r"['\"][^'\"]*[wax][^'\"]*['\"]")
_LITERAL_FIRST_ARG_RE = re.compile(r"\A\s*(['\"])(?P<path>[^'\"]*)\1\s*(?:,|\Z)")
_TRAILING_WRITE_CALL_RE = re.compile(r"\A\s*\.\s*write(?:_text|_bytes)?\s*\(")


def _open_call_spans(segment: str) -> "list[tuple[int, int, str]]":
    spans: "list[tuple[int, int, str]]" = []
    for match in _OPEN_CALL_RE.finditer(segment):
        depth = 0
        i = match.end() - 1
        while i < len(segment):
            if segment[i] == "(":
                depth += 1
            elif segment[i] == ")":
                depth -= 1
                if depth == 0:
                    spans.append((match.start(), i + 1, segment[match.end() : i]))
                    break
            i += 1
    return spans


def _interpreter_write_sinks_are_ungoverned(segment: str) -> bool:
    """True only when EVERY write in an interpreter payload is an
    analysable literal-path `open()` in a write mode and none of those
    paths names a governed surface. FAIL-CLOSED everywhere else: only the
    adjacent `open(<literal>, <write mode>)` shape -- optionally chained
    into `.write()`/`.write_text()`/`.write_bytes()` -- is analysed. No
    bindings, no receiver walk, no dataflow -- analysing less is what
    makes this sound. READS ARE NOT SINKS: a read-mode `open('<governed>')`
    is left standing on purpose."""
    blanked = list(segment)
    analysable = False
    for start, end, args in _open_call_spans(segment):
        if "," not in args or not _WRITE_MODE_RE.search(args[args.find(",") + 1 :]):
            continue
        literal = _LITERAL_FIRST_ARG_RE.match(args)
        if literal is None:
            return False
        if _mentions_governed_identifier(literal.group("path")):
            return False
        analysable = True
        stop = end
        chained = _TRAILING_WRITE_CALL_RE.match(segment[end:])
        if chained is not None:
            stop = end + chained.end()
        for i in range(start, stop):
            blanked[i] = " "
    if not analysable:
        return False
    return not _has_write_marker("".join(blanked))


def _is_interpreter_read_shape(segment: str) -> bool:
    token = _segment_command_token(segment)
    if token is None:
        return False
    if _command_token_basename(token) not in _PYTHON_BASENAMES and _command_token_basename(
        token
    ) not in ("perl", "ruby", "node"):
        return False
    if _python_dash_m_module(segment) is not None:
        return False
    if _has_write_marker(segment) and not _interpreter_write_sinks_are_ungoverned(segment):
        return False
    if _has_os_exec_marker(segment):
        return False
    if _EVAL_RE.search(segment) or _XARGS_RE.search(segment):
        return False
    return not _mentions_governed_identifier(_strip_quoted_spans(segment))


def is_denied_bash_write(cmd: str) -> bool:
    if not _mentions_governed_identifier(cmd):
        return False

    segments = _split_top_level_segments(cmd)

    # `_ASSIGN_RE` matches its `add =` prefix as though it were a live
    stripped_cmd = _strip_heredoc_bodies(cmd)
    stripped_segments = _split_top_level_segments(stripped_cmd)
    if _has_var_assignment_indirection(
        stripped_segments
    ) and _assignment_indirection_reaches_a_write(stripped_segments):
        return True

    if _has_stdin_program_var_write(cmd):
        return True

    if _has_xargs_pipe_indirection(segments):
        return True

    for segment in segments:
        if not _mentions_governed_identifier(segment):
            continue
        if _is_git_content_mutation(segment):
            return True
        if (
            _is_git_read_shape(segment)
            or _is_commit_wrapper_read_shape(segment)
            or _is_claude_md_grant_read_shape(segment)
            or _is_interpreter_read_shape(segment)
        ):
            continue
        if _has_write_marker_for_point3(segment) or _has_indirection_marker(segment):
            return True

    return False


def _looks_commit_shaped(cmd: str) -> bool:
    for segment in _split_top_level_segments(cmd):
        if _git_subcommand(segment) in ("commit", "add"):
            return True
        if _is_commit_wrapper_command(segment):
            return True
    return False


def _looks_quoted_content_shaped(cmd: str) -> bool:
    mentioning_segments = [
        segment
        for segment in _split_top_level_segments(cmd)
        if _mentions_governed_identifier(segment)
    ]
    if not mentioning_segments:
        return False
    for segment in mentioning_segments:
        target = _redirect_target_token(segment)
        if target and _mentions_governed_identifier(target):
            return False
        without_redirect = _BARE_REDIRECT_RE.sub(
            " ", _SAFE_REDIRECT_RE.sub(" ", segment)
        )
        if any(
            pattern.search(without_redirect)
            for pattern in (
                _CP_MV_RE,
                _TEE_RE,
                _SED_INPLACE_RE,
                _PERL_INPLACE_RE,
                _PATCH_RSYNC_RE,
            )
        ):
            return False
    return all(
        not _mentions_governed_identifier(_strip_quoted_spans(segment))
        for segment in mentioning_segments
    )


def _compose_deny_message(*, commit_shaped: bool = False, quoted_content_shaped: bool = False):
    if commit_shaped:
        prose = (
            "BLOCKED: this looks commit-shaped, but a write marker sits "
            "outside the message. Use the pathspec form, not Write/Edit."
        )
    elif quoted_content_shaped:
        prose = (
            "BLOCKED: the governed name is quoted content, not a write "
            "target. Edit the real destination; see "
            "findings-self-persist-sentinel.md."
        )
    else:
        prose = "BLOCKED: writes a governed doctrine surface. If real target is one of the four, use Write/Edit."
    return compose(prose, anchor=_WIKI_ANCHOR)


def evaluate(payload: dict):
    if not isinstance(payload, dict):
        return None
    if payload.get("tool_name") not in _COMMAND_TOOL_NAMES:
        return None

    tool_input = payload.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd:
        return None

    if not is_denied_bash_write(cmd):
        return None

    return _compose_deny_message(
        commit_shaped=_looks_commit_shaped(cmd),
        quoted_content_shaped=_looks_quoted_content_shaped(cmd),
    )


@register_op("hooks.guard_doctrine_surface_bash_write")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Bash|PowerShell) op: deny a Bash/PowerShell command whose
    SINK writes a `_claude_md_ledger.GOVERNED_AUTHORING_SURFACES` file,
    closing the Bash escape from the Write/Edit/MultiEdit-only C7 admission
    gate."""
    params = payload_of(params)
    message = evaluate(params)
    if message is None:
        return no_advisory()
    return deny("PreToolUse", render(message, env=params.get("env")))
