"""coordinator_core.bash_guards._shape_classifier -- tokenizer-based shape
classifier for the six ranked bash-spawn shapes identified by the
1,389-transcript / 62,487-call fork-tax measurement
(``state/plan-sidecars/2026-07-28-bash-tax-negative-space.md``,
DoE ``docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md`` C2, AC-2):
grep-via-Bash (50.9% of forks), multi-probe banner commands (40.1%),
head/tail plumbing (25%), for-loops (9.0%), ``find -exec``/``xargs``
(3.5%), and while-read loops (unmeasured in that sample -- see
``docs/plans/2026-08-10-the-one-fan-out-shape-the-classifier-nev.md``,
added as the structural twin of the for-loop shape, spawn-per-iteration in
the exact same way, and no percentage of its own is invented for it).

Built exclusively on ``_command_tokenizer.tokenize_full_command`` and its
segment/binary-identity helpers -- NEVER regex for command-shape detection
(AC-2, plan Anti-scope "Do not use regex for shape detection"; three
independent regex holes were found in ``check_offer_git_c``'s matcher in a
single day). The one string-scanning helper this module defines,
``_has_banner_marker``, is a plain character-run counter over an ALREADY-
tokenized argument, not a regex over the raw command text -- see its own
docstring.

THIS MODULE DOES NOT DENY. It classifies a command string into zero or
more of the six shapes and returns a verdict object; C3 (grep-via-Bash),
C4 (multi-probe banner), C5 (head/tail plumbing + for-loop + while-read),
and C9 (find-exec/xargs, if shipped) each consume this module's output to
decide their own allow/advise/deny policy. Keeping detection and policy in
separate modules means a policy change in one guard can never silently
change what another guard sees as a match.

===========================================================================
PINNED CONTRACT for C3/C4/C5/C9 -- do not change these shapes without
updating every consumer and this module's own test file
(``tests/test_shape_classifier.py``):
===========================================================================

``Shape`` -- an ``str`` `Enum` naming the seven shapes. Six are the
bash-measured set; ``PIPELINE_FOREACH_OBJECT`` is PowerShell-only (D2,
pln-the-shape-classifier-reaches-a-e743e5 § C2) and never appears in the
BASH table entry -- a `ForEach-Object`/`%` pipeline stage has no bash
analogue, so there is no bash detector to reuse or fork.

``SHAPE_PRECEDENCE`` -- all seven shapes in fixed precedence order:
``GREP_VIA_BASH > MULTI_PROBE_BANNER > HEAD_TAIL_PLUMBING > FOR_LOOP >
PIPELINE_FOREACH_OBJECT > WHILE_READ_LOOP > FIND_EXEC_XARGS``. Both
``PIPELINE_FOREACH_OBJECT`` and ``WHILE_READ_LOOP`` sit between ``FOR_LOOP``
and ``FIND_EXEC_XARGS`` -- each is the structural twin of the for-loop shape
(spawn-per-iteration) and was unmeasured, not measured at zero, so each
inherits its position from that twin rather than carrying a rank of its own
(``WHILE_READ_LOOP``: docs/plans/2026-08-10-the-one-fan-out-shape-the-
classifier-nev.md § Design decisions already made; ``PIPELINE_FOREACH_OBJECT``:
this plan's D2, seated immediately after ``FOR_LOOP`` on the same argument).
This order is LOAD-BEARING, not incidental: the shapes overlap
(a single real command can be a multi-probe banner, a grep-via-Bash, AND
carry head/tail plumbing simultaneously -- the plan's own canonical
example is ``echo "=== ... ==="; ... | grep -i ... | head``), and with no
precedence rule AC-7 ("no shipped deny message misdescribes what tripped
it") is silently unsatisfiable, because a caller would have to pick
arbitrarily among simultaneously-true matches. This module removes that
arbitrariness by construction: ``classify_command`` walks
``SHAPE_PRECEDENCE`` in order and appends each shape's match (if any) to
the result, so the FIRST entry in ``ShapeClassification.matches`` is
always the precedence winner -- there is no separate sort step for a
caller to get wrong. A command that is both FOR_LOOP and WHILE_READ_LOOP
shaped classifies FOR_LOOP-primary, with the while-read match surfacing in
``.residue``.

``ShapeMatch`` -- a frozen dataclass pairing the matched ``Shape`` with a
human-readable ``evidence`` string (the matched segment's tokens, rejoined
with spaces) -- enough for a deny/advise message to say e.g. "this looks
like ``grep -rn TODO src/``" without re-deriving the segment itself.

``ShapeClassification`` -- a frozen dataclass carrying:
  - ``tokens``: the full tokenized command, or ``None`` if
    ``tokenize_full_command`` failed to parse it (unterminated quote /
    trailing backslash). A caller that must fail closed on an unparseable
    command checks ``tokens is None`` itself -- this module does not make
    that fail-open/fail-closed choice, since it never denies.
  - ``matches``: a tuple of every ``ShapeMatch`` that fired, in
    ``SHAPE_PRECEDENCE`` order (so ``matches[0]``, when non-empty, is
    always the precedence winner -- never an arbitrary pick).
  - ``.primary`` (property): ``matches[0]`` or ``None`` if nothing matched.
  - ``.residue`` (property): ``matches[1:]`` -- every OTHER shape this
    command also matches, in precedence order. A caller renders this as
    "this command is also a grep-via-Bash and carries head plumbing"
    rather than silently dropping the secondary matches.
  - ``.matched_shapes`` (property): just the ``Shape`` values, in the same
    order, for a caller that only needs identity, not evidence.
  - ``.has_shape(shape)``: membership test.

``classify_command(cmd_text: str, *, dialect: Optional[Dialect] =
Dialect.BASH) -> ShapeClassification`` -- the one entry point. `dialect` is
keyword-only and defaults to ``Dialect.BASH``, so every pre-existing caller
(passing nothing) gets byte-for-byte identical behaviour to the pre-dialect
version of this function. An explicitly passed ``dialect=None`` is SILENT
-- an empty classification with ``tokens=None`` -- and never falls back to
bash; the default and an explicit `None` stay distinguishable because the
default is a concrete enum member, not a sentinel that collapses with it.
Dialect-aware tokenization routes through ``_dialect.tokenize_command``,
never ``_command_tokenizer.tokenize_full_command`` directly, so a
POWERSHELL-dialect call never reaches the posix `shlex` tokenizer. Detection
per dialect is table-driven (``_DETECTOR_TABLE``, D4) rather than a branch
inside the walk; BASH's table entry reproduces ``SHAPE_PRECEDENCE`` exactly
minus ``PIPELINE_FOREACH_OBJECT`` (bash has no such shape). POWERSHELL's
entry (D2, C2) carries six of the seven shapes in ``SHAPE_PRECEDENCE``
order: three binary-identity detectors reused UNCHANGED from the bash leg
(``GREP_VIA_BASH``, ``HEAD_TAIL_PLUMBING``, ``FIND_EXEC_XARGS`` -- they key
on argv[0] identity, not shell keywords, so pwsh tokens already satisfy
them), two forked pwsh-vocabulary predicates (``MULTI_PROBE_BANNER``,
``FOR_LOOP``), and the new ``PIPELINE_FOREACH_OBJECT`` member.
``WHILE_READ_LOOP`` is deliberately ABSENT from the POWERSHELL entry --
PowerShell has no ``while read`` idiom -- see the comment at the table
entry itself (AC8): an absence with no stated reason reads as an oversight
to the next author. Never raises on ordinary unparseable/empty input (mirrors
``tokenize_full_command``'s own fail-return-None contract); a genuine
internal exception indicates a programming bug in this module, not user
input.

Spec backlink: pln-the-shape-classifier-reaches-a-e743e5 § C1
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from ._command_tokenizer import (
    segments_from_tokens_with_pipe_flag,
    token_matches_binary,
)
from ._dialect import Dialect, tokenize_command
from ._verdict import record_silent
from .block_subagent_destructive_action import _strip_heredoc_bodies

__all__ = [
    "Shape",
    "SHAPE_PRECEDENCE",
    "ShapeMatch",
    "ShapeClassification",
    "classify_command",
]


class Shape(str, Enum):
    """The six ranked bash-spawn shapes this module classifies against.

    Values are stable identifiers (used in test assertions and, by
    downstream guards, in deny/advise message templates) -- do not rename
    a value without a corresponding update in every C3/C4/C5/C9 consumer.
    """

    GREP_VIA_BASH = "grep_via_bash"
    MULTI_PROBE_BANNER = "multi_probe_banner"
    HEAD_TAIL_PLUMBING = "head_tail_plumbing"
    FOR_LOOP = "for_loop"
    #: PowerShell-only -- see `_DETECTOR_TABLE`'s POWERSHELL entry (D2, C2
    #: of pln-the-shape-classifier-reaches-a-e743e5). No bash analogue: a
    #: bash `for`/`while` loop is source text the classifier can see ahead
    #: of the spawn, but a `ForEach-Object`/`%` PIPELINE STAGE spawns once
    #: per input object with no bash equivalent shape. Seated here, between
    #: `FOR_LOOP` and `WHILE_READ_LOOP`, on the same "structural twin of the
    #: for-loop shape" argument `WHILE_READ_LOOP` itself was seated on --
    #: unmeasured, not measured at zero (see `SHAPE_PRECEDENCE` below).
    PIPELINE_FOREACH_OBJECT = "pipeline_foreach_object"
    WHILE_READ_LOOP = "while_read_loop"
    FIND_EXEC_XARGS = "find_exec_xargs"


#: Fixed precedence order, highest first. See the module docstring's
#: "PINNED CONTRACT" section for why this exists and why it must not
#: become a per-call sort -- ``classify_command`` derives match ORDER
#: directly from iterating this tuple, so the precedence rule cannot drift
#: out of sync with the actual detection order.
SHAPE_PRECEDENCE: Tuple[Shape, ...] = (
    Shape.GREP_VIA_BASH,
    Shape.MULTI_PROBE_BANNER,
    Shape.HEAD_TAIL_PLUMBING,
    Shape.FOR_LOOP,
    Shape.PIPELINE_FOREACH_OBJECT,
    Shape.WHILE_READ_LOOP,
    Shape.FIND_EXEC_XARGS,
)

#: grep-family binaries recognized for the grep-via-Bash shape. ``rg``
#: (ripgrep) is included because it is the same habit (ad-hoc content
#: search spawned as a Bash child) with a different binary name, not a
#: distinct shape -- the fork-tax measurement counts the HABIT, not the
#: literal argv[0] spelling.
_GREP_FAMILY_BINARIES: Tuple[str, ...] = ("grep", "egrep", "fgrep", "rg")

#: head/tail plumbing binaries -- see ``_detect_head_tail_plumbing``.
_HEAD_TAIL_BINARIES: Tuple[str, ...] = ("head", "tail")

#: Session-fact probe binaries this fleet's harness already knows the
#: answer to -- the family the MULTI_PROBE_BANNER shape's own name and
#: docstring claim to detect (``echo``/``printf`` carry the banner label
#: itself and are handled separately in ``_is_probe_segment``; the
#: remaining harness-known-fact probes are ``git``, ``pwd``, ``whoami``,
#: ``date``, and ``uname`` -- the same family
#: ``dispatch_checks._bt_probe_segment_kind`` recognizes as translatable
#: session-fact probes for the sibling rewrite guard. Not imported from
#: there: that module imports THIS one, and duplicating a five-name tuple
#: is cheaper than restructuring the import graph for it.
_SESSION_FACT_PROBE_BINARIES: Tuple[str, ...] = (
    "git",
    "pwd",
    "whoami",
    "date",
    "uname",
)

#: Minimum total segment count (post ``;``/``&``/``|`` split) for a
#: banner-marked echo to count as a MULTI-probe banner rather than a
#: single-purpose banner-plus-one-command. A bare
#: ``echo "=== status ==="; git status`` (2 segments) is a labeled single
#: probe, not the "N unrelated probes in one call" shape the plan
#: describes (C4 body) -- the third segment is what turns it into the
#: fan-out this shape targets.
_MIN_BANNER_SEGMENTS = 3


@dataclass(frozen=True)
class ShapeMatch:
    """One shape's match against a command: which shape, and the evidence
    (the matched segment's tokens, rejoined with spaces) a deny/advise
    message can quote directly.
    """

    shape: Shape
    evidence: str


@dataclass(frozen=True)
class ShapeClassification:
    """Full classification result for one command. See the module
    docstring's "PINNED CONTRACT" section -- this is the shape C3/C4/C5/C9
    are authored against.
    """

    tokens: Optional[List[str]]
    matches: Tuple[ShapeMatch, ...]

    @property
    def primary(self) -> Optional[ShapeMatch]:
        """The highest-precedence match, or ``None`` if nothing matched."""
        return self.matches[0] if self.matches else None

    @property
    def residue(self) -> Tuple[ShapeMatch, ...]:
        """Every OTHER shape this command also matches, in precedence
        order -- never dropped, so a caller can render "also matches X, Y".
        """
        return self.matches[1:]

    @property
    def matched_shapes(self) -> Tuple[Shape, ...]:
        """Just the ``Shape`` identities of ``matches``, same order."""
        return tuple(m.shape for m in self.matches)

    def has_shape(self, shape: Shape) -> bool:
        """Whether ``shape`` is among ``matches``, at any precedence."""
        return shape in self.matched_shapes


def _has_banner_marker(token: str) -> bool:
    """Return whether `token` contains a run of 3+ consecutive `=`
    characters -- the ``===`` banner-delimiter convention this fleet's
    multi-probe commands use (e.g. ``echo "=== git status ==="``).

    Plain character-run counting over one ALREADY-tokenized argument, not
    a regex over the raw command string -- this module's one string-scan
    helper, kept deliberately this simple so it cannot become the kind of
    regex-over-raw-text shape detector AC-2 forbids. A run of 3 is chosen
    over an exact ``===`` literal match so ``====`` / longer banner rules
    used inconsistently across a transcript still count as a banner.
    """
    run = 0
    for ch in token:
        if ch == "=":
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def _detect_grep_via_bash(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """Match if ANY segment invokes a grep-family binary
    (``_GREP_FAMILY_BINARIES``), at any pipe position -- a piped
    ``... | grep -i foo`` is exactly as much "grep via Bash" as a bare
    ``grep -rn foo src/``.
    """
    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        if any(token_matches_binary(tokens[0], b) for b in _GREP_FAMILY_BINARIES):
            return ShapeMatch(Shape.GREP_VIA_BASH, evidence=" ".join(tokens))
    return None


#: Matches a leading `VAR=value` environment-assignment token, the same
#: shape `env FOO=bar ...` uses for each assignment before its target
#: binary -- see `_peel_probe_prefix`.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _peel_probe_prefix(tokens: List[str]) -> List[str]:
    """Peel a leading `sudo` token, or a leading `env` token plus its run of
    `VAR=value` assignment tokens, off `tokens` before `_is_probe_segment`
    tests the head binary (Review: coordinator:code-reviewer, Finding 2) --
    `sudo git status` / `env FOO=bar git status` as a top-level segment is
    still a genuine session-fact re-derivation, not a different shape,
    despite the wrapper prefix disqualifying a bare head-binary check.

    Deliberately narrow -- exactly one `sudo` token, or `env` plus ONLY
    `VAR=value`-shaped tokens, stops at anything else (a flag, a
    non-assignment operand). This is NOT a general command-prefix parser:
    it does not peel `command`/`time`/`exec`/`nice`/`nohup` or chain
    multiple wrappers (`sudo env FOO=bar ...`) -- those are out of scope
    for this narrowly-named gap.
    """
    if not tokens:
        return tokens
    head = tokens[0]
    if token_matches_binary(head, "sudo"):
        return tokens[1:]
    if token_matches_binary(head, "env"):
        rest = tokens[1:]
        i = 0
        while i < len(rest) and _ENV_ASSIGNMENT_RE.match(rest[i]):
            i += 1
        return rest[i:]
    return tokens


def _is_probe_segment(tokens: List[str]) -> bool:
    """Whether `tokens` (one already-split segment) invokes a
    harness-known-fact probe binary -- ``echo``/``printf`` (the banner
    label itself, or a bare probe echo like ``echo "=== git status ==="``
    or ``echo hi``) or one of ``_SESSION_FACT_PROBE_BINARIES`` (``git``,
    ``pwd``, ``whoami``, ``date``, ``uname``), after peeling a leading
    `sudo`/`env VAR=x ...` prefix (`_peel_probe_prefix`). Used by
    ``_detect_multi_probe_banner`` to require every OTHER segment in a
    banner-shaped command to actually be a session-fact re-derivation,
    not just formatted like one -- see that function's docstring.
    """
    if not tokens:
        return False
    tokens = _peel_probe_prefix(tokens)
    if not tokens:
        return False
    head = tokens[0]
    if token_matches_binary(head, "echo") or token_matches_binary(head, "printf"):
        return True
    return any(token_matches_binary(head, b) for b in _SESSION_FACT_PROBE_BINARIES)


def _detect_multi_probe_banner(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """Match if the command contains a banner-marked ``echo``/``printf``
    segment (see ``_has_banner_marker``), the command has at least
    ``_MIN_BANNER_SEGMENTS`` total segments (a banner label alone --
    ``echo "=== status ==="; git status``, 2 segments -- is a single
    labeled probe, not the N-unrelated-probes-in-one-call shape this
    detector targets), AND every OTHER TOP-LEVEL segment (``pipe_before``
    is ``False`` -- the head of a ``;``/``&``-separated command, not a
    pipe continuation of one) is itself a harness-known-fact probe
    (``_is_probe_segment``: ``echo``/``printf`` or the
    ``git``/``pwd``/``whoami``/``date``/``uname`` family).

    Pipe-continuation segments (``pipe_before`` True) are deliberately
    NOT required to be probe-shaped: the plan's own canonical overlap
    example, ``echo "=== git status ==="; git status | grep -i modified |
    head``, is a SINGLE probe (``git status``) piped through plumbing --
    the ``grep``/``head`` segments are shape of that one probe's output,
    not additional probes, and gating on them here would make this
    detector silent on its own pinned canonical case
    (``TestPrecedence.test_canonical_triple_overlap_resolves_grep_first``).

    That third condition is the semantic content this shape's own name
    claims: "re-derives session facts the harness already knows". Without
    it, this detector was pure banner-formatting shape (``echo`` + ``===``
    + 3+ segments) and matched ANY labeled multi-segment command -- e.g.
    ``echo "=== EM snippets ==="; wc -c a.md b.md; ls x``, a legitimate
    labeled multi-file measurement that probes nothing the harness
    already knows. Confirmed false positive,
    ``state/audits/2026-08-14-boot-payload-baseline.md`` § "The
    false-positive matcher". A command that mixes a real probe segment
    with a non-probe segment (``echo "=== x ==="; git status; wc -l a``)
    does not match either -- it is not PURELY the re-derive-known-facts
    shape, so this detector stays silent and lets whatever shape (if any)
    the non-probe segment matches speak for itself, rather than misnaming
    the mix.
    """
    banner_segment: Optional[List[str]] = None
    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        if token_matches_binary(tokens[0], "echo") or token_matches_binary(
            tokens[0], "printf"
        ):
            if any(_has_banner_marker(tok) for tok in tokens[1:]):
                banner_segment = tokens
                break
    if banner_segment is None:
        return None
    if len(segments) < _MIN_BANNER_SEGMENTS:
        return None
    for tokens, pipe_before in segments:
        if tokens is banner_segment:
            continue
        if not tokens:
            continue
        if pipe_before:
            continue
        if not _is_probe_segment(tokens):
            return None
    return ShapeMatch(Shape.MULTI_PROBE_BANNER, evidence=" ".join(banner_segment))


def _detect_head_tail_plumbing(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """Match if a ``head``/``tail`` segment is fed by a pipe
    (``pipe_before`` True) -- ``cmd | head`` is the plumbing shape (using
    a subprocess to truncate output that a paged read could have avoided);
    a bare, un-piped ``head file.txt`` is an ordinary bounded read and is
    deliberately NOT matched here.
    """
    for tokens, pipe_before in segments:
        if not tokens:
            continue
        if pipe_before and any(
            token_matches_binary(tokens[0], b) for b in _HEAD_TAIL_BINARIES
        ):
            return ShapeMatch(Shape.HEAD_TAIL_PLUMBING, evidence=" ".join(tokens))
    return None


def _detect_for_loop(tokens: List[str]) -> Optional[ShapeMatch]:
    """Match if the command's OWN first token (not a token nested inside a
    quoted argument elsewhere -- ``tokenize_full_command`` already
    resolved quoting, so a quoted ``"for x in y; do z; done"`` string
    yields a single opaque token, never a literal ``for`` at position 0
    unless the command itself starts with an unquoted ``for``) is the
    shell keyword ``for``, and the token stream also contains both ``do``
    and ``done`` -- the minimal signature of a POSIX ``for ... do ...
    done`` loop. Plain equality against a shell keyword, not
    ``token_matches_binary`` -- ``for``/``do``/``done`` are shell
    grammar, not executables, so the Windows-launcher-suffix/path-
    separator normalization that function exists for does not apply.
    """
    if not tokens:
        return None
    if tokens[0] != "for":
        return None
    if "do" in tokens and "done" in tokens:
        preview = tokens[: min(len(tokens), 12)]
        return ShapeMatch(Shape.FOR_LOOP, evidence=" ".join(preview))
    return None


def _detect_while_read_loop(
    segments: List[Tuple[List[str], bool]], tokens: List[str]
) -> Optional[ShapeMatch]:
    """Match if some segment's OWN first token is the shell keyword
    ``while`` and ``read`` appears anywhere else in that same segment's
    tokens, AND both ``do`` and ``done`` appear in the FULL token stream --
    the minimal signature of a ``while read`` fan-out loop
    (``... | while read f; do ...; done``, ``while IFS= read -r f; do
    ...; done < f``).

    Segment-scoped for the ``while``/``read`` pair because the pipe-fed
    spelling (``cat f | while read x; do ...; done``) puts ``while`` at a
    segment head, not the command's own first token the way ``for`` always
    is for `_detect_for_loop` -- segments split on ``;``/``&``/``|``, so
    matching against every segment (not just ``tokens[0]``) is required to
    see the piped spelling at all. ``do``/``done`` are checked against the
    FULL stream instead, because the segment split on ``;`` ends the
    ``while`` segment before ``do`` ever appears in it (mirrors
    `_detect_for_loop`'s own full-stream `do`/`done` check).

    ``read`` is required (not just ``while``) so a poll loop
    (``while true; do ...; done``) -- a different shape with a different
    remedy -- does not classify; and ``read`` is looked for anywhere in
    ``tokens[1:]`` of the segment (not at a fixed position) so the
    canonical safe spelling ``while IFS= read -r f``, which puts an
    assignment between ``while`` and ``read``, still matches.

    Plain equality against the shell keywords/builtin (``while``, ``read``,
    ``do``, ``done``), never `token_matches_binary` -- mirrors
    `_detect_for_loop`'s own reasoning: these are shell grammar and a
    builtin, not executables, so the Windows-launcher-suffix/path-separator
    normalization `token_matches_binary` exists for does not apply.
    """
    if "do" not in tokens or "done" not in tokens:
        return None
    for seg_tokens, _pipe_before in segments:
        if not seg_tokens:
            continue
        if seg_tokens[0] != "while":
            continue
        if "read" in seg_tokens[1:]:
            preview = seg_tokens[: min(len(seg_tokens), 12)]
            return ShapeMatch(Shape.WHILE_READ_LOOP, evidence=" ".join(preview))
    return None


def _detect_find_exec_xargs(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """Match if any segment is a ``find`` invocation carrying a literal
    ``-exec`` argument, or any segment invokes ``xargs`` (bare or
    pipe-fed, e.g. ``find . -name '*.log' | xargs rm``).
    """
    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        if token_matches_binary(tokens[0], "find") and any(
            tok == "-exec" for tok in tokens[1:]
        ):
            return ShapeMatch(Shape.FIND_EXEC_XARGS, evidence=" ".join(tokens))
        if token_matches_binary(tokens[0], "xargs"):
            return ShapeMatch(Shape.FIND_EXEC_XARGS, evidence=" ".join(tokens))
    return None


# ---------------------------------------------------------------------------
# PowerShell-only detectors (D2, D5). Every predicate below is authored
# against PRINTED `_powershell_tokens` output, never assumed isomorphic to
# the bash detectors above -- see each function's own docstring for the
# measured tokens it was written against. D5's binding fact: a NUMERIC flag
# splits (`git log -1` -> `['git', 'log', '-', '1']`) while an IDENTIFIER
# flag survives intact (`-Recurse`, `-l`) -- no predicate below matches a
# numeric flag token.
# ---------------------------------------------------------------------------

#: PowerShell banner-cmdlet vocabulary -- the pwsh analogue of bash's
#: ``echo``/``printf`` (handled inline in `_is_probe_segment`'s bash
#: version). Matched via `token_matches_binary` (case-insensitive, cmdlet
#: names carry no path separator so this reduces to a case-fold).
#:
#: ``echo`` is here because PowerShell ships it as a live ALIAS of
#: ``Write-Output``, exactly as it ships ``%`` for ``ForEach-Object``
#: (`_PWSH_FOREACH_OBJECT_ALIASES` below applies the same reasoning). D2
#: phrases this vocabulary as "``Write-Host``/``Write-Output``, not
#: ``echo``/``printf``" -- that contrast is drawn against BASH's builtins,
#: and reading it as a prohibition on the pwsh alias re-opens the very
#: escape hatch this dialect leg exists to close: ``echo '=== x ==='; git
#: status; git log -1; pwd`` runs under the PowerShell tool and spawns per
#: probe exactly as its ``Write-Host`` spelling does. ``printf`` is
#: correctly absent -- PowerShell ships no such alias, so admitting it
#: would match a name that cannot run.
_PWSH_BANNER_BINARIES: Tuple[str, ...] = ("write-host", "write-output", "echo")

#: `ForEach-Object` cmdlet identity plus its two live aliases -- the pwsh
#: pipeline-stage spelling of per-item iteration. Deliberately does NOT
#: include the bare statement keyword `foreach` when followed by `(` --
#: that is `_detect_for_loop_pwsh`'s grammar (a different statement, per
#: D2's row-14-superseding note), disambiguated below by requiring
#: `pipe_before` rather than by inspecting the token after the alias.
_PWSH_FOREACH_OBJECT_ALIASES: Tuple[str, ...] = ("foreach-object", "foreach", "%")


def _is_probe_segment_pwsh(tokens: List[str]) -> bool:
    """PowerShell analogue of `_is_probe_segment`: whether `tokens` (one
    already-split segment) invokes a banner cmdlet (`_PWSH_BANNER_BINARIES`)
    or one of `_SESSION_FACT_PROBE_BINARIES` (`git`, `pwd`, `whoami`,
    `date`, `uname` -- reused UNCHANGED; these are the same argv[0]
    identities on the PowerShell leg, e.g. `git status` and bare `pwd`, the
    latter a native pwsh alias for `Get-Location` but still the harness-
    known-fact re-derivation this shape targets). No `sudo`/`env` prefix
    peeling -- unlike bash, `sudo`/`env FOO=bar` wrapping is not a pwsh
    idiom this shape has a measured case for.
    """
    if not tokens:
        return False
    head = tokens[0]
    if any(token_matches_binary(head, b) for b in _PWSH_BANNER_BINARIES):
        return True
    return any(token_matches_binary(head, b) for b in _SESSION_FACT_PROBE_BINARIES)


def _detect_multi_probe_banner_pwsh(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """PowerShell analogue of `_detect_multi_probe_banner`, same structure
    (banner-marked segment + `_MIN_BANNER_SEGMENTS` total + every OTHER
    top-level segment is probe-shaped) with the vocabulary swapped:
    `Write-Host`/`Write-Output` instead of `echo`/`printf`
    (`_is_probe_segment_pwsh` instead of `_is_probe_segment`).
    `_has_banner_marker` (the ``===`` character-run counter) is reused
    UNCHANGED -- it scans one already-tokenized argument's text, which is
    dialect-agnostic.

    Measured (D5) against
    ``Write-Host '=== status ==='; git status; git log -1; pwd`` ->
    ``['Write-Host', "'=== status ==='", ';', 'git', 'status', ';', 'git',
    'log', '-', '1', ';', 'pwd']``, four segments, all three non-banner
    segments probe-shaped -> matches. A bare
    ``Write-Host 'hello'`` -> ``['Write-Host', "'hello'"]`` is ONE segment,
    below `_MIN_BANNER_SEGMENTS` -- inherited from the shared minimum
    check, this alone keeps a lone banner call from matching (AC7).
    """
    banner_segment: Optional[List[str]] = None
    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        if any(token_matches_binary(tokens[0], b) for b in _PWSH_BANNER_BINARIES):
            if any(_has_banner_marker(tok) for tok in tokens[1:]):
                banner_segment = tokens
                break
    if banner_segment is None:
        return None
    if len(segments) < _MIN_BANNER_SEGMENTS:
        return None
    for tokens, pipe_before in segments:
        if tokens is banner_segment:
            continue
        if not tokens:
            continue
        if pipe_before:
            continue
        if not _is_probe_segment_pwsh(tokens):
            return None
    return ShapeMatch(Shape.MULTI_PROBE_BANNER, evidence=" ".join(banner_segment))


def _detect_for_loop_pwsh(tokens: List[str]) -> Optional[ShapeMatch]:
    """Match PowerShell's `foreach ($x in $y) { ... }` STATEMENT grammar --
    the command's own first token is (case-insensitively) `foreach`,
    immediately followed by a `(` (the parenthesised `in` clause -- this is
    what distinguishes the statement from the `ForEach-Object`/`foreach`/`%`
    PIPELINE-STAGE alias `_detect_pipeline_foreach_object` matches, which is
    never the command's own first token since a pipeline stage is always
    preceded by a `|`), an `in` token somewhere in that clause, and a brace
    block. No `do`/`done` -- that is bash's grammar, not pwsh's (D2 row-14
    supersession).

    Measured (D5) against ``foreach ($f in $files) { git log -1 $f }`` ->
    ``['foreach', '(', '$f', 'in', '$files', ')', '{', 'git', 'log', '-',
    '1', '$f', '}']``.
    """
    if not tokens:
        return None
    if tokens[0].lower() != "foreach":
        return None
    if len(tokens) < 2 or tokens[1] != "(":
        return None
    if not any(tok.lower() == "in" for tok in tokens):
        return None
    if "{" not in tokens or "}" not in tokens:
        return None
    preview = tokens[: min(len(tokens), 12)]
    return ShapeMatch(Shape.FOR_LOOP, evidence=" ".join(preview))


#: Approved-verb prefix set for the in-process-cmdlet exclusion
#: (`_is_pwsh_inprocess_head`, Finding 2 fix). PowerShell cmdlets run
#: in-process by construction -- a `Verb-Noun` head matching one of these
#: approved verbs is excluded from `_block_has_native_call`'s call-head
#: test, not because D3 permits hand-listing cmdlets as a SHAPE signal
#: (it doesn't -- this list never determines a MATCH, only rules out a
#: false one) but because using cmdlet identity to EXCLUDE a false
#: positive serves D3's goal (no confident-wrong verdict) rather than
#: violating it.
#:
#: Deliberately excludes `Start` and `Invoke` -- the carve-out is this
#: omission itself, reasoned at `_PWSH_CMDLET_VERB_PATTERN` below; there
#: is no separate constant naming it. Anchored on this
#: specific verb list, not a bare "contains a hyphen" check, so a
#: hyphenated native executable (`docker-compose`, `git-lfs`) still fails
#: the match and is correctly treated as a native call.
_PWSH_APPROVED_VERBS: Tuple[str, ...] = (
    "Get", "Set", "New", "Remove", "Write", "Select", "Where", "ForEach",
    "Out", "Format", "Measure", "Sort", "Group", "Compare", "Test", "Add",
    "Clear", "Copy", "Move", "Rename", "Import", "Export", "ConvertTo",
    "ConvertFrom", "Join", "Split", "Read", "Receive", "Send", "Push",
    "Pop", "Enter", "Exit",
)

#: `Start-Process`, `Invoke-Expression`, `Invoke-Command`, `Invoke-Item`
#: genuinely spawn a process, so `Start`/`Invoke` are deliberately absent
#: from `_PWSH_APPROVED_VERBS` above: those two verbs are semantically
#: the process-starting ones, and a `Start-`/`Invoke-` head must keep
#: matching as a native call rather than being excluded by this rule.
_PWSH_CMDLET_VERB_PATTERN = re.compile(
    r"^(?:" + "|".join(_PWSH_APPROVED_VERBS) + r")-\w+$",
    re.IGNORECASE,
)

#: Known in-process cmdlet ALIASES -- these fork nothing on the
#: PowerShell leg (unlike a same-named bash builtin/binary), so a block
#: sub-segment headed by one of these is excluded from
#: `_block_has_native_call`'s call-head test on the same D3-serving
#: grounds as `_PWSH_CMDLET_VERB_PATTERN`.
_PWSH_INPROCESS_ALIASES: Tuple[str, ...] = (
    "echo", "write", "select", "sls", "where", "?", "%",
    "cat", "ls", "dir", "gci", "gc", "type",
)


def _is_pwsh_inprocess_head(head: str) -> bool:
    """Whether `head` (a block sub-segment's first token) names an
    in-process PowerShell cmdlet or alias -- an approved-verb `Verb-Noun`
    cmdlet (`_PWSH_CMDLET_VERB_PATTERN`) or a known cmdlet alias
    (`_PWSH_INPROCESS_ALIASES`) -- and so spawns nothing even though it
    is a call. Case-insensitive both ways: PowerShell command-name
    matching is case-insensitive, and a hand-typed `get-childitem` or
    `ECHO` is exactly as in-process as its canonically-cased spelling.
    """
    if _PWSH_CMDLET_VERB_PATTERN.match(head):
        return True
    return head.lower() in _PWSH_INPROCESS_ALIASES


def _block_has_native_call(block_tokens: List[str]) -> bool:
    """Whether `block_tokens` (a `ForEach-Object`/`%` script block's tokens,
    braces already stripped) contains a NATIVE call -- as opposed to being
    purely a variable/member-access chain (`$_.FullName`, `$_.Name`) or an
    in-process cmdlet/alias call (`Select-Object -First 1`, `Write-Host
    $_`).

    Re-segments the block (`;`-separated statements inside one block are a
    real, if unmeasured, case) and treats a sub-segment as a native call
    only if its FIRST token (Finding 2 fix, D3):

    1. Does not start with `$` (unchanged -- a pure member-access chain is
       not a call).
    2. Is NOT an approved-verb `Verb-Noun` cmdlet
       (`_is_pwsh_inprocess_head`) -- PowerShell cmdlets run in-process by
       construction, so a `Get-ChildItem | ForEach-Object { Select-Object
       -First 1 }` block spawns nothing and must not match.
    3. Is NOT a known in-process cmdlet alias (`echo`, `select`, `%`, ...)
       -- these are cmdlet aliases on the PowerShell leg and fork nothing,
       even though the same spelling is a real bash builtin/binary on the
       bash leg.

    Where a sub-segment head is ambiguous, this prefers the FALSE
    NEGATIVE (treat it as not-a-call) over a confident wrong verdict --
    this module never denies, so silence on an unmeasured shape is always
    the safer failure than a false positive advisory (the D3
    confident-wrong-verdict failure this fix exists to close).

    Measured (D5): ``python3 script.py $_ . FullName`` (from the AC6
    `ForEach-Object` case) -> first sub-segment head `python3`, does not
    start with `$`, not an approved-verb cmdlet, not a known alias -> call,
    matches. ``$_ . Name`` (the D2/AC7 pure-property-access negative) ->
    first (only) sub-segment head `$_` -> no call, does not match.
    ``Select-Object -First 1`` -> head `Select-Object`, approved-verb
    cmdlet -> in-process, no call, does not match.
    """
    for seg_tokens, _pipe_before in segments_from_tokens_with_pipe_flag(
        block_tokens
    ):
        if not seg_tokens:
            continue
        head = seg_tokens[0]
        if head.startswith("$"):
            continue
        if _is_pwsh_inprocess_head(head):
            continue
        return True
    return False


def _detect_pipeline_foreach_object(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """Match a `ForEach-Object`/`%`/`foreach` PIPELINE-STAGE segment (D2,
    the new member) whose script block contains a native-executable call
    (`_block_has_native_call`) -- the block-content check is what separates
    a per-item spawn from an in-process pipeline stage (D3): a block doing
    pure property access spawns nothing and must not match.

    Requires `pipe_before` (the segment was fed by a `|`) -- this is what
    disambiguates the pipeline ALIAS spelling of bare `foreach`/`%` from the
    statement-keyword spelling `_detect_for_loop_pwsh` owns: a pipeline
    stage is never the command's first token and is always pipe-fed, while
    the `foreach (...) {...}` statement is never pipe-fed. No parenthesis
    inspection needed as a result.

    Only the FIRST `{`/first `}` pair is taken as the block boundary
    (nested braces inside the block are an unmeasured case, not handled
    here).

    Measured (D5) against
    ``Get-ChildItem *.py | ForEach-Object { python3 script.py $_.FullName }``
    -> segment ``['ForEach-Object', '{', 'python3', 'script.py', '$_', '.',
    'FullName', '}']``, `pipe_before=True` -> matches. Against
    ``Get-ChildItem -Recurse | % { git log -1 $_ }`` -> segment ``['%', '{',
    'git', 'log', '-', '1', '$_', '}']``, `pipe_before=True` -> matches.
    """
    for tokens, pipe_before in segments:
        if not tokens or not pipe_before:
            continue
        head = tokens[0]
        if not any(
            token_matches_binary(head, alias)
            for alias in _PWSH_FOREACH_OBJECT_ALIASES
        ):
            continue
        if "{" not in tokens or "}" not in tokens:
            continue
        start = tokens.index("{") + 1
        end = tokens.index("}")
        if end <= start:
            continue
        block = tokens[start:end]
        if _block_has_native_call(block):
            return ShapeMatch(Shape.PIPELINE_FOREACH_OBJECT, evidence=" ".join(tokens))
    return None


#: One dialect table entry's detector shape: given the FULL token stream
#: and the segmented (``;``/``&``/``|``-split) view of it, return a match or
#: ``None``. A uniform two-argument signature so the table can hold every
#: detector interchangeably even though the underlying detectors disagree
#: among themselves about which view they need (some read `segments` only,
#: `_detect_for_loop` reads `tokens` only, `_detect_while_read_loop` reads
#: both) -- the adapters below close that gap with a thin lambda apiece
#: rather than changing any detector's own signature, which would be a
#: behaviour-risking edit to logic D4 requires stay byte-for-byte frozen.
Detector = Callable[[List[str], List[Tuple[List[str], bool]]], Optional[ShapeMatch]]

#: Dialect-indexed detector table (D4): a dialect's shape set is DATA, not a
#: branch inside `classify_command`'s walk. BASH's entry reproduces
#: `SHAPE_PRECEDENCE`'s walk exactly minus `PIPELINE_FOREACH_OBJECT` (bash
#: has no such shape) -- same six shapes, same order, same detector
#: functions, adapted only to the uniform `Detector` signature above.
#:
#: POWERSHELL's entry (D2, C2 of pln-the-shape-classifier-reaches-a-e743e5):
#: six of the seven `SHAPE_PRECEDENCE` shapes, in `SHAPE_PRECEDENCE` order.
#: `GREP_VIA_BASH`/`HEAD_TAIL_PLUMBING`/`FIND_EXEC_XARGS` reuse their bash
#: detector functions UNCHANGED -- measurement (D5, the plan's own
#: pre-flight sizing) confirmed all three already key on argv[0] identity
#: at a segment head, which pwsh tokens satisfy just as well as posix ones;
#: forking them would be behaviour-identical code with no purpose.
#: `MULTI_PROBE_BANNER`/`FOR_LOOP` use forked pwsh-vocabulary predicates
#: (`_detect_multi_probe_banner_pwsh`, `_detect_for_loop_pwsh`) --
#: `Write-Host`/`Write-Output` is not `echo`/`printf`, and `foreach (...) {}`
#: is not `for...do...done` (D2's row-14-superseding note).
#: `PIPELINE_FOREACH_OBJECT` is the new member (D2), seated here immediately
#: after `FOR_LOOP` per `SHAPE_PRECEDENCE`.
#:
#: `WHILE_READ_LOOP` is DELIBERATELY ABSENT from this tuple (AC8) --
#: PowerShell has no `while read` idiom to detect. This is the stated
#: reason an absence with no comment would otherwise read as an oversight
#: to the next author, not silence to be rediscovered.
_DETECTOR_TABLE: Dict[Dialect, Tuple[Tuple[Shape, Detector], ...]] = {
    Dialect.BASH: (
        (
            Shape.GREP_VIA_BASH,
            lambda tokens, segments: _detect_grep_via_bash(segments),
        ),
        (
            Shape.MULTI_PROBE_BANNER,
            lambda tokens, segments: _detect_multi_probe_banner(segments),
        ),
        (
            Shape.HEAD_TAIL_PLUMBING,
            lambda tokens, segments: _detect_head_tail_plumbing(segments),
        ),
        (
            Shape.FOR_LOOP,
            lambda tokens, segments: _detect_for_loop(tokens),
        ),
        (
            Shape.WHILE_READ_LOOP,
            lambda tokens, segments: _detect_while_read_loop(segments, tokens),
        ),
        (
            Shape.FIND_EXEC_XARGS,
            lambda tokens, segments: _detect_find_exec_xargs(segments),
        ),
    ),
    Dialect.POWERSHELL: (
        (
            Shape.GREP_VIA_BASH,
            lambda tokens, segments: _detect_grep_via_bash(segments),
        ),
        (
            Shape.MULTI_PROBE_BANNER,
            lambda tokens, segments: _detect_multi_probe_banner_pwsh(segments),
        ),
        (
            Shape.HEAD_TAIL_PLUMBING,
            lambda tokens, segments: _detect_head_tail_plumbing(segments),
        ),
        (
            Shape.FOR_LOOP,
            lambda tokens, segments: _detect_for_loop_pwsh(tokens),
        ),
        (
            Shape.PIPELINE_FOREACH_OBJECT,
            lambda tokens, segments: _detect_pipeline_foreach_object(segments),
        ),
        # Shape.WHILE_READ_LOOP -- deliberately absent, see comment above.
        (
            Shape.FIND_EXEC_XARGS,
            lambda tokens, segments: _detect_find_exec_xargs(segments),
        ),
    ),
}

#: `guard_name` this module's own tokenization step records SILENT under
#: (via `_dialect.tokenize_command` / `record_silent`) -- `classify_command`
#: has no `guard_name` parameter of its own (AC1's signature is fixed), so a
#: stable module-level identifier stands in for "which guard called this"
#: on the out-of-band SILENT channel. Inert in production either way: that
#: channel is a no-op unless a caller opens `_verdict.collecting()`, which
#: `dispatch.py` never does (see `_verdict.py`'s own docstring).
_GUARD_NAME = "shape_classifier.classify_command"


def classify_command(
    cmd_text: str, *, dialect: Optional[Dialect] = Dialect.BASH
) -> ShapeClassification:
    """Classify `cmd_text` against the shape set for `dialect` and return a
    ``ShapeClassification`` whose ``matches`` are in that dialect's fixed
    precedence order (see module docstring; BASH's order is
    ``SHAPE_PRECEDENCE``). Never raises on ordinary unparseable input -- if
    tokenization returns ``None`` (unterminated quote / trailing backslash
    on the BASH leg, or an unparseable PowerShell command on the POWERSHELL
    leg), this returns a ``ShapeClassification`` with ``tokens=None`` and no
    matches; the fail-open-vs-fail-closed decision on an unparseable command
    belongs to the calling guard, not to this classifier (this module never
    denies).

    `dialect` is keyword-only (D1) and defaults to ``Dialect.BASH`` -- a
    caller not participating in dialect routing (i.e. every pre-existing
    caller of this function) passes nothing and gets today's behaviour
    exactly, byte-for-byte. A caller that DOES participate in dialect
    routing passes ``dialect_from_tool_name(tool_name)``, which returns
    ``None`` for an absent or unrecognized `tool_name`. An explicitly
    passed ``dialect=None`` is SILENT -- an empty classification with
    ``tokens=None`` -- and NEVER falls back to bash; the default value and
    an explicit `None` stay distinguishable by construction (defaulting to
    ``Dialect.BASH`` rather than a sentinel), which is the whole point of
    D1. See the module's spec backlink for the plan this distinction is
    load-bearing for.

    Heredoc-body stripping (2026-07-30, added alongside the shared
    tokenizer's unquoted-newline-to-``;`` pre-pass): a heredoc BODY is stdin
    DATA, never shell command text. Before that pre-pass, a heredoc body
    (whitespace-joined by the tokenizer's own newline-as-whitespace
    handling) could only be mistaken for one of these six shapes if it
    happened to contain a literal ``;``/``&``/``|`` -- narrow, and already
    an accepted limitation. Once a bare newline became a segment boundary
    too, EVERY multi-line heredoc body would fragment at each line break,
    and prose merely describing (say) a ``find ... -exec`` shape could
    classify as one. ``cmd_text`` is run through ``_strip_heredoc_bodies``
    (the same helper ``block_worktree_creation.check()`` already relies on)
    before tokenizing, so no consumer of this classifier
    (``guard_grep_via_bash``, ``guard_multiprobe_banner``,
    ``guard_head_tail_rewrite``, ``guard_plumbing_and_loops``) ever sees a
    heredoc body as a candidate segment.
    """
    if dialect is None:
        record_silent(
            _GUARD_NAME,
            "explicit dialect=None (AC3): never falls back to bash",
        )
        return ShapeClassification(tokens=None, matches=())

    cmd_text = _strip_heredoc_bodies(cmd_text)
    tokens = tokenize_command(cmd_text, dialect, guard_name=_GUARD_NAME)
    if tokens is None:
        return ShapeClassification(tokens=None, matches=())

    segments = segments_from_tokens_with_pipe_flag(tokens)

    matches: List[ShapeMatch] = []
    for shape, detector in _DETECTOR_TABLE.get(dialect, ()):
        match = detector(tokens, segments)
        if match is not None:
            matches.append(match)

    return ShapeClassification(tokens=tokens, matches=tuple(matches))
