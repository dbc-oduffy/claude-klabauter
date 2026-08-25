"""coordinator_core.search.answer -- the seam that turns a grep- or read-shaped Bash
command into an answer, or declines cleanly.

Purpose: single entry point for the PreToolUse(Bash) hook. Given a command string,
either return the exact text the command would have printed (plus a short provenance
note), or return None so existing guard behaviour proceeds untouched.

The contract is deliberately two-valued. There is no "partial answer" and no "answer
with a warning that the result may be wrong" -- an answer is faithful or it is not
offered. See `engine.Unanswerable` for why that asymmetry is the whole design.

Generalized from grep-only to source-plus-stages (C3): `plan_for` recognizes either a
grep-family invocation or a `cat`/`head`/`tail`/`sed -n` read invocation, each wrapped
in its own `engine.Source` implementation (`engine.GrepSource`, `ReadSource` below).
`answer` and `_render` never branch on which one it got -- the stage pipeline, the cap
policy, and the footer-first ordering contract are the real, shared reuse; grep
vocabulary (`files_scanned`, `cap_hit`, "(no matches)") and read vocabulary ("read
in-process", an empty body staying empty) each stay inside their own `Source`.

Negative-spec:
  - Does NOT decide the hook's verdict shape. It returns text; the caller chooses the
    envelope. Keeping the policy decision out of here means this module stays testable
    without a harness payload.
  - Does NOT mutate anything on disk. Read-only by construction.
  - Does NOT fall back to a rewrite. Declining is the fallback.
  - Does NOT gate read-shape recognition on `_Shape.GREP_VIA_BASH` or add a new
    `Shape` member -- read recognition is its own branch in `plan_for`, applying the
    identical structural pipe/compound guards the grep branch applies, for the
    identical reasons (see `_plan_for_read`'s own docstring).
  - Does NOT re-model `ls` here (C3b). `_plan_for_listdir`/`LsSource` wire
    `sources_listdir`'s already-settled flag set, decline set, and collation
    policy in; they do not re-open that contract.
  - Does NOT key PowerShell recognition (C10b) off the bash basename table.
    `_plan_for_powershell`/`PowerShellSource` route on the PowerShell TOOL
    NAME (`tool_name="PowerShell"`, plumbed through `plan_for`/`answer`),
    never off `os.path.basename(first_tokens[0])` the way the bash branches
    above do -- `ls`/`cat` are live aliases in BOTH dialects (`gc`/`type`
    resolve to `Get-Content`, `dir`/`gci` to `Get-ChildItem`) and cross-wiring
    would hand a PowerShell `ls`/`cat` token stream to the bash-shaped
    `sources_read`/`sources_listdir` parsers, which is the confidently-wrong
    failure mode `sources_powershell.py`'s own module docstring refuses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _segments,
)
from coordinator_core.bash_guards._dialect import Dialect, tokenize_command as _tokenize_command
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _Shape,
    classify_command as _classify,
)
from coordinator_core.search.engine import (
    GREP_FAMILY,
    MAX_RENDER_BYTES,
    AnswerPlan,
    GrepSource,
    SourceOutcome,
    Unanswerable,
    build_stage,
    parse_grep_segment,
)
from coordinator_core.search.sources_listdir import (
    LsSpec,
    parse_ls_segment,
    run as _run_ls,
)
from coordinator_core.search.sources_powershell import (
    ChildItemSpec,
    ContentSpec,
    parse_powershell_segment,
    run_childitem as _run_childitem,
)
from coordinator_core.search.sources_read import (
    READ_VERBS,
    ReadSpec,
    parse_read_segment,
)


@dataclass
class ReadSource:
    """`engine.Source` implementation wrapping a parsed file-read invocation
    (`sources_read.ReadSpec`). Defined here rather than in `engine.py`: `sources_read`
    already imports FROM `engine`, so `engine` importing `sources_read` back would be
    a cycle.

    Says what actually happened in ITS OWN vocabulary ("read in-process, no
    subprocess spawned") rather than reusing grep's "searched in-process: N file(s)"
    note -- there is no file count or match rate for a read, so grep's note would
    either lie or need read-shaped fields grep never populates.
    """

    spec: ReadSpec

    def execute(self, cwd: str, stop_after: Optional[int]) -> SourceOutcome:
        # `stop_after` (the stage pipeline's early-stop bound) has no read-shape
        # analogue -- a read has no "stop scanning early" concept, it reads the
        # whole (already render-cap-gated, see `clip` below) file.
        text = self.spec.produce(cwd)
        return SourceOutcome(
            lines=text.splitlines(),
            raw_text=text,
            truncated=False,
            cap_hit=None,
            note="[read in-process: no subprocess spawned]",
            empty_body_text="",
        )

    def clip(self, body: str) -> Tuple[str, bool]:
        """A read that cannot be delivered whole is not a faithful read -- decline
        outright, never render with a truncation note (C3): unlike a search, a
        clipped read body looks exactly like a complete file, and no downstream
        stage tolerance changes that (`cat big | wc -l` on a clipped body is a wrong
        count too).

        In practice this branch never fires: `ReadSpec.produce`'s own stat-gate
        (`sources_read._read_text_strict`) already declines above
        `engine.MAX_RENDER_BYTES` before any body is built, and no stage this
        package absorbs can grow a body past what was read. Kept as an explicit,
        named guard rather than an assumed invariant -- if that stat-gate's cap ever
        drifts from `MAX_RENDER_BYTES`, this is what keeps a clipped read from
        silently rendering as complete.
        """
        if len(body) <= MAX_RENDER_BYTES:
            return body, False
        raise Unanswerable("read body exceeds the render cap")

    def finalize_note(self, note_base: str, cap_hit: Optional[str], truncated: bool,
                       clipped: bool) -> str:
        # A read never truncates (declines via `Unanswerable` instead, both in
        # `execute` and in `clip` above) -- nothing to fold in.
        return note_base


@dataclass
class LsSource:
    """`engine.Source` implementation wrapping a parsed `ls` invocation
    (`sources_listdir.LsSpec`). Defined here rather than in `engine.py` for the
    same reason `ReadSource` is (C3b): `sources_listdir` already imports FROM
    `engine`, so `engine` importing it back would be a cycle.

    Own vocabulary, same as `ReadSource`: "listed in-process", no file count or
    match rate to report, and an empty directory listing renders as genuinely
    empty output rather than grep's "(no matches)".
    """

    spec: LsSpec

    def execute(self, cwd: str, stop_after: Optional[int]) -> SourceOutcome:
        # `stop_after` has no `ls` analogue, same as `ReadSource.execute` -- the
        # collated entry list is produced whole, never scanned incrementally.
        lines = _run_ls(self.spec, cwd)
        return SourceOutcome(
            lines=lines,
            raw_text=None,
            truncated=False,
            cap_hit=None,
            note="[listed in-process: no subprocess spawned]",
            empty_body_text="",
        )

    def clip(self, body: str) -> Tuple[str, bool]:
        """A clipped directory listing is not a faithful listing -- decline
        outright, mirroring `ReadSource.clip` (C3b): unlike a search, a clipped
        `ls` body looks like a complete listing with no way to tell it was cut."""
        if len(body) <= MAX_RENDER_BYTES:
            return body, False
        raise Unanswerable("ls body exceeds the render cap")

    def finalize_note(self, note_base: str, cap_hit: Optional[str], truncated: bool,
                       clipped: bool) -> str:
        # `ls` never truncates (declines via `Unanswerable` instead, both in
        # `execute` -- through `sources_listdir.run`'s own decline paths -- and in
        # `clip` above) -- nothing to fold in.
        return note_base


@dataclass
class PowerShellSource:
    """`engine.Source` implementation wrapping a parsed PowerShell read/listing
    invocation (`sources_powershell.ContentSpec`/`ChildItemSpec`, C10b). Defined here
    rather than in `engine.py` for the same reason `ReadSource`/`LsSource` are:
    `sources_powershell` already imports FROM `engine`, so `engine` importing it back
    would be a cycle.

    Own vocabulary, same split as the bash sources: a `ContentSpec` (`Get-Content`)
    reads like `ReadSource`, a `ChildItemSpec` (`Get-ChildItem`) lists like `LsSource`
    -- picked at execute-time on `isinstance`, since both specs share this one
    `Source` wrapper rather than each getting its own (there is exactly one
    dispatch point, `sources_powershell.parse_powershell_segment`, and mirroring its
    two-shape union here keeps `_plan_for_powershell` from having to branch twice).
    """

    spec: Union[ContentSpec, ChildItemSpec]

    def execute(self, cwd: str, stop_after: Optional[int]) -> SourceOutcome:
        # `stop_after` has no PowerShell-source analogue, same as
        # `ReadSource.execute`/`LsSource.execute` -- both `ContentSpec.produce` and
        # `run_childitem` produce their result whole, never scanned incrementally.
        if isinstance(self.spec, ContentSpec):
            # `newline=os.linesep`: `[Environment]::NewLine` on the box the real
            # PowerShell host would have run on IS `os.linesep` on that same box --
            # this process and that host share one platform, so the value is
            # DERIVED, not guessed (test_powershell_shapes_differential.py's own
            # `_produce_ours` establishes the identical resolution against a real
            # host). `ContentSpec.produce`'s own docstring forbids a caller from
            # guessing this argument; `os.linesep` is always establishable, so no
            # decline branch is needed here for the newline resolution itself.
            text = self.spec.produce(cwd, newline=os.linesep)
            return SourceOutcome(
                lines=text.splitlines(),
                raw_text=text,
                truncated=False,
                cap_hit=None,
                note="[read in-process: no subprocess spawned]",
                empty_body_text="",
            )
        lines = _run_childitem(self.spec, cwd)
        return SourceOutcome(
            lines=lines,
            raw_text=None,
            truncated=False,
            cap_hit=None,
            note="[listed in-process: no subprocess spawned]",
            empty_body_text="",
        )

    def clip(self, body: str) -> Tuple[str, bool]:
        """A clipped PowerShell read/listing is not a faithful one -- decline
        outright, mirroring `ReadSource.clip`/`LsSource.clip`."""
        if len(body) <= MAX_RENDER_BYTES:
            return body, False
        raise Unanswerable("PowerShell read/listing body exceeds the render cap")

    def finalize_note(self, note_base: str, cap_hit: Optional[str], truncated: bool,
                       clipped: bool) -> str:
        # Neither `Get-Content` nor `Get-ChildItem` truncates here (both decline via
        # `Unanswerable` instead, in `execute` and in `clip` above) -- nothing to
        # fold in, mirroring `ReadSource.finalize_note`/`LsSource.finalize_note`.
        return note_base


def plan_for(cmd: str, tool_name: str = "Bash") -> Optional[AnswerPlan]:
    """Build an AnswerPlan for `cmd`, or None if it is not answerable in-process.

    `tool_name` (C10b) selects the dialect: `"PowerShell"` routes through
    `_plan_for_powershell` and none of the bash branches below; anything else
    (including the default `"Bash"`, preserving every existing caller's behavior
    unchanged) takes the pre-existing bash recognition chain. Keyed off the TOOL
    NAME the guard payload already carries, never off `cmd`'s own basename table --
    see this module's own docstring negative-spec for why.
    """
    if not cmd:
        return None
    if tool_name == "PowerShell":
        return _plan_for_powershell(cmd)
    classification = _classify(cmd.replace("\r", ""))
    if classification.tokens is None:
        return None

    grep_plan = _plan_for_grep(classification)
    if grep_plan is not None:
        return grep_plan
    read_plan = _plan_for_read(classification)
    if read_plan is not None:
        return read_plan
    return _plan_for_listdir(classification)


def _plan_for_grep(classification) -> Optional[AnswerPlan]:
    """The grep branch, unchanged (AC4) -- kept as its own function so the read
    branch beside it can be added without touching this logic at all."""
    if not classification.has_shape(_Shape.GREP_VIA_BASH):
        return None
    try:
        segments = _segments(classification.tokens)
    except Exception:
        return None
    if not segments:
        return None

    first_tokens, piped_into = segments[0]
    if piped_into or not first_tokens:
        return None  # `<cmd> | grep ...` -- the input does not exist until that runs
    if os.path.basename(first_tokens[0]) not in GREP_FAMILY:
        return None
    # Every later segment must be pipe-connected. A `;`/`&&`-joined segment is separate
    # work sequenced around the grep, and answering only the grep half would drop it.
    if not all(piped for _tokens, piped in segments[1:]):
        return None

    try:
        spec = parse_grep_segment(first_tokens)
        stages = [build_stage(tokens) for tokens, _piped in segments[1:]]
    except Unanswerable:
        return None
    return AnswerPlan(source=GrepSource(spec), stages=stages)


def _plan_for_read(classification) -> Optional[AnswerPlan]:
    """Read-shape recognition (C3): deliberately NOT gated on
    `_Shape.GREP_VIA_BASH` and does not add a new `Shape` member -- a bare
    `cat`/`head`/`tail`/`sed -n` invocation is not a grep-family shape at all, so
    gating it on that shape would make it unrecognizable by construction.

    Applies the identical structural guards the grep branch applies, for the
    identical reasons: the first segment must not be piped INTO (its input does not
    exist until the upstream command runs), and every later segment must be
    pipe-connected (a `;`/`&&`-joined segment is separate work whose other half
    would be silently dropped by answering only the read).
    """
    try:
        segments = _segments(classification.tokens)
    except Exception:
        return None
    if not segments:
        return None

    first_tokens, piped_into = segments[0]
    if piped_into or not first_tokens:
        return None
    if os.path.basename(first_tokens[0]) not in READ_VERBS:
        return None
    if not all(piped for _tokens, piped in segments[1:]):
        return None

    try:
        read_spec = parse_read_segment(first_tokens)
        stages = [build_stage(tokens) for tokens, _piped in segments[1:]]
    except Unanswerable:
        return None
    return AnswerPlan(source=ReadSource(read_spec), stages=stages)


def _plan_for_listdir(classification) -> Optional[AnswerPlan]:
    """`ls`-shape recognition (C3b): the same structural guards `_plan_for_read`
    applies, for the same reasons -- the first segment must not be piped INTO,
    and every later segment must be pipe-connected.

    Deliberately checked keyed off the `ls` basename specifically, never folded
    into `READ_VERBS` -- `sources_listdir` is a separate module from
    `sources_read` for reasons its own docstring names (collation, not byte
    reproduction), and this row does not re-open that contract.
    """
    try:
        segments = _segments(classification.tokens)
    except Exception:
        return None
    if not segments:
        return None

    first_tokens, piped_into = segments[0]
    if piped_into or not first_tokens:
        return None
    if os.path.basename(first_tokens[0]) != "ls":
        return None
    if not all(piped for _tokens, piped in segments[1:]):
        return None

    try:
        ls_spec = parse_ls_segment(first_tokens)
        stages = [build_stage(tokens) for tokens, _piped in segments[1:]]
    except Unanswerable:
        return None
    return AnswerPlan(source=LsSource(ls_spec), stages=stages)


def _plan_for_powershell(cmd: str) -> Optional[AnswerPlan]:
    """PowerShell-shape recognition (C10b): tokenizes through
    `_dialect.tokenize_command(cmd, Dialect.POWERSHELL, ...)` -- the ONE
    dialect-aware tokenizer this repo already built for a PowerShell payload
    (`sources_powershell.py`'s own module docstring, "Tokenization discipline")
    -- rather than `_classify`/`_segments`, which are POSIX-`shlex`-shaped and
    never see a PowerShell payload correctly.

    Applies the identical structural guards `_plan_for_read`/`_plan_for_listdir`
    apply, for the identical reasons: the first segment must not be piped INTO,
    and every later segment must be pipe-connected -- an unrecognized downstream
    PowerShell cmdlet declines the whole plan via `build_stage`'s own bash-verb-
    keyed lookup raising `Unanswerable` (no PowerShell stage vocabulary exists,
    so any downstream segment declines by construction; this module does not
    invent one).
    """
    try:
        tokens = _tokenize_command(cmd, Dialect.POWERSHELL, guard_name="search.answer")
    except Exception:
        return None
    if tokens is None:
        return None
    try:
        segments = _segments(tokens)
    except Exception:
        return None
    if not segments:
        return None

    first_tokens, piped_into = segments[0]
    if piped_into or not first_tokens:
        return None
    if not all(piped for _tokens, piped in segments[1:]):
        return None

    try:
        spec = parse_powershell_segment(first_tokens)
        stages = [build_stage(tokens) for tokens, _piped in segments[1:]]
    except Unanswerable:
        return None
    return AnswerPlan(source=PowerShellSource(spec), stages=stages)


def answer(cmd: str, cwd: str = ".", tool_name: str = "Bash") -> Optional[str]:
    """Return the rendered answer for `cmd`, or None to decline."""
    plan = plan_for(cmd, tool_name=tool_name)
    if plan is None:
        return None
    try:
        outcome = plan.source.execute(cwd, plan.early_stop)
    except Unanswerable:
        return None
    except OSError:
        return None

    # A truncated source feeding a stage that aggregates or reads from the end
    # produces a confidently wrong number. Decline rather than caveat it -- the
    # agent has no reason to re-check a plausible-looking result.
    if outcome.truncated and not plan.tolerates_truncation:
        return None

    lines: List[str] = outcome.lines
    for stage in plan.stages:
        try:
            lines = stage.apply(lines)
        except Unanswerable:
            return None

    if not plan.stages and outcome.raw_text is not None:
        # A bare, stage-free read renders from its own raw-text representation,
        # verbatim -- no line-list round-trip, which is what would silently add a
        # trailing newline a file never had, or collapse a CRLF terminator.
        body = outcome.raw_text
    else:
        body = "\n".join(lines)

    try:
        body, clipped = plan.source.clip(body)
    except Unanswerable:
        return None

    note = plan.source.finalize_note(outcome.note, outcome.cap_hit, outcome.truncated,
                                     clipped)
    return _render(body, note, outcome.empty_body_text)


def _render(body: str, note: str, empty_body_text: str) -> str:
    """Wrap the source's own output in a short, honest provenance note.

    The output comes FIRST and verbatim: the agent asked a question and this is the
    answer to it. Provenance goes underneath, where it informs the next invocation
    without displacing the result.

    Never branches on source type (C3): `note` and `empty_body_text` are already
    fully composed by the `Source` that produced `outcome` in `answer()` above --
    grep's "(no matches)" is search vocabulary that must not apply to a read's
    genuinely empty output (`cat empty.txt` prints nothing, not a literal string),
    so a read source hands this an empty `empty_body_text` instead.
    """
    if not body:
        body = empty_body_text
    return body + "\n\n" + note
