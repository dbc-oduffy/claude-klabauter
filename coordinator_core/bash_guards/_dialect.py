"""coordinator_core.bash_guards._dialect -- a dialect-aware tokenizer seam
behind the EXISTING `_command_tokenizer.py` contract (C2 of
`docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-silent.md`).

Problem this module exists to close: `_command_tokenizer.tokenize_full_command`
lexes with `shlex(posix=True)`, a correct model of `sh` and a structurally
wrong one for PowerShell (see that module's own docstring and the spike
verdict record this plan cites). Feeding it PowerShell input either raises
inside `shlex` or silently produces garbage tokens (a Windows path loses its
separators -- see `state/audits/2026-08-07-bash-guard-tokenizer-eats-
windows-path-separators.md`, and this module's own "Bash-leg escape defect"
section below for why that specific mangling is NOT this chunk's to fix).

MANDATED SHAPE -- dialect is a PARAMETER, never inferred (plan Anti-scope:
"do not build a dialect detector"; `git push --force` is byte-identical in
both dialects, so detection is undecidable on exactly the commands that
matter most). `dialect_from_tool_name` is the one recognized carry path --
`payload["tool_name"]`, already present in every guard's payload (confirmed
on `block_reviewer_bash_outside_allowlist.check` and
`block_subagent_destructive_action.check`) -- and it returns `None` (never a
bash default) for any `tool_name` this module does not recognize. No
function in this module inspects a command STRING to guess which dialect it
is written in.

OUTPUT SHAPE -- deliberately NOT a new segmentation abstraction. The
PowerShell tokenizer below (`_powershell_tokens`) emits a `List[str]`
token stream shaped so it can be handed DIRECTLY to
`_command_tokenizer.segments_from_tokens_with_pipe_flag` /
`segments_from_tokens_simple` UNCHANGED: a PowerShell `;` statement
separator becomes the literal token `";"`, and a leading `&` call-operator
becomes the literal token `"&"` -- both already recognized separator
punctuation by `_command_tokenizer._SEPARATOR_TOKEN_RE`. This is what lets
`tokenize_command`'s callers (C4/C5's per-guard conversions) reuse the
EXISTING segmentation function bodies without their own edits: only the
tokenize call at the front of a guard's parse needs to become dialect-aware,
never the segmentation call after it. `token_matches_binary` and
`normalize_executable_basename` also apply unchanged to a PowerShell token,
since both operate on a plain token string, not on dialect-specific syntax.

Grammar package: `tree-sitter-pwsh` (wharflab), NOT `tree-sitter-powershell`
(Airbus CERT) -- see "Package head-to-head" below for the measured reason.
Import of `tree_sitter`/`tree_sitter_pwsh` is LAZY, confined to
`_parser()` below, so a Bash-dialect (the ~99% case) or an absent-dialect
call pays zero import cost -- see "Cold-import cost" below and C8's own
lazy-import mandate, which this module's structure exists to satisfy ahead
of C8 landing the dependency declaration.

Package head-to-head (10-minute evaluation mandated by this chunk's body,
measured 2026-08-07 on this box, `tree-sitter` 0.25.2): fed both packages
the plan's own named grammar-gap case, a backtick-escaped paren in argument
mode (`Write-Output `(hello`)`):

    airbus (tree-sitter-powershell 0.26.4):  root_node.has_error == True
      -- the paren characters fall OUTSIDE the `pipeline` node entirely, as
      two separate top-level `ERROR` children; this is the "backtick-
      escaped parens in argument mode yield has_error=True" gap the plan's
      body names explicitly, confirmed independently on this measurement.
    wharflab (tree-sitter-pwsh 0.38.1):      root_node.has_error == False
      -- the whole `` `(hello`) `` span parses cleanly as a single
      `escape_character` node inside `command_elements`.

wharflab parses the plan's own named gap case correctly where Airbus does
not, so wharflab is the package this module uses, DESPITE Airbus's being
the one already smoke-tested in the spike (this chunk's body explicitly
warns against skipping the head-to-head for that reason). Per the plan's
own lineage note, this is not two independent implementations converging
by chance -- wharflab's contributor list includes Airbus CERT's own handle
(`citronneur`) alongside `tinovyatkin`, i.e. a disconnected derivative of
the same grammar that has since diverged and, on this case, closed a gap
the ancestor still has. `cmd &> out.txt` (the plan's SECOND named gap) was
independently re-verified against wharflab alone (Airbus was not
re-checked for this second case, since the head-to-head above already
settled which package this module uses): `root_node.has_error == True`,
correctly routing to SILENT via the `has_error` check in `_parse_powershell`
below -- confirmed NOT a silent mis-split (the `&` is a top-level `ERROR`
child, not absorbed into a `redirection` node), consistent with the plan's
own note that this shape is genuinely ambiguous between "combine-redirect"
and "background + new command" and must not be guessed.

`py-tree-sitter` version constraint for C8 to pin (not this chunk's job to
declare, per the plan's C2/C8 split -- this is the constraint C8 must
provision): `tree-sitter` at 0.21 or above and below 1, for the SAME reason
the plan's own body
names -- 0.21+ replaced `Parser.set_language(LANG)` with `Parser(LANG)` /
`parser.language = LANG`, and the two-arg `Language(path, name)`
constructor with the single-arg `Language(ptr)` form. This module uses ONLY
the 0.21+ spellings (`Parser(lang)`, `Language(mod.language())`) --
pinning below 0.21 would break `_parser()` outright, and the plan's own cited
history says an unpinned upper bound is exactly how the pattern breaks
again on some future major release C8 has not evaluated. Measured on this
box at `tree-sitter==0.25.2`.

Cold-import cost (C8's REQUIRED sub-measurement, done here since it gates
this module's own lazy-import design, not asserted): `import tree_sitter`
+ `Language(tsp2.language())` + `Parser(lang)`, cold subprocess, three runs
on this box (Windows, Git for Windows): 17.2ms / 17.6ms / 17.7ms. Small in
absolute terms but NOT charged to the ~99% of Bash-tool dispatches that
never reach this path, because the import is lazy -- see `_parser()`.

Bash-leg escape defect (carried obligation from
`state/audits/2026-08-07-bash-guard-tokenizer-eats-windows-path-separators.md`,
not this chunk's to fix): that audit found `_command_tokenizer
.tokenize_full_command`'s `shlex(posix=True)` ALSO mangles a Windows path on
the BASH leg (a Windows path such as ``C:`` + backslash + ``Users`` + backslash +
``example-operator`` + backslash + ``foreign`` losing its separators), because
<!-- abs-path-ok: verbatim quote of that audit's own measured tokenizer input/output pair -->
`shlex` under `posix=True` treats backslash as an escape character, which is
correct for real POSIX shells and wrong for a Windows-path-bearing string
typed at a Windows-first agent seam. DISPOSITION: documented-why-not-fixed-
here, not handled. This module's `dialect_from_tool_name` and BASH branch
(`resolve_segments_for_dialect`) delegate to `_command_tokenizer
.tokenize_full_command` UNCHANGED -- no edit to that function or its
`shlex` construction. Reasons, both independently sufficient: (1) that
audit's own § "Ownership" is explicit that the shared `_command_tokenizer`
module is claimed and in flight by another session/owner, and a second
concurrent change to the same `shlex` construction under an active owner is
how two sessions produce one incoherent guard set -- exactly what this
plan's own Anti-scope forbids for `dispatch.py` and, by the same reasoning
this audit itself applies, for `_command_tokenizer.py`'s bash-leg lexer
construction too. (2) This chunk's OWN surface is `_dialect.py`, not
`_command_tokenizer.py` -- widening scope to also patch the bash leg would
be exactly the "the wave-map author works out the partition" style
scope-creep this repo's north star exists to rule out. The tradeoff itself
(disable `shlex.escape`, which is byte-identical on every non-backslash
shape tried but changes `find ... -exec rm {} \\;`) is fully documented at
that audit's own § "The fix is a real tradeoff" and requires no restatement
here; a future PowerShell-dialect caller of this module's BASH branch
inherits the SAME pre-existing bash-leg mangling every other bash-dialect
caller of `tokenize_full_command` already has today -- this module makes
that inheritance no worse and no better.

Required segmentation/tokenization test cases (measured returning `None` in
the spike verdict record, Table 1; see `tests/test_command_tokenizer_length
_ceiling.py`'s `TestDialectAwareTokenizer` for the pinned assertions):
backtick line-continuation, the `;`-chain form, and the `&` call-operator
form -- all three route through `_powershell_tokens` -> the EXISTING
`_command_tokenizer.segments_from_tokens_with_pipe_flag` unchanged.

NEGATIVE SPEC: this module does not edit `dispatch.py` (Anti-scope), does
not add a dialect detector (Anti-scope), and does not change a single
guard's own body (that is C4/C5's job) -- it only builds the capability
those chunks wire in.

DURABLE OBSERVABILITY FOR THE ImportError BRANCH (residual fix, this
session): `_powershell_tokens`'s three SILENT-routing cases are NOT
equivalent. `has_error=True` and the generic-exception branch are both
per-command grammar residue -- expected, and correctly silent-only, exactly
like every other guard's `record_silent` call, because `record_silent` is a
no-op unless a caller opens `collecting()` (see `_verdict.py`'s own
docstring: "inert in production ... observable only to a caller that
deliberately opens it"), and `dispatch.py` never opens one (Anti-scope).
The ImportError branch is different in kind: it is not a property of any
one command, it is a property of the INSTALL -- `tree-sitter`/
`tree-sitter-pwsh` missing disables PowerShell command classification for
EVERY guard, machine-wide, and with only `record_silent`, that fires with
zero observable signal in production. So the ImportError branch alone also
writes a durable, out-of-band record (`_log_dialect_parser_unavailable`,
mirroring `block_subagent_destructive_action._log_fail_open`'s settings-
home-rooted, never-raise, best-effort append) -- `record_silent` is kept
alongside it unchanged (tests still rely on the SILENT channel), and no
allow/deny verdict changes as a result of either.

Spec backlink: pln-guards-reach-a-verdict-on-powe-0e4bc3 § C2
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional

from . import _command_tokenizer
from ._verdict import record_silent
from coordinator_core._settings_home import settings_home

# Generator-provenance declaration (generator_provenance.py).
# _log_dialect_parser_unavailable appends to
# settings_home()/state/dialect-parser-unavailable.log -- settings-home
# rooted, never a tracked claude-klabauter repo artifact.
GENERATES = []

__all__ = [
    "Dialect",
    "dialect_from_tool_name",
    "tokenize_command",
    "resolve_segments_for_dialect",
    "expand_start_process_invocations",
]


class Dialect(Enum):
    """The two dialects this module recognizes. Deliberately NOT open-ended
    -- an unrecognized `tool_name` maps to `None` (see
    `dialect_from_tool_name`), not to a third enum member, so "unrecognized"
    stays a distinct, un-forgeable state from "recognized as X"."""

    BASH = "bash"
    POWERSHELL = "powershell"


#: The one recognized carry path (AC2): `payload["tool_name"]` -> `Dialect`.
#: `"PowerShell"` IS live as of 2026-08-07: the conversion plan named below
#: landed its C1/C2/C3, so a `PowerShell` `tool_name` now clears the master
#: gate and reaches every guard whose own `MATCHERS` declares it. This entry
#: is no longer forward-declared -- it carries real traffic. Absent from this
#: table == unrecognized == `None`, never a bash default (Anti-scope: "do not
#: let an absent dialect default to bash silently").
#:
#: SUPERSEDED 2026-08-07 -- this comment previously read that `"PowerShell"`
#: was "NOT yet emitted by any tool-name site in this repo", naming
#: `docs/plans/2026-08-07-command-guards-fire-under-both-tool-names.md` as
#: what would change that. It did. The stale sentence is recorded here rather
#: than silently dropped because it was scoped to *guard payloads inside this
#: repo* and was read once, the same day, as a claim about *harness
#: transcripts* -- which it never was, and which measurement refutes outright
#: (1,753 of 9,665 shell-tool invocations across 1,476 local transcripts
#: arrive as `PowerShell`). If you are reaching for this comment as evidence
#: that some surface never sees PowerShell, it does not say that and never
#: did.
_TOOL_NAME_TO_DIALECT = {
    "Bash": Dialect.BASH,
    "PowerShell": Dialect.POWERSHELL,
}


def dialect_from_tool_name(tool_name: Optional[str]) -> Optional[Dialect]:
    """Return the `Dialect` carried by `tool_name`, or `None` if absent or
    unrecognized. This is the ONLY place this module reads a dialect from --
    never from the command string itself (see module docstring, Anti-scope).
    """
    if not tool_name:
        return None
    return _TOOL_NAME_TO_DIALECT.get(tool_name)


# ---------------------------------------------------------------------------
# PowerShell tokenizer (tree-sitter-pwsh / wharflab -- see module docstring
# "Package head-to-head" for why).
# ---------------------------------------------------------------------------

#: Node types whose FULL TEXT is taken as a single token without descending
#: into their own named children, even though they have some. These are
#: composite-argument shapes where descending would split what a guard must
#: see as ONE argument token into pieces -- most concretely, an
#: environment-variable-expansion path argument
#: (`$env:TEMP\scratch-target`) parses as `expandable_bareword` wrapping a
#: `variable` child (`$env:TEMP`) and a `generic_token` child
#: (`\scratch-target`); descending would silently drop the env-var prefix
#: from the argument token C4's `$env:` target-recognition case (see the
#: plan's C4 body) needs to match against. Confirmed by direct parse on this
#: box (2026-08-07): `Remove-Item -Recurse -Force $env:TEMP\scratch-target`
#: tokenizes to `['Remove-Item', '-Recurse', '-Force',
#: '$env:TEMP\\scratch-target']` with this set, one whole token for the
#: target -- not two.
_ATOMIC_ARGUMENT_NODE_TYPES = frozenset({
    "expandable_bareword",
    "expandable_string_literal",
    "expandable_here_string_literal",
})

_parser_cache = None


# ---------------------------------------------------------------------------
# DURABLE ImportError OBSERVABILITY (OBSERVABILITY ONLY -- it never changes
# an allow/deny verdict). Mirrors `block_subagent_destructive_action.py`'s
# `_FAIL_OPEN_LOG_RELPATH` / `_log_fail_open` precedent: settings-home-
# rooted (machine-scoped, like that log -- this is about the INSTALL, not
# any one target repo), best-effort append, NEVER raises. A separate file
# from that guard's own log (not folded in) for the same reason that log
# gives for staying separate from ITS sibling: different grammar/verbs, a
# different failure class (grammar-package absence vs. identity-resolution
# fail-open).
#
# Deliberately NOT gated behind `_parser_cache` for de-duplication: unlike
# the SUCCESS path (`_parser()` sets `_parser_cache` once and reuses it),
# an ImportError is raised BEFORE `_parser_cache` is ever assigned, so this
# branch re-raises and re-enters on every single PowerShell-dialect call for
# the life of the process, not once (verified by reading `_parser()` below --
# do not assume otherwise). Left unbounded per-call would make a broken
# install's log grow once per PowerShell command; `_LOGGED_PARSER_UNAVAILABLE`
# below bounds it to one durable write per process, matching this module's
# own "record once per process, not once per call" intent while still
# guaranteeing the FIRST occurrence -- the one that matters for detection --
# is never lost.
# ---------------------------------------------------------------------------
_DIALECT_PARSER_UNAVAILABLE_LOG_RELPATH = ("state", "dialect-parser-unavailable.log")
_LOGGED_PARSER_UNAVAILABLE = False


def _dialect_parser_unavailable_log_path() -> Path:
    """Settings-home-rooted path for the durable ImportError record -- see
    module-level comment above for why this mirrors, but does not share,
    `block_subagent_destructive_action._fail_open_log_path`."""
    return settings_home() / Path(*_DIALECT_PARSER_UNAVAILABLE_LOG_RELPATH)


def _log_dialect_parser_unavailable(guard_name: str, reason: str) -> None:
    """Best-effort, once-per-process durable append recording that the
    PowerShell grammar package could not be imported -- i.e. PowerShell
    command classification is disabled machine-wide, not merely undecided
    for one command. NEVER raises (mirrors `_log_fail_open`'s own
    never-raise contract one layer up: a guard hot path must never fail
    because its observability write failed).

    OBSERVABILITY ONLY -- it never changes an allow/deny verdict.

    Agent-facing register (`docs/wiki/guard-messaging.md` § Register): the
    remedy line names `/coordinator:install`, never an override key.
    """
    global _LOGGED_PARSER_UNAVAILABLE
    if _LOGGED_PARSER_UNAVAILABLE:
        return
    try:
        log_path = _dialect_parser_unavailable_log_path()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (
            f"[{timestamp}] PARSER-UNAVAILABLE guard={guard_name!r} "
            f"reason={reason!r} remedy='/coordinator:install rebuilds the venv'\n"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        _LOGGED_PARSER_UNAVAILABLE = True
    except Exception:  # noqa: BLE001 -- observability must never raise into a guard
        pass


def _parser():
    """Lazily construct and cache the `tree-sitter-pwsh` parser. `tree_sitter`
    and `tree_sitter_pwsh` are imported HERE, not at module load time --
    C8's mandate (and this module's own "Cold-import cost" docstring
    section) is that a Bash-dialect dispatch pays zero import cost for a
    package the ~99% of Bash calls never touch. Raises `ImportError` if the
    package is absent -- callers (`_parse_powershell`) catch it and record
    SILENT rather than letting it propagate into a guard's hot path.
    """
    global _parser_cache
    if _parser_cache is None:
        from tree_sitter import Language, Parser
        import tree_sitter_pwsh

        lang = Language(tree_sitter_pwsh.language())
        _parser_cache = Parser(lang)
    return _parser_cache


def _parse_powershell(cmd_text: str):
    """Parse `cmd_text` as PowerShell. Returns the tree, or `None` if the
    package cannot be imported (ImportError) or the source is empty. Does
    NOT itself check `has_error` -- callers do, since "parsed with errors"
    and "could not parse at all" are recorded with different reasons.
    """
    parser = _parser()
    return parser.parse(cmd_text.encode("utf-8", errors="surrogateescape"))


def _flatten_powershell_tokens(node, src: bytes) -> List[str]:
    """Walk a `tree-sitter-pwsh` parse tree and emit a flat `List[str]`
    token stream shaped for `_command_tokenizer.segments_from_tokens_*`
    (see module docstring "Output shape").

    - `empty_statement` (a bare `;` between two statements) emits the
      literal token `";"` -- the existing bash-shaped segmenter already
      treats this as an always-separate punctuation token.
    - A node in `_ATOMIC_ARGUMENT_NODE_TYPES` emits its own full source
      span as ONE token, without descending into its named children (see
      that set's own comment).
    - A true leaf (no children at all, named or unnamed) emits its own
      source span as one token, UNLESS that span is pure whitespace -- this
      is what makes the backtick-line-continuation case a no-op with no
      special-casing: the grammar already folds `` `<newline><indent> ``
      into an unnamed whitespace-only child of `command_argument_sep`, so
      the general "whitespace produces no token" rule already drops it
      (confirmed by direct parse: `Remove-Item `+"`"+`\\n  -Recurse -Force
      C:\\scratch` tokenizes to `['Remove-Item', '-Recurse', '-Force',
      'C:\\scratch']`, the continuation contributing nothing).
    - Any other node (a structural container -- `program`, `statement_list`,
      `pipeline`, `pipeline_chain`, `command`, `command_elements`,
      `command_invocation_operator`, etc.) recurses through ALL of its
      children (both named and unnamed) in source order. Recursing through
      UNNAMED children too is what makes a bare `&` call-operator prefix
      (an unnamed child of `command_invocation_operator`) surface as its
      own literal `"&"` token, matching the existing bash separator-token
      shape (`_command_tokenizer._SEPARATOR_TOKEN_RE`) so
      `segments_from_tokens_with_pipe_flag` drops it as a leading no-op
      separator with no special-casing here either (confirmed: `&
      Remove-Item -Recurse -Force C:\\scratch` tokenizes to `['&',
      'Remove-Item', '-Recurse', '-Force', 'C:\\scratch']`, and segmenting
      that yields one segment, `['Remove-Item', '-Recurse', '-Force',
      'C:\\scratch']`, with the leading `&` consumed as an empty-current
      separator).
    """
    if node.type == "empty_statement":
        return [";"]
    if node.type in _ATOMIC_ARGUMENT_NODE_TYPES:
        text = src[node.start_byte:node.end_byte].decode("utf-8", errors="surrogateescape")
        return [text] if text.strip() else []
    if len(node.children) == 0:
        text = src[node.start_byte:node.end_byte].decode("utf-8", errors="surrogateescape")
        return [text] if text.strip() else []
    tokens: List[str] = []
    for child in node.children:
        tokens.extend(_flatten_powershell_tokens(child, src))
    return tokens


def _powershell_tokens(cmd_text: str, *, guard_name: str) -> Optional[List[str]]:
    """Tokenize `cmd_text` as PowerShell, or return `None` (recording
    SILENT for `guard_name`) when it cannot be reached at all.

    Three SILENT-routing cases, per the plan's own mandate that
    `root_node.has_error` is a guard-relevant signal in its own right:

    1. `tree_sitter`/`tree_sitter_pwsh` is not importable (matches C8's
       required ImportError -> SILENT fallback, exercised ahead of C8
       landing the dependency declaration).
    2. `root_node.has_error` is `True` -- the plan's own two named grammar
       gaps land here: backtick-escaped parens in argument mode (closed by
       the wharflab package per the head-to-head above, listed here for
       completeness since a FUTURE grammar release could reopen it) and
       `cmd &> out.txt` (confirmed still `has_error=True` under wharflab,
       see module docstring).
    3. Any other parse-time exception -- never allowed to propagate into a
       guard's hot path (mirrors `record_silent`'s own "never raises"
       contract one layer up).
    """
    try:
        tree = _parse_powershell(cmd_text)
    except ImportError as exc:
        reason = "tree-sitter-pwsh not importable: %r" % (exc,)
        record_silent(guard_name, reason)
        _log_dialect_parser_unavailable(guard_name, reason)
        return None
    except Exception as exc:  # pragma: no cover - defensive, mirrors record_silent's own contract
        record_silent(guard_name, "PowerShell parse raised %r" % (exc,))
        return None

    if tree.root_node.has_error:
        record_silent(
            guard_name,
            "PowerShell parse has_error=True (unparseable or a known grammar "
            "gap -- see _dialect.py module docstring)",
        )
        return None

    src = cmd_text.encode("utf-8", errors="surrogateescape")
    return _flatten_powershell_tokens(tree.root_node, src)


def tokenize_command(
    cmd_text: str, dialect: Optional[Dialect], *, guard_name: str
) -> Optional[List[str]]:
    """Tokenize `cmd_text` according to `dialect`, returning a
    `List[str]` shaped for `_command_tokenizer.segments_from_tokens_*`
    (BASH) or `None`.

    - `Dialect.BASH` delegates UNCHANGED to
      `_command_tokenizer.tokenize_full_command` -- zero behavior change on
      the bash leg (AC4). Does NOT itself call `record_silent` on a bash
      parse failure: that is pre-existing fail-closed behavior each bash
      guard already owns (some deny on a `None` tokenize result, some do
      not), not a "declined to rule" SILENT this module introduces.
    - `Dialect.POWERSHELL` routes through `_powershell_tokens`, which
      itself records SILENT on any failure (see that function's docstring).
    - `None` (absent/unrecognized dialect) records SILENT for `guard_name`
      and returns `None` -- the Anti-scope-mandated "absent dialect is
      SILENT, never a bash default" behavior.
    """
    if dialect is Dialect.BASH:
        return _command_tokenizer.tokenize_full_command(cmd_text)
    if dialect is Dialect.POWERSHELL:
        return _powershell_tokens(cmd_text, guard_name=guard_name)
    record_silent(guard_name, "no recognized dialect (dialect=%r)" % (dialect,))
    return None


#: `Start-Process`'s two recognized aliases (`saps` -- the built-in cmdlet
#: alias -- and `start`, a cmd.exe-compatibility alias PowerShell also
#: registers). Matched case-insensitively (see `expand_start_process_
#: invocations`'s own lower-casing) since PowerShell cmdlet/alias names are
#: case-insensitive by language design, same convention `_RUNNER_PREFILTER_RE`
#: already documents for `Invoke-Pester`.
_START_PROCESS_NAMES = frozenset({"start-process", "saps", "start"})

#: `-ArgumentList`'s recognized spellings: the full name and PowerShell's
#: unambiguous prefix-abbreviation `-Args` (accepted by the real cmdlet;
#: `-ArgumentList` is the only positional-2 parameter beginning `Ar...`, so
#: `-Args` resolves unambiguously there). Matched case-insensitively.
_ARGUMENT_LIST_FLAGS = frozenset({"-argumentlist", "-args"})

#: `-FilePath`'s recognized spelling (the parameter `Start-Process` itself
#: names its target executable with). Matched case-insensitively.
_FILE_PATH_FLAGS = frozenset({"-filepath"})

#: Separator/statement-boundary tokens this walk must stop consuming
#: argument-list elements at -- the SAME punctuation
#: `_command_tokenizer._SEPARATOR_TOKEN_RE` already treats as always-separate
#: (this module's own "Output shape" section names `;`/`&` as the two forms a
#: PowerShell statement boundary is emitted as).
_STATEMENT_BOUNDARY_TOKENS = frozenset({";", "&", "|"})


def _strip_ps_quotes(token: str) -> str:
    """Strip a leading PowerShell quote from `token`, if present -- the
    PowerShell tokenizer above emits a quoted leaf's full source span
    VERBATIM (quotes included, see `_flatten_powershell_tokens`'s own
    docstring), unlike `shlex`, which strips them during lexing. Guards
    consuming reconstructed argv expect a plain value (`pytest`, not
    `'pytest'`), matching what the real process actually receives as its
    argv element once PowerShell's own quote-parsing removes them.

    Residual of `023d4b63c` (see
    `state/bug-backlog/2026-08-08-unbalanced-or-backtick-escaped-quotes-
    st-aaf1e89862ae.yaml`): the prior version only recognized a token
    wholly wrapped in ONE matching pair (`token[0] == token[-1]`), which a
    naive last-char comparison leaves fooled by two spellings that still
    reach `os.path.isabs()` with a leading quote intact -- the SAME
    fail-open direction `023d4b63c` closed (a foreign-repo write judged
    same-repo and ALLOWED because the leading quote defeats
    `os.path.isabs()`, falling through to `os.path.join(cwd, ...)`):

      (a) an unbalanced/truncated quote -- a target opening with a quote
          that never closes (`token[-1]` is not the matching quote at
          all).
      (b) a backtick-escaped embedded quote inside a double-quoted
          string, e.g. `` "C:\\foo`"bar" `` -- a naive last-char check
          can land on an ESCAPED quote as the presumed closer,
          misreading the token's true bounds.
          # abs-path-ok: illustrative synthetic token text in a
          # docstring, never resolved/executed against a real path.

    This walks forward from the opening quote, honoring backtick as
    PowerShell's escape character (an escaped quote is NOT a terminator;
    backtick is not itself a quote character and is otherwise inert
    outside a double-quoted string, so this walk only special-cases it
    inside one) and PowerShell's doubled-single-quote literal (`''`
    inside a single-quoted string is one literal quote, also not a
    terminator). `023d4b63c`'s own well-formed cases -- balanced double
    quotes, balanced single quotes, unquoted tokens -- are unaffected:
    each still resolves its real closing quote at the token's last
    character and strips identically to before.

    FAIL-OPEN DIRECTION, NOT PRESERVATION (per this bug's own remedy
    framing): when no real closing quote is found at all (case (a)), the
    opening quote is still stripped -- exposing the token's true content
    to `os.path.isabs()` rather than leaving a leading quote that hides
    it. A token that does not OPEN with a quote is returned unchanged
    (no interior quote is ever touched), so a legitimate path containing
    a quote character but not itself quote-wrapped is never corrupted by
    this function -- see this function's own tests for that
    non-mangling case."""
    if len(token) < 2 or token[0] not in ("'", '"'):
        return token
    quote = token[0]
    n = len(token)
    i = 1
    if quote == '"':
        while i < n:
            ch = token[i]
            if ch == "`" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                break
            i += 1
    else:
        while i < n:
            ch = token[i]
            if ch == quote:
                if i + 1 < n and token[i + 1] == quote:
                    i += 2
                    continue
                break
            i += 1

    if i < n and token[i] == quote and i == n - 1:
        inner = token[1:i]
        if quote == '"':
            inner = inner.replace("`\"", "\"")
        else:
            inner = inner.replace("''", "'")
        return inner

    # No real closing quote found at the token's own end -- unbalanced or
    # truncated (case (a)), or a genuine close found mid-token with extra
    # trailing characters after it (not the ends-wrapped shape this
    # function strips). Either way, strip the leading quote alone: this
    # is the fail-open-safe direction -- a bare leading quote is exactly
    # what defeats `os.path.isabs()`, and stripping it can only EXPOSE
    # the token's true content, never hide it further.
    return token[1:]


def expand_start_process_invocations(tokens: List[str]) -> List[str]:
    """Rewrite every `Start-Process`/`saps`/`start` call in `tokens` (a
    PowerShell token stream, as emitted by `_powershell_tokens` --
    `tokenize_command(..., Dialect.POWERSHELL, ...)`) so the LAUNCHED
    command's own argv becomes visible in COMMAND POSITION, exactly as if
    it had been typed directly.

    Problem this closes: `Start-Process python -ArgumentList '-m','pytest'`
    is the idiomatic PowerShell spelling of "run this without blocking my
    prompt" -- exactly what an agent reaches for to background a suite run
    -- and every EXISTING token-consuming guard (this module's own
    docstring: "hides argv from every command guard that inspects tokens,
    not just the suite guard") sees `Start-Process` in command position and
    the real runner (`python`, and its own `-m pytest` argv) buried inside
    a quoted, comma-separated `-ArgumentList` array it never inspects.
    Measured live (2026-08-07, `dispatch.evaluate_payload_json`):
    `check_test_suite_invocation.check()` ALLOWED this shape for a resolved
    subagent, while the byte-identical un-wrapped `python -m pytest` (and
    seven other idioms) correctly DENIED.

    Widens the tokenizer's own OUTPUT, never a guard's verdict (module
    docstring's own "Fail-open stays fail-open, fail-closed stays
    fail-closed" contract, restated here since this is the function that
    contract binds): this only ever REPLACES `Start-Process ...` with the
    launched command's own argv, so any guard that already recognized
    `Start-Process notepad` as harmless recognizes the identically-shaped
    `notepad` after expansion too -- no new token is invented that was not
    already present (as a `-ArgumentList`/`-FilePath` value) in the input.

    Shapes handled (per this chunk's dispatch brief):
      - `-ArgumentList`/`-Args`, single- OR double-quoted elements
        (`-ArgumentList '-m','pytest'`, `-ArgumentList "-m","pytest"`).
      - A single (unsplit) `-ArgumentList` string value
        (`-ArgumentList '-m pytest'`) -- split on whitespace after
        unquoting, since `Start-Process` accepts either an array of
        already-split arguments or one pre-joined command-line string;
        this widens detection either way (see "Widens... never a verdict"
        above).
      - `-FilePath <target>` named explicitly, OR the bare positional
        immediately following the `Start-Process`/alias token (the cmdlet's
        own positional-1 binding) when `-FilePath` is absent.
      - The `saps` and `start` aliases (case-insensitively).
      - Parameter order variation -- `-ArgumentList` before `-FilePath`, a
        named `-FilePath` before or after `-ArgumentList`, either flag
        appearing before or after the bare positional target -- this walk
        scans every token in the call's own span rather than assuming a
        fixed order.

    Shapes deliberately NOT resolved to a value (see this module's own
    "NEGATIVE SPEC", same discipline applied here): a `-ArgumentList`
    element that is a variable or sub-expression (`-ArgumentList $args`)
    cannot be evaluated statically, so its LITERAL source text (`$args`) is
    carried through as the reconstructed argv element verbatim, rather than
    the value it would expand to at runtime -- this is the same class of
    gap `_dialect.py`'s own module docstring already accepts for command
    SUBSTITUTION generally (`_INDETERMINATE_HEAD_RE`'s sibling limitation
    one layer down in `_command_tokenizer.py`). Only when NO `-FilePath`
    value and no bare positional target can be resolved AT ALL (the
    launched executable itself is indeterminate) is the `Start-Process`
    call left entirely untouched, so no rewrite is emitted with an unknown
    head. This function is not exhaustive against adversarial
    `-ArgumentList` input -- per this chunk's own dispatch brief, the goal
    is that the honest idioms an agent actually reaches for are seen, not
    that every conceivable obfuscation is unwound.

    Runs BEFORE segmentation (called from `resolve_segments_for_dialect`'s
    POWERSHELL branch, ahead of `segments_from_tokens_with_pipe_flag`), so
    the reconstructed argv participates in ordinary command-position
    resolution exactly like any other segment -- no second segmentation
    pass, no new output shape.
    """
    out: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.lower() not in _START_PROCESS_NAMES:
            out.append(tok)
            i += 1
            continue

        j = i + 1
        filepath: Optional[str] = None
        arg_tokens: List[str] = []
        while j < n and tokens[j] not in _STATEMENT_BOUNDARY_TOKENS:
            t = tokens[j]
            low = t.lower()
            if low in _FILE_PATH_FLAGS:
                j += 1
                if j < n and tokens[j] not in _STATEMENT_BOUNDARY_TOKENS:
                    filepath = _strip_ps_quotes(tokens[j])
                    j += 1
                continue
            if low in _ARGUMENT_LIST_FLAGS:
                j += 1
                while j < n and tokens[j] not in _STATEMENT_BOUNDARY_TOKENS:
                    elem = tokens[j]
                    if elem == ",":
                        j += 1
                        continue
                    # A bare `-Flag`-shaped element with no quoting at all is
                    # a DIFFERENT Start-Process parameter beginning after an
                    # unquoted/unterminated argument-list value this walk
                    # cannot resolve as a literal -- stop consuming rather
                    # than swallowing an unrelated flag as an argv element.
                    if elem.startswith("-") and elem[:1] not in ("'", '"'):
                        break
                    unquoted = _strip_ps_quotes(elem)
                    if unquoted.strip():
                        arg_tokens.extend(unquoted.split())
                    j += 1
                continue
            if low.startswith("-"):
                # An unrecognized Start-Process parameter (`-WindowStyle`,
                # `-NoNewWindow`, `-Wait`, `-Credential`, ...) -- skip the
                # flag itself. Not walked for its own value/arity (this
                # function only needs to locate `-FilePath`/`-ArgumentList`
                # and the bare positional target, not fully parse every
                # `Start-Process` parameter), so a value-taking unrecognized
                # flag's value token falls through to the bare-positional
                # branch below on the next loop iteration and is silently
                # absorbed as a would-be filepath ONLY if `filepath` is
                # still unset -- acceptable here since an unrecognized
                # flag's value is never itself the runner argv this
                # function exists to surface.
                j += 1
                continue
            if filepath is None:
                filepath = _strip_ps_quotes(t)
                j += 1
                continue
            # A second bare positional with `filepath` already resolved is
            # outside this cmdlet's own positional grammar (Start-Process
            # has exactly one positional parameter, FilePath) -- leave it
            # and stop scanning this call.
            break

        if filepath:
            out.append(filepath)
            out.extend(arg_tokens)
            i = j
            continue

        # No resolvable literal FilePath found (e.g. `-ArgumentList` given
        # but the target came from a variable) -- leave this call's tokens
        # untouched rather than emit a partial/incorrect rewrite.
        out.append(tok)
        i += 1
    return out


def resolve_segments_for_dialect(
    cmd_text: str, dialect: Optional[Dialect], *, guard_name: str
):
    """Tokenize AND segment `cmd_text` for `dialect` in one call, reusing
    the EXISTING `_command_tokenizer.segments_from_tokens_with_pipe_flag`
    for the segmentation step regardless of dialect (see module docstring
    "Output shape") -- this is the seam C4/C5's per-guard conversions call
    instead of `tokenize_full_command` + `segments_from_tokens_with_pipe_flag`
    directly, gaining dialect coverage with no change to the segmentation
    logic itself.

    Returns `None` when `tokenize_command` returns `None` (SILENT already
    recorded by that call, or pre-existing bash fail-closed `None` for the
    BASH branch -- see `tokenize_command`'s own docstring for which is
    which); otherwise the same `List[Tuple[List[str], bool]]` shape
    `segments_from_tokens_with_pipe_flag` always returns.

    POWERSHELL-only: the raw token stream is run through
    `expand_start_process_invocations` BEFORE segmentation, so a
    `Start-Process <target> -ArgumentList ...` call's hidden argv is
    already reconstructed and visible in command position by the time
    every caller of this function (every guard sourcing tokens through
    this seam) sees its segments -- see that function's own docstring for
    the bypass this closes and the shapes handled. BASH is unaffected
    (AC4): the expansion runs only on the POWERSHELL leg, after
    `tokenize_command` has already dispatched on dialect.
    """
    tokens = tokenize_command(cmd_text, dialect, guard_name=guard_name)
    if tokens is None:
        return None
    if dialect is Dialect.POWERSHELL:
        tokens = expand_start_process_invocations(tokens)
    return _command_tokenizer.segments_from_tokens_with_pipe_flag(tokens)
