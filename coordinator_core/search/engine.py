"""coordinator_core.search.engine -- answer a grep-shaped Bash command IN-PROCESS.

Purpose: parse a grep-family Bash command (optionally with pipe-connected downstream
stages), execute the search inside the calling process, and render exactly what the
command would have printed -- so the coordinator PreToolUse(Bash) hook can ANSWER a
search instead of spawning one.

Why in-process is the whole point
---------------------------------
The coordinator PreToolUse(Bash) dispatcher is already a Python process, spawned on
every Bash tool call (`coordinator/hooks/hooks.json` -> `preuse-bash-dispatch.py`).
That interpreter startup -- measured ~38ms on the authoring host -- is paid
unconditionally, before any guard logic runs. A search executed in THAT process
therefore costs zero additional forks.

This is the only construction under which the claim the guard package used to make
("the harness can do this search in-process / no subprocess fork") is actually true.
The shipped alternative it replaced rewrote the command into a SECOND `python3 -c`
subprocess -- which pays the same startup again and saves no fork at all on the
dominant shape.

Why it also absorbs the downstream pipeline stage
--------------------------------------------------
Measured against a 69,329-command corpus: bare single-segment grep -- the only shape a
Bash-to-Bash rewrite can even attempt -- is 2.7% of real usage. 97.3% is a pipeline or
chain, and the single most common real shape is `grep -n PAT path | <cmd>`. A rewrite
can only ever replace the grep segment, leaving the downstream fork in place; answering
in-process collapses `| head -20` into a slice and `| wc -l` into a count, taking the
whole command to zero forks.

Negative-spec -- what this module deliberately does NOT do:
  - Does NOT answer a command whose grep is fed by an upstream stage
    (`<cmd> | grep ...`). That upstream output does not exist until the upstream command
    runs, so there is nothing to search. ~74% of real search-shaped commands are this
    shape and they correctly stay on bash; this module refuses rather than guessing.
  - Does NOT answer a `;`/`&&`-joined compound. Other real work is sequenced around the
    grep, and answering only the grep half would silently drop it.
  - Does NOT translate regex dialects itself. `coordinator_core.search.regex_translate`
    owns that, refusal discipline included; a second translation here would drift.
  - Does NOT answer when a cap truncated the result AND a downstream stage needs the
    complete input (`wc`, `sort`, `uniq`, `tail`). A truncated count is a confidently
    wrong answer, which is worse than any refusal -- see `Stage.needs_complete_input`.
  - Does NOT emit or apply a rewrite. It either answers fully and faithfully, or returns
    None and lets existing guard behaviour proceed untouched.
  - Does NOT treat an unreadable file as an empty one. A file the process cannot open
    is a hole in the result set, not an absence of matches, and under concurrent peers
    the holes land on the files being edited. See `_read_text`.
  - Does NOT treat an operand the shell would have resolved as an opaque filename. See
    `_expand_targets`: an unexpanded glob or a nonexistent operand refuses, because both
    otherwise render as an authoritative "(no matches)" for a search that never ran.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from coordinator_core.search.regex_translate import translate as _translate_pattern

#: Hot-path caps. This module runs inside the PreToolUse hook, which gates EVERY Bash
#: call -- unbounded work here does not merely slow one command, it wedges the session.
#: These are correctness-relevant, not defensive garnish: exceeding one is what flips an
#: answer into a refusal when a downstream stage cannot tolerate truncation.
#:
#: MAX_PROCESS_SECONDS is measured against `time.process_time()` (this process's own
#: user+system CPU time), never wall clock. `time.perf_counter()` -- what this constant
#: was checked against before -- is wall clock, the instrument CLAUDE.md forbids for
#: exactly this reason ("process time and spawn count, never wall clock -- wall clock
#: measures peer load"): under the box's normal 50-70 concurrent sessions, a wall-clock
#: cap fires EARLIER the busier the box is, truncating answers that would have completed
#: and charging this search for load it did not cause. Observed live on this box: an
#: ordinary two-term grep returned "2500ms ... truncated at the time-cap" under load that
#: never touched the CPU this search itself consumed.
#:
#: `benchmarks.process_time.batched_process_time_ms` is NOT reused here -- that module
#: measures a SPAWNED subprocess tree's rusage from the parent (job object / kevent
#: reap), a different question from this call's own in-process CPU consumption.
#: `time.process_time()` answers this call's question directly, with no subprocess to
#: attach to.
#:
#: 500ms is DR-344's brightline (`docs/decisions/DR-344-the-brightline-process-budget-
#: for-claude-klabauter.md` -- "process its entire job in 500ms end-to-end, including under an
#: [everyday] load"), not a preserved fraction of the old 2.5s wall-clock number.
MAX_PROCESS_SECONDS = 0.5
MAX_FILES_SCANNED = 20000
MAX_MATCH_LINES = 2000
MAX_RENDER_BYTES = 48_000

#: Directories that are never what an agent means by "search the repo". Real grep walks
#: them and floods the result set -- a measured `grep -rn coordinator .` at the authoring
#: repo root emits 76MB across 162,502 lines. Pruning by default is a capability the
#: sanctioned path has and raw grep does not; adoption depends on this path being BETTER
#: than bash, not merely equivalent.
DEFAULT_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".next", "site-packages", ".gradle", "target",
})

GREP_FAMILY = ("grep", "egrep", "fgrep", "rg")

#: Short flags that consume a value, either glued (`-A2`) or as the next token (`-A 2`).
_VALUE_FLAGS = frozenset({"A", "B", "C", "m", "e"})

#: Long options accepted, mapped to the short flag whose behaviour they duplicate.
_LONG_BOOL = {
    "recursive": "r", "line-number": "n", "ignore-case": "i",
    "files-with-matches": "l", "count": "c", "word-regexp": "w",
    "invert-match": "v", "extended-regexp": "E", "fixed-strings": "F",
    "no-filename": "h", "with-filename": "H", "only-matching": "o",
}
_LONG_VALUE = frozenset({"include", "exclude", "exclude-dir", "max-count", "regexp",
                         "after-context", "before-context", "context"})

#: Flags accepted and then ignored because they do not change what we would print in the
#: cases we answer. Listed explicitly rather than silently swallowed -- an unrecognized
#: flag must always refuse, so the tolerated set has to be enumerable and reviewable.
_TOLERATED_NOOP = frozenset({"s", "a", "I", "H"})


class Unanswerable(Exception):
    """Raised when a command cannot be answered faithfully in-process.

    Every raise site is a deliberate refusal. Refusing costs one bash call; answering
    wrongly costs the agent's trust in every subsequent answer, because it has no reason
    to re-check a result that looks plausible.
    """


# --------------------------------------------------------------------------- specs


@dataclass
class SearchSpec:
    """A parsed grep-family invocation, dialect-resolved but not yet compiled."""

    pattern: str
    targets: List[str] = field(default_factory=list)
    dialect: str = "basic"
    recursive: bool = False
    ignore_case: bool = False
    line_numbers: bool = False
    files_only: bool = False
    count_only: bool = False
    only_matching: bool = False
    word: bool = False
    invert: bool = False
    no_filename: bool = False
    with_filename: bool = False
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    exclude_dir: List[str] = field(default_factory=list)
    after: int = 0
    before: int = 0
    max_count: Optional[int] = None


@dataclass
class Stage:
    """One absorbed downstream pipeline stage.

    `needs_complete_input` is the correctness-critical field: a stage that aggregates or
    reads from the end of the stream (`wc`, `sort`, `uniq`, `tail`) produces a WRONG
    answer if the search was truncated by a cap, so its presence forces a refusal rather
    than a caveat.
    """

    name: str
    apply: Callable[[List[str]], List[str]]
    needs_complete_input: bool
    #: Number of leading lines this stage ultimately keeps, when knowable -- lets the
    #: search stop early instead of scanning a whole tree to then discard the remainder.
    early_stop: Optional[int] = None


@dataclass
class SourceOutcome:
    """One source's own execution outcome -- the generalized successor to a bare
    `SearchResult` (C3). Every field a source-type-specific fact must feed is named
    here explicitly, so `answer._render` never has to branch on source identity to
    decide what to print.

    `lines`: line-list representation, always present -- used whenever a downstream
      stage is applied (`cat f | wc -l` is genuinely line-shaped) or a source has no
      raw-text representation of its own (grep).
    `raw_text`: exact bytes-as-text with terminators preserved (final-newline
      presence, CRLF, ...), or None for a source with no such representation. Used
      ONLY for a stage-free source whose deliverable is verbatim bytes (a bare
      `cat f`) -- round-tripping that through `lines` and rejoining with `"\n"`
      cannot reproduce a missing final newline or a CRLF terminator.
    `truncated`: whether a hot-path cap cut this source's own output short, BEFORE
      any downstream stage or render-cap clip. Interacts with
      `Stage.needs_complete_input` identically for every source kind -- a truncated
      result feeding an aggregating/tail-reading stage is a confidently wrong
      answer regardless of source (`answer.answer` owns that shared check).
    `cap_hit`: which cap tripped `truncated`, or None -- grep-specific vocabulary a
      read source never populates (nothing to say if it never truncates).
    `note`: fully rendered, source-specific provenance text -- grep vocabulary
      (`files_scanned`, `elapsed_ms`) stays inside the source that produced it, per
      `_render`'s own contract (C3): it appends whichever note the source built,
      never constructs one itself.
    `empty_body_text`: what `_render` prints in place of an EMPTY body -- grep's is
      "(no matches)" (this IS search vocabulary and does not apply to a read: `cat
      empty.txt` prints nothing, not "(no matches)"); a read source's is "".
    """

    lines: List[str]
    raw_text: Optional[str]
    truncated: bool
    cap_hit: Optional[str]
    note: str
    empty_body_text: str


class Source:
    """Source protocol (C3): the shared shape every answerable command class
    implements so the stage pipeline, the cap policy, and the footer-first
    ordering contract stay genuinely shared while grep vocabulary and read
    vocabulary each stay inside their own implementation.

    Duck-typed rather than an ABC -- a concrete implementation (`GrepSource` here,
    `answer.ReadSource` in the answer-assembly module, which cannot import this
    module's own downstream `sources_read` without a cycle) need only provide these
    three methods with these signatures:

      `execute(cwd, stop_after) -> SourceOutcome` -- run the source, or raise
        `Unanswerable`/`OSError` to decline.
      `clip(body) -> (str, bool)` -- apply this source's OWN render-cap policy to
        the fully-assembled body (post-stages). A search may clip-with-caveat
        (returns `(clipped_body, True)`); a read must decline outright on the same
        condition (raises `Unanswerable`) -- see `answer.ReadSource.clip`'s
        docstring for why that divergence is deliberate, not an oversight.
      `finalize_note(note_base, cap_hit, truncated, clipped) -> str` -- fold the
        execution outcome's truncation/clip facts into the note text, in this
        source's OWN vocabulary.
    """

    def execute(self, cwd: str, stop_after: Optional[int]) -> SourceOutcome:  # pragma: no cover
        raise NotImplementedError

    def clip(self, body: str) -> Tuple[str, bool]:  # pragma: no cover
        raise NotImplementedError

    def finalize_note(self, note_base: str, cap_hit: Optional[str], truncated: bool,
                       clipped: bool) -> str:  # pragma: no cover
        raise NotImplementedError


@dataclass
class GrepSource(Source):
    """`Source` implementation wrapping a parsed grep-family invocation. Folds
    `files_scanned`/`cap_hit` into ITS OWN note text -- grep vocabulary stays here,
    never leaking into `answer._render` (C3)."""

    spec: SearchSpec

    def execute(self, cwd: str, stop_after: Optional[int]) -> SourceOutcome:
        result = run(self.spec, cwd=cwd, stop_after=stop_after)
        note = (
            "[searched in-process: %d file(s), %.0fms, no subprocess spawned]"
            % (result.files_scanned, result.elapsed_ms)
        )
        return SourceOutcome(
            lines=result.lines,
            raw_text=None,
            truncated=result.truncated,
            cap_hit=result.cap_hit,
            note=note,
            empty_body_text="(no matches)",
        )

    def clip(self, body: str) -> Tuple[str, bool]:
        """A clipped SEARCH result is still an honest answer with a caveat -- a
        search never declines outright on the render cap alone (C3)."""
        if len(body) <= MAX_RENDER_BYTES:
            return body, False
        return body[:MAX_RENDER_BYTES].rsplit("\n", 1)[0], True

    def finalize_note(self, note_base: str, cap_hit: Optional[str], truncated: bool,
                       clipped: bool) -> str:
        if truncated or clipped:
            note_base += (
                "\n[truncated at the %s -- narrow it with --include='*.py' or a "
                "tighter path to see the rest]" % (cap_hit or "render cap")
            )
        return note_base


@dataclass
class AnswerPlan:
    source: Source
    stages: List[Stage]

    @property
    def tolerates_truncation(self) -> bool:
        return not any(s.needs_complete_input for s in self.stages)

    @property
    def early_stop(self) -> Optional[int]:
        """Smallest leading-line bound across stages, if every stage has one."""
        bounds = [s.early_stop for s in self.stages]
        if not bounds or any(b is None for b in bounds):
            return None
        return min(b for b in bounds if b is not None)


# -------------------------------------------------------------------------- parsing


def parse_grep_segment(tokens: Sequence[str]) -> SearchSpec:
    """Parse one grep-family argv into a SearchSpec, or raise Unanswerable."""
    if not tokens:
        raise Unanswerable("empty grep segment")
    binary = os.path.basename(tokens[0])
    spec = SearchSpec(pattern="")
    if binary == "egrep":
        spec.dialect = "extended"
    elif binary == "fgrep":
        spec.dialect = "fixed"
    elif binary == "rg":
        # ripgrep's defaults differ from grep's in ways that change output shape, not
        # just performance: recursive and line-numbered by default, Rust-regex dialect.
        spec.dialect = "extended"
        spec.recursive = True
        spec.line_numbers = True

    # Review: coordinator:code-reviewer -- F9: track "was -e seen" explicitly rather than
    # round-tripping through spec.pattern's truthiness, which mis-parses `-e ''` (a legal
    # empty-pattern invocation) by falling through to consume the first operand as if no
    # flag pattern had been supplied.
    e_seen = False
    operands: List[str] = []
    i, n = 1, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            operands.extend(tokens[i + 1:])
            break
        if tok.startswith("--"):
            name, _, inline = tok[2:].partition("=")
            if name in _LONG_BOOL:
                _apply_short_flag(spec, _LONG_BOOL[name])
                i += 1
                continue
            if name in _LONG_VALUE:
                if inline:
                    value, i = inline, i + 1
                else:
                    if i + 1 >= n:
                        raise Unanswerable("long option --%s is missing its value" % name)
                    value, i = tokens[i + 1], i + 2
                _apply_long_value(spec, name, value)
                if name == "regexp":
                    e_seen = True
                continue
            raise Unanswerable("unsupported long option --%s" % name)
        if tok.startswith("-") and len(tok) > 1:
            i, e_seen = _consume_short_cluster(spec, tokens, i, e_seen)
            continue
        operands.append(tok)
        i += 1

    if not e_seen:
        if not operands:
            raise Unanswerable("no search pattern operand")
        spec.pattern = operands.pop(0)
    spec.targets = list(operands) or ["."]
    if spec.only_matching:
        # -o changes what is printed per match, not merely which lines match. Faithful
        # support needs per-match spans rather than per-line output; refuse until built.
        raise Unanswerable("-o (only-matching) output shape not implemented")
    return spec


def _consume_short_cluster(spec: SearchSpec, tokens: Sequence[str], i: int,
                           e_seen: bool) -> Tuple[int, bool]:
    """Consume one `-xyz`-style token, returning (next index, updated e_seen)."""
    tok = tokens[i]
    j = 1
    while j < len(tok):
        ch = tok[j]
        if ch in _VALUE_FLAGS:
            glued = tok[j + 1:]
            if glued:
                value, nxt = glued, i + 1
            else:
                if i + 1 >= len(tokens):
                    raise Unanswerable("-%s is missing its value" % ch)
                value, nxt = tokens[i + 1], i + 2
            if ch == "e" and e_seen:
                # Multiple -e patterns are a real grep feature (implicit alternation) but
                # combining them correctly is dialect-sensitive; refuse rather than guess.
                raise Unanswerable("multiple -e patterns not supported")
            _apply_value_flag(spec, ch, value)
            if ch == "e":
                e_seen = True
            return nxt, e_seen
        _apply_short_flag(spec, ch)
        j += 1
    return i + 1, e_seen


def _apply_value_flag(spec: SearchSpec, ch: str, value: str) -> None:
    if ch in ("A", "B", "C"):
        try:
            count = int(value)
        except ValueError:
            raise Unanswerable("-%s expects an integer, got %r" % (ch, value))
        if ch in ("A", "C"):
            spec.after = count
        if ch in ("B", "C"):
            spec.before = count
    elif ch == "m":
        try:
            spec.max_count = int(value)
        except ValueError:
            raise Unanswerable("-m expects an integer, got %r" % value)
    elif ch == "e":
        # Duplicate-`-e` refusal lives in `_consume_short_cluster`, keyed off `e_seen`
        # rather than `spec.pattern`'s truthiness -- an empty `-e ''` pattern is falsy
        # but still a legitimate, already-supplied pattern (F9).
        spec.pattern = value


def _apply_long_value(spec: SearchSpec, name: str, value: str) -> None:
    if name == "include":
        spec.include.append(value)
    elif name == "exclude":
        spec.exclude.append(value)
    elif name == "exclude-dir":
        spec.exclude_dir.append(value)
    elif name == "max-count":
        spec.max_count = int(value)
    elif name == "regexp":
        spec.pattern = value
    elif name in ("after-context", "before-context", "context"):
        count = int(value)
        if name in ("after-context", "context"):
            spec.after = count
        if name in ("before-context", "context"):
            spec.before = count


def _apply_short_flag(spec: SearchSpec, ch: str) -> None:
    if ch in ("r", "R"):
        spec.recursive = True
    elif ch == "n":
        spec.line_numbers = True
    elif ch == "i":
        spec.ignore_case = True
    elif ch == "l":
        spec.files_only = True
    elif ch == "c":
        spec.count_only = True
    elif ch == "w":
        spec.word = True
    elif ch == "v":
        spec.invert = True
    elif ch == "o":
        spec.only_matching = True
    elif ch == "h":
        spec.no_filename = True
    elif ch == "E":
        spec.dialect = "extended"
    elif ch == "F":
        spec.dialect = "fixed"
    elif ch == "P":
        raise Unanswerable("-P (PCRE) has no provable Python re equivalent")
    elif ch == "f":
        raise Unanswerable("-f reads patterns from a file")
    elif ch == "H":
        spec.with_filename = True
    elif ch in _TOLERATED_NOOP:
        return
    else:
        raise Unanswerable("unsupported short flag -%s" % ch)


def compile_spec(spec: SearchSpec) -> "re.Pattern[str]":
    """Compile a SearchSpec's pattern, delegating dialect translation."""
    source = _translate_pattern(spec.pattern, spec.dialect)
    if source is None:
        raise Unanswerable("regex dialect not provably translatable")
    if spec.word:
        source = r"\b(?:%s)\b" % source
    try:
        return re.compile(source, re.IGNORECASE if spec.ignore_case else 0)
    except re.error as exc:
        raise Unanswerable("translated pattern did not compile: %s" % exc)


# ------------------------------------------------------------------ downstream stages


def _stage_head(args: Sequence[str]) -> Stage:
    count = _line_count_arg(args, default=10)
    return Stage("head", lambda lines: lines[:count], False, early_stop=count)


def _stage_tail(args: Sequence[str]) -> Stage:
    count = _line_count_arg(args, default=10)
    # Reading from the END means a truncated search silently yields the wrong window.
    return Stage("tail", lambda lines: lines[-count:], True)


def _stage_wc(args: Sequence[str]) -> Stage:
    """Absorb `wc -l` as a line count.

    KNOWN, DELIBERATE DIVERGENCE: BSD `wc` (macOS) right-pads its count to width 8,
    GNU `wc` (Linux) does not. Reproducing the host's padding faithfully would mean
    probing the host's own `wc` -- a process spawn, which is precisely the cost this
    package exists to avoid. So the count is emitted unpadded on every platform.
    The VALUE is always correct; only the leading whitespace differs, and only against
    BSD. Declared here rather than discovered later by a reader diffing outputs.
    """
    # Review: coordinator:code-reviewer -- F2: bare `wc` (no flags) prints three numbers
    # (lines, words, bytes), not a line count. Only `-l` is absorbed; `[]` used to be
    # treated as equivalent to `["-l"]`, which silently answered a three-number command
    # with a single number.
    if list(args) != ["-l"]:
        raise Unanswerable("only `wc -l` is absorbed")
    return Stage("wc", lambda lines: ["%d" % len(lines)], True)


def _stage_sort(args: Sequence[str]) -> Stage:
    unique = False
    for a in args:
        if a in ("-u", "--unique"):
            unique = True
        else:
            raise Unanswerable("unsupported sort option %r" % a)

    def apply(lines: List[str]) -> List[str]:
        out = sorted(lines)
        if unique:
            deduped: List[str] = []
            for ln in out:
                if not deduped or deduped[-1] != ln:
                    deduped.append(ln)
            return deduped
        return out

    return Stage("sort", apply, True)


def _stage_uniq(args: Sequence[str]) -> Stage:
    counting = False
    for a in args:
        if a in ("-c", "--count"):
            counting = True
        else:
            raise Unanswerable("unsupported uniq option %r" % a)

    def apply(lines: List[str]) -> List[str]:
        out: List[str] = []
        for ln in lines:
            if out and _uniq_key(out[-1], counting) == ln:
                if counting:
                    n, text = out[-1].split(" ", 1)
                    out[-1] = "%d %s" % (int(n) + 1, text)
                continue
            out.append(("1 %s" % ln) if counting else ln)
        return out

    return Stage("uniq", apply, True)


def _uniq_key(rendered: str, counting: bool) -> str:
    return rendered.split(" ", 1)[1] if counting and " " in rendered else rendered


def _stage_grep_filter(args: Sequence[str]) -> Stage:
    """A downstream `grep` used as a line filter over the previous stage's output."""
    spec = parse_grep_segment(["grep", *args])
    if spec.targets != ["."] or spec.recursive:
        raise Unanswerable("downstream grep names its own targets")
    if spec.count_only or spec.files_only:
        raise Unanswerable("downstream grep -c/-l changes output shape")
    if spec.after or spec.before:
        # Review: coordinator:code-reviewer -- F3: the piped lines are already-rendered
        # strings by the time they reach this filter stage, so reconstructing -A/-B/-C
        # context from them is not faithfully derivable (the surrounding lines that
        # would satisfy the window may already have been filtered out upstream, or
        # never rendered at all). Refuse rather than silently dropping the flag.
        raise Unanswerable("downstream grep -A/-B/-C context is not derivable from piped lines")
    rx = compile_spec(spec)
    invert = spec.invert

    def apply(lines: List[str]) -> List[str]:
        return [ln for ln in lines if bool(rx.search(ln)) != invert]

    return Stage("grep", apply, False)


def _stage_cut(args: Sequence[str]) -> Stage:
    delim, fields = "\t", None
    it = list(args)
    i = 0
    while i < len(it):
        a = it[i]
        if a.startswith("-d"):
            delim = a[2:] or (it[i + 1] if i + 1 < len(it) else "\t")
            i += 1 if a[2:] else 2
            continue
        if a.startswith("-f"):
            raw = a[2:] or (it[i + 1] if i + 1 < len(it) else "")
            i += 1 if a[2:] else 2
            if not raw.isdigit():
                raise Unanswerable("only a single numeric cut -f field is absorbed")
            fields = int(raw)
            continue
        raise Unanswerable("unsupported cut option %r" % a)
    if fields is None:
        raise Unanswerable("cut without -f")

    def apply(lines: List[str]) -> List[str]:
        out = []
        for ln in lines:
            parts = ln.split(delim)
            out.append(parts[fields - 1] if len(parts) >= fields else "")
        return out

    return Stage("cut", apply, False)


def _line_count_arg(args: Sequence[str], default: int) -> int:
    it = list(args)
    for idx, a in enumerate(it):
        if a == "-n":
            if idx + 1 >= len(it):
                raise Unanswerable("head/tail -n missing its value")
            return int(it[idx + 1])
        if a.startswith("-n") and a[2:].isdigit():
            return int(a[2:])
        if a.startswith("-") and a[1:].isdigit():
            return int(a[1:])
        raise Unanswerable("unsupported head/tail option %r" % a)
    return default


_STAGE_BUILDERS = {
    "head": _stage_head,
    "tail": _stage_tail,
    "wc": _stage_wc,
    "sort": _stage_sort,
    "uniq": _stage_uniq,
    "cut": _stage_cut,
    "grep": _stage_grep_filter,
    "egrep": _stage_grep_filter,
    "fgrep": _stage_grep_filter,
}


def build_stage(tokens: Sequence[str]) -> Stage:
    if not tokens:
        raise Unanswerable("empty pipeline stage")
    verb = os.path.basename(tokens[0])
    builder = _STAGE_BUILDERS.get(verb)
    if builder is None:
        raise Unanswerable("downstream stage %r is not absorbable" % verb)
    return builder(tokens[1:])


# ------------------------------------------------------------------------- execution


@dataclass
class SearchResult:
    lines: List[str]
    files_scanned: int
    truncated: bool
    cap_hit: Optional[str]
    elapsed_ms: float


#: How often (in scanned lines) the render-mode hit-collection loop re-checks the wall
#: clock. Checking every line would add measurable per-line overhead; checking only
#: between files (the pre-F8 behaviour) lets one pathological file blow the whole
#: MAX_WALL_SECONDS budget before any cap engages. This is the compromise.
_WALL_CHECK_STRIDE = 4096


#: Characters that make an operand a shell glob rather than a filename. `{`/`}` (brace
#: expansion) is deliberately absent: a brace operand that does not exist on disk already
#: refuses via the existence check below, and one that DOES exist is a literal filename.
_GLOB_METACHARS = ("*", "?", "[")


def _expand_targets(targets: Sequence[str], cwd: str) -> List[str]:
    """Resolve grep's operands the way the shell resolves them before grep is exec'd.

    The hook sees the command as TYPED, so a glob operand arrives unexpanded. Treating
    it as a filename is the defect this function exists to close: `_read_text` returns
    None for the nonexistent literal `dir/*.py`, `scan` counts it as one file scanned,
    and the caller is handed `(no matches)` plus `[searched in-process: 1 file(s)]` for
    a search that never looked at anything. That shape is the module's worst failure
    mode -- confidently wrong rather than refused -- and it cost an architecture-survey
    analyst a near-miss "zero registered ops" conclusion (2026-08-06 survey, digest #1).

    Refuses rather than approximating in the two cases the shell's own behaviour makes
    unreproducible:
      - an unmatched glob: bash without `nullglob` passes the pattern through literally
        and grep exits 2 with a stderr diagnostic this seam cannot emit;
      - a nonexistent plain operand: same stderr/exit-2 shape.

    Quoting is not recoverable here -- the shared tokenizer is `shlex`-based and strips
    it -- so a QUOTED glob (which the shell would not expand) expands anyway. That is a
    deliberate, bounded divergence: the pre-existing behaviour for the same input was a
    silent wrong `(no matches)`, so expanding is strictly better on every input, and the
    honest alternative (refuse every glob) would drop a shape that is a large share of
    real explicitly-targeted searches.
    """
    resolved: List[str] = []
    for target in targets:
        if any(ch in target for ch in _GLOB_METACHARS):
            # Non-recursive: bash without `globstar` treats `**` as `*`, and so does
            # `glob.glob` with `recursive=False`. `root_dir` keeps the expansion
            # cwd-relative, which is what the shell would have written into argv and
            # therefore what `scan`'s `shown` must echo.
            matches = sorted(glob.glob(target, root_dir=cwd, recursive=False))
            if not matches:
                raise Unanswerable("glob operand %r matches nothing" % target)
            resolved.extend(matches)
            continue
        base = target if os.path.isabs(target) else os.path.join(cwd, target)
        if not os.path.exists(base):
            raise Unanswerable("target %r does not exist" % target)
        resolved.append(target)
    return resolved


#: Metacharacters that make a read-source operand something other than a plain,
#: literal filename. Brace is included here (unlike `_GLOB_METACHARS` above) because
#: a read source declines on a glob/brace operand outright rather than expanding it
#: -- see `resolve_plain_path_operand`.
_GLOB_OR_BRACE_METACHARS = ("*", "?", "[", "{")


def resolve_plain_path_operand(operand: str, cwd: str) -> str:
    """Resolve one path operand the way a file-read source (cat/head/tail/sed -n)
    must: relative operands join against `cwd`, absolute operands pass through
    unchanged -- matching `_expand_targets`'s own absolute-operand handling, since
    there is no cwd-confinement to reuse (`_expand_targets` has none either; grep
    the module for `commonpath`/`realpath`). Declines on any glob/brace
    metacharacter or a path that is not an existing, readable regular file.

    Deliberately NOT the same policy as `_expand_targets`: that function EXPANDS
    an unquoted glob operand, because the shell would have expanded it before grep
    ever ran and a glob is exactly what real grep receives on that argv position.
    A read source's real command (`cat a*.txt`) never resolves that way either --
    the shell expands it into a list `cat` then reads in expanded order, which
    this seam has no shell to perform -- so it declines on the metacharacter
    instead of guessing an expansion, per the read-source contract (C1).
    """
    if any(ch in operand for ch in _GLOB_OR_BRACE_METACHARS):
        raise Unanswerable("glob/brace operand %r not supported" % operand)
    base = operand if os.path.isabs(operand) else os.path.join(cwd, operand)
    if not os.path.isfile(base):
        raise Unanswerable("operand %r is not a readable regular file" % operand)
    if not os.access(base, os.R_OK):
        raise Unanswerable("operand %r is not readable" % operand)
    return base


def run(spec: SearchSpec, cwd: str = ".", stop_after: Optional[int] = None) -> SearchResult:
    """Execute a SearchSpec in-process, honoring the module's hot-path caps."""
    rx = compile_spec(spec)
    # Before ANY target-shaped decision below -- the filename-prefix rule and the
    # include/exclude divergence check both key off the operand COUNT, which is a
    # post-expansion property of the argv grep actually receives.
    targets = _expand_targets(spec.targets, cwd)
    prune = DEFAULT_PRUNE_DIRS | set(spec.exclude_dir)
    limit = MAX_MATCH_LINES if stop_after is None else min(stop_after, MAX_MATCH_LINES)

    # grep prefixes each line with the filename only when more than one file is in
    # play -- a single named file gets bare output. Getting this wrong makes every
    # single-file answer differ from the real command in a way that looks like a
    # cosmetic detail and is not: agents pipe this output into other tools.
    searching_a_tree = spec.recursive and any(
        os.path.isdir(t if os.path.isabs(t) else os.path.join(cwd, t))
        for t in targets
    )
    show_name = spec.with_filename or (
        (searching_a_tree or len(targets) > 1) and not spec.no_filename
    )

    # Review: coordinator:code-reviewer -- F5: GNU grep applies --include/--exclude only
    # to files discovered by recursive walk; explicitly-named target arguments are always
    # searched. BSD grep (verified on this host, 2026-07-29: `grep --include='*.py' -n
    # alpha a.txt` on a `.txt` file containing "alpha" returns no match) applies the
    # filter to explicitly-named targets too -- the two dialects genuinely disagree here,
    # the same class of split already handled in `regex_translate.py`'s anchor-position
    # divergence. Rather than pick a dialect, refuse when both are in play.
    if spec.include or spec.exclude:
        for target in targets:
            base = target if os.path.isabs(target) else os.path.join(cwd, target)
            if not os.path.isdir(base):
                raise Unanswerable(
                    "--include/--exclude on an explicitly-named target diverges "
                    "between GNU and BSD grep"
                )

    out: List[str] = []
    files_scanned = 0
    truncated = False
    cap_hit: Optional[str] = None
    # (shown, end_lineno) of the last rendered context group, or None if nothing has
    # been rendered yet -- used to decide whether the next group needs a `--` separator
    # (F4). Crossing a file boundary always breaks adjacency, which falls out for free
    # here because `shown` changes.
    last_group: Optional[Tuple[str, int]] = None
    started = time.process_time()

    def budget_exhausted() -> bool:
        nonlocal truncated, cap_hit
        if len(out) >= limit:
            truncated, cap_hit = True, "match-cap"
            return True
        if files_scanned > MAX_FILES_SCANNED:
            truncated, cap_hit = True, "file-cap"
            return True
        if time.process_time() - started > MAX_PROCESS_SECONDS:
            # DECLINE, not truncate: match-cap/file-cap bound a legitimate result
            # SIZE the agent can narrow with --include or a tighter path -- the time
            # budget bounds this call's own COST, and a search that is already this
            # expensive in-process is exactly the shape real grep (a separate process,
            # off this hook's own budget) should run instead. Truncating here would
            # serve a confidently partial answer while still having spent the full
            # budget, on a path that gates every Bash call in the session.
            raise Unanswerable("search exceeded the %.1fs process-time budget" % MAX_PROCESS_SECONDS)
        return False

    def scan(path: str, shown: str) -> bool:
        """Scan one file. Returns False when the caller must stop entirely.

        `path` is the filesystem path to read; `shown` is the path as the caller wrote
        it, which is what gets printed -- grep echoes the operand it was given, so
        resolving to an absolute path here would silently change every output line.
        """
        nonlocal files_scanned, truncated, cap_hit, last_group
        name = os.path.basename(path)
        if spec.include and not any(fnmatch.fnmatch(name, p) for p in spec.include):
            return True
        if spec.exclude and any(fnmatch.fnmatch(name, p) for p in spec.exclude):
            return True
        if budget_exhausted():
            return False
        files_scanned += 1
        text = _read_text(path)
        if text is None:
            return True
        lines = text.splitlines()

        # `-l`/`-c` never need matched line TEXT, only presence/count -- storing full
        # (idx, line) tuples for every match on a file where most lines match is exactly
        # the unbounded-accumulation hazard F8 flags, so these two shapes are handled
        # without ever building a `hits` list.
        if spec.files_only:
            for line in lines:
                if bool(rx.search(line)) != spec.invert:
                    out.append(shown)
                    return len(out) < limit
            return True

        if spec.count_only:
            # Review: coordinator:code-reviewer -- F1: real `grep -c` always emits one
            # count line per searched file, including "0" for a zero-match file (`-l`
            # correctly omits zero-match files; `-c` does not). Emitting unconditionally
            # here -- rather than only after an `if not hits` guard -- is the fix.
            count = 0
            checked_at = 0
            for idx, line in enumerate(lines, 1):
                if bool(rx.search(line)) != spec.invert:
                    count += 1
                    if spec.max_count and count >= spec.max_count:
                        break
                if idx - checked_at >= _WALL_CHECK_STRIDE:
                    checked_at = idx
                    if time.process_time() - started > MAX_PROCESS_SECONDS:
                        # DECLINE (see budget_exhausted's own comment): a partial
                        # count would be a confidently wrong number, and this search
                        # is already too expensive in-process to keep going.
                        raise Unanswerable(
                            "search exceeded the %.1fs process-time budget" % MAX_PROCESS_SECONDS
                        )
            out.append("%s:%d" % (shown, count) if show_name else str(count))
            return True

        hits: List[Tuple[int, str]] = []
        checked_at = 0
        for idx, line in enumerate(lines, 1):
            if bool(rx.search(line)) != spec.invert:
                hits.append((idx, line))
                if spec.max_count and len(hits) >= spec.max_count:
                    break
                if len(hits) >= MAX_MATCH_LINES:
                    # F8: bound hit-collection regardless of `spec.max_count` -- a file
                    # where most/all lines match must not accumulate unboundedly before
                    # the render loop's own cap gets a chance to engage.
                    truncated, cap_hit = True, "match-cap"
                    break
            if idx - checked_at >= _WALL_CHECK_STRIDE:
                checked_at = idx
                if time.process_time() - started > MAX_PROCESS_SECONDS:
                    # DECLINE (see budget_exhausted's own comment).
                    raise Unanswerable(
                        "search exceeded the %.1fs process-time budget" % MAX_PROCESS_SECONDS
                    )
        if not hits:
            return True

        groups, matched = _merge_windows(hits, spec.before, spec.after, len(lines))
        has_context = bool(spec.before or spec.after)
        for lo, hi in groups:
            if has_context and last_group is not None and not (
                last_group[0] == shown and lo <= last_group[1] + 1
            ):
                # F4: real grep inserts a bare `--` line between two context groups
                # that are not adjacent/overlapping -- within one file, or across a
                # file boundary (verified differentially both ways).
                out.append("--")
                if len(out) >= limit:
                    truncated, cap_hit = True, "match-cap"
                    return False
            for ctx_no in range(lo, hi + 1):
                rendered = _render_line(shown, ctx_no, lines[ctx_no - 1], spec, show_name,
                                        separator=":" if ctx_no in matched else "-")
                out.append(rendered)
                if len(out) >= limit:
                    truncated, cap_hit = True, "match-cap"
                    return False
            last_group = (shown, hi)
        if cap_hit == "match-cap" and truncated:
            # The hit-collection loop itself tripped the F8 cap partway through this
            # file -- stop entirely rather than silently moving on to the next file
            # having only rendered a prefix of this one's matches.
            return False
        return True

    for target in targets:
        base = target if os.path.isabs(target) else os.path.join(cwd, target)
        if os.path.isdir(base):
            if not spec.recursive:
                continue
            stop = False
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in prune]
                relative = os.path.relpath(root, base)
                for filename in sorted(files):
                    shown = (os.path.join(target, filename) if relative == "."
                             else os.path.join(target, relative, filename))
                    if not scan(os.path.join(root, filename), shown):
                        stop = True
                        break
                if stop:
                    break
            if stop:
                break
        else:
            if not scan(base, target):
                break

    return SearchResult(out, files_scanned, truncated, cap_hit,
                        (time.process_time() - started) * 1000)


def _merge_windows(hits: List[Tuple[int, str]], before: int, after: int,
                   nlines: int) -> Tuple[List[Tuple[int, int]], "set[int]"]:
    """Collapse per-hit `-A`/`-B`/`-C` windows into merged, non-overlapping groups.

    Real grep de-duplicates a line that falls inside two overlapping/adjacent context
    windows rather than printing it twice (verified differentially: two matches one
    line apart with `-A1 -B1` print each shared line once). `matched` is every line
    number that was itself a match, independent of grouping, so the caller can render
    the `:`/`-` separator correctly per line.
    """
    groups: List[List[int]] = []
    matched = {lineno for lineno, _ in hits}
    for lineno, _ in hits:
        lo = max(1, lineno - before)
        hi = min(nlines, lineno + after)
        if groups and lo <= groups[-1][1] + 1:
            groups[-1][1] = max(groups[-1][1], hi)
        else:
            groups.append([lo, hi])
    return [(lo, hi) for lo, hi in groups], matched


def _render_line(path: str, lineno: int, line: str, spec: SearchSpec,
                 show_name: bool, separator: str) -> str:
    prefix = ""
    if show_name:
        prefix += path + separator
    if spec.line_numbers:
        prefix += "%d%s" % (lineno, separator)
    return prefix + line


def _read_text(path: str) -> Optional[str]:
    """Read a file as text, None if binary, or raise Unanswerable if unreadable.

    Binary detection mirrors grep's own NUL-byte heuristic rather than guessing at
    encodings -- grep prints `Binary file X matches` and does not dump content, so
    skipping is closer to the caller's expectation than emitting mojibake. A binary
    file contributing nothing is FAITHFUL; the caller keeps scanning.

    An OSError is not. This used to return None for both cases, which made a file
    the process could not open indistinguishable from a file with no matches -- and
    on Windows the dominant cause of that OSError is a sharing violation from one of
    the peers concurrently editing the same tree, so the omission is not random but
    concentrated on exactly the files under active work. The caller was then handed
    an authoritative `(no matches)` for a search that skipped them, the identical
    confidently-wrong shape `_expand_targets` exists to close. Real grep reports the
    unreadable path on stderr and exits 2; this seam has no stderr, so it refuses
    and the caller pays one bash spawn for a complete answer.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
            if b"\x00" in head:
                return None
            rest = handle.read()
    except OSError as exc:
        raise Unanswerable("cannot read %r: %s" % (path, exc))
    return (head + rest).decode("utf-8", errors="replace")
