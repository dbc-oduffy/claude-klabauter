"""coordinator_core.bash_guards._shape_classifier -- tokenizer-based shape
classifier for the six ranked bash-spawn shapes identified by the
1,389-transcript / 62,487-call fork-tax measurement
(``state/plan-sidecars/2026-07-28-bash-tax-negative-space.md``,
Coordinator-claude ``docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md`` C2, AC-2):
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

``Shape`` -- an ``str`` `Enum` naming the six shapes.

``SHAPE_PRECEDENCE`` -- the six shapes in fixed precedence order:
``GREP_VIA_BASH > MULTI_PROBE_BANNER > HEAD_TAIL_PLUMBING > FOR_LOOP >
WHILE_READ_LOOP > FIND_EXEC_XARGS``. ``WHILE_READ_LOOP`` sits immediately
after ``FOR_LOOP`` and before ``FIND_EXEC_XARGS`` -- it is the structural
twin of the for-loop shape (spawn-per-iteration) and was unmeasured, not
measured at zero, in the founding sample, so it inherits its position from
that twin rather than carrying a rank of its own (docs/plans/2026-08-10-
the-one-fan-out-shape-the-classifier-nev.md § Design decisions already
made). This order is LOAD-BEARING, not incidental: the six shapes overlap
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

``classify_command(cmd_text: str) -> ShapeClassification`` -- the one
entry point. Never raises on ordinary unparseable/empty input (mirrors
``tokenize_full_command``'s own fail-return-None contract); a genuine
internal exception indicates a programming bug in this module, not user
input.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from ._command_tokenizer import (
    segments_from_tokens_with_pipe_flag,
    token_matches_binary,
    tokenize_full_command,
)
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


def _detect_multi_probe_banner(
    segments: List[Tuple[List[str], bool]]
) -> Optional[ShapeMatch]:
    """Match if the command contains a banner-marked ``echo``/``printf``
    segment (see ``_has_banner_marker``) AND the command has at least
    ``_MIN_BANNER_SEGMENTS`` total segments -- a banner label alone
    (``echo "=== status ==="; git status``, 2 segments) is a single
    labeled probe, not the N-unrelated-probes-in-one-call shape this
    detector targets.
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


def classify_command(cmd_text: str) -> ShapeClassification:
    """Classify `cmd_text` against all six ranked shapes and return a
    ``ShapeClassification`` whose ``matches`` are in fixed
    ``SHAPE_PRECEDENCE`` order (see module docstring). Never raises on
    ordinary unparseable input -- if ``tokenize_full_command`` returns
    ``None`` (unterminated quote / trailing backslash), this returns a
    ``ShapeClassification`` with ``tokens=None`` and no matches; the
    fail-open-vs-fail-closed decision on an unparseable command belongs to
    the calling guard, not to this classifier (this module never denies).

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
    cmd_text = _strip_heredoc_bodies(cmd_text)
    tokens = tokenize_full_command(cmd_text)
    if tokens is None:
        return ShapeClassification(tokens=None, matches=())

    segments = segments_from_tokens_with_pipe_flag(tokens)

    matches: List[ShapeMatch] = []
    for shape in SHAPE_PRECEDENCE:
        match: Optional[ShapeMatch]
        if shape is Shape.GREP_VIA_BASH:
            match = _detect_grep_via_bash(segments)
        elif shape is Shape.MULTI_PROBE_BANNER:
            match = _detect_multi_probe_banner(segments)
        elif shape is Shape.HEAD_TAIL_PLUMBING:
            match = _detect_head_tail_plumbing(segments)
        elif shape is Shape.FOR_LOOP:
            match = _detect_for_loop(tokens)
        elif shape is Shape.WHILE_READ_LOOP:
            match = _detect_while_read_loop(segments, tokens)
        elif shape is Shape.FIND_EXEC_XARGS:
            match = _detect_find_exec_xargs(segments)
        else:  # pragma: no cover -- exhaustive over SHAPE_PRECEDENCE
            match = None
        if match is not None:
            matches.append(match)

    return ShapeClassification(tokens=tokens, matches=tuple(matches))
