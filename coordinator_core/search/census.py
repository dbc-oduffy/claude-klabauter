"""coordinator_core.search.census -- answer a `find`-shaped CENSUS command IN-PROCESS,
sibling to `sources_listdir.py`/`sources_read.py` (`answer.py`'s existing answerers) but
consumed directly by `coordinator_core.bash_guards.guard_head_tail_rewrite` rather than
through `answer.py`/`engine.py` -- this module answers only the narrow upstream shape
that guard already recognizes feeding `head`/`tail`, not a general search surface.

Purpose: given the tokens of a `find`-invocation segment (no `-exec` -- that shape
belongs to `check_find_exec_rewrite`), either return the exact set of paths real `find`
would print for that segment (as an unsorted list the caller sorts, matching
`guard_head_tail_rewrite`'s own SORTED-for-determinism choice, since real `find`'s
traversal order is not guaranteed reproducible across a rewrite), or raise
`coordinator_core.search.engine.Unanswerable`.

Why this module exists (C1, docs/plans/2026-08-21-the-advisory-band-gets-smaller-
cheaper-and-honest.md) -- and why it is NOT `guard_head_tail_rewrite`'s existing
generator
----------------------------------------------------------------------------------------
`guard_head_tail_rewrite._bt_build_generator_lines`'s `find` branch does not walk or
slice anything itself -- it emits PYTHON SOURCE STRINGS assembled into a `python3 -c`
one-liner the agent is told to run instead. That is a real fork+exec, one FEWER than the
two-stage pipeline it replaces, never zero. This module is the actual in-process
evaluator ASK-1 asked for: it runs the walk here, in the hook's own process, so the
guard can SERVE the answer (zero additional forks) rather than hand back a command to
run. C0's spike (`docs/research/2026-09-10-in-process-census-evaluator-spike.md`)
bounds the shape this module is allowed to claim before any of it was built; its
verdict (`(a)`) is the recognized-subset contract this module's parser encodes.

Negative-spec -- what this module deliberately does NOT do (per C0's spike, AC4, AC13):
  - Does NOT recognize `-exec` (owned by `check_find_exec_rewrite`), a shell redirection
    or substitution token in the segment, more than one `-name`, more than one `-type`,
    or any `-type` value other than `f` -- including `-type d`. C0's spike flagged that
    `guard_head_tail_rewrite._bt_parse_find_census_segment` (the GENERATOR this module's
    sibling rewrite path falls back to) silently treats `-type d` the same as no `-type`
    at all (`only_files = (pred[i+1] == "f")`). That is a latent bug in the generator
    being measured, not a shape C0's oracle corpus exercised -- this module does not
    reproduce it or attempt to fix it standalone; it simply declines `-type d` like every
    other flag its corpus never certified, per AC4's "never guess" and AC13's "shape the
    corpus does not cover is a shape the evaluator must return None for".
  - Does NOT recognize any other `find` predicate: `-iname`, `-mtime`, `-size`,
    `-maxdepth`, `-prune`, `-not`, `-o`/`-or`, `-regex`, and so on. Every one of these is
    corpus-uncovered per C0's spike -- declined by name, not by omission.
  - Does NOT walk unbounded. `WALK_BUDGET_ENTRIES` bails to `Unanswerable` mid-walk on an
    oversized tree rather than occupying the hook's synchronous PreToolUse budget past
    its slice (C0's spike § (c); AC12). A caller that receives `Unanswerable` here must
    fall back to the existing generator-rewrite path, never guess at a partial answer.
  - Does NOT sort inside `run()`. The caller (`guard_head_tail_rewrite`) already sorts
    the generator's own output for determinism; this module returns the same unsorted
    list shape `os.walk` produces per directory so a caller applying its own sort gets
    byte-identical results, and so a caller checking real `find`'s own unsorted order
    (as AC13's oracle for the `-type f`-only, no-`-name` row wants to, since bare
    unsorted `find` order is what the real binary prints) can compare directly.

Spec backlink: docs/plans/2026-08-21-the-advisory-band-gets-smaller-cheaper-and-honest.md
row C1; C0's spike: docs/research/2026-09-10-in-process-census-evaluator-spike.md
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import dataclass
from typing import List, Optional, Sequence

from coordinator_core.search.engine import Unanswerable

#: Sized from C0's spike measurement (docs/research/2026-09-10-in-process-census-
#: evaluator-spike.md § (c)): ~1.5us/entry measured against this repo's own working
#: tree (40.9ms p50 / 26,055 entries), so 100,000 entries costs roughly 150ms --
#: comfortably under DR-344's per-guard slice with headroom, and well short of the
#: ~330,000-entry knee the spike extrapolates for a full unfiltered walk to approach
#: the whole chain's 500ms brightline. A tree that exceeds this bails to `Unanswerable`
#: mid-walk (never a truncated or wrong census) rather than occupying the hook past its
#: budget -- the exact constant is this chunk's to set per the spike's own closing note,
#: not re-derived from a guess.
WALK_BUDGET_ENTRIES = 100_000

#: Redirection operators that must decline the whole segment if present as an exact
#: token -- checked before any flag/operand parsing, same discipline
#: `sources_listdir.py`'s `_reject_redirection_and_substitution` applies to `ls`.
_REDIRECT_TOKENS = frozenset({
    ">", ">>", "<", "<<", "<<<", "2>", "&>", "|&",
})

#: Substring markers of command/process substitution -- checked as substrings because
#: the shared tokenizer does not split `$(...)`/`` `...` ``/`<(...)` off from an
#: adjoining token. Mirrors `sources_listdir.py`'s identical constant.
_SUBSTITUTION_MARKERS = ("$(", "`", "<(", "$")


@dataclass
class FindCensusSpec:
    """A parsed `find`-census invocation: path, optional name glob, files-only."""

    path: str = "."
    name_pattern: Optional[str] = None
    only_files: bool = False


def _reject_redirection_and_substitution(tokens: Sequence[str]) -> None:
    for tok in tokens:
        if tok in _REDIRECT_TOKENS:
            raise Unanswerable("redirection token %r in find segment" % tok)
        if any(marker in tok for marker in _SUBSTITUTION_MARKERS):
            raise Unanswerable("substitution token in find operand %r" % tok)


def parse_find_census_segment(tokens: Sequence[str]) -> FindCensusSpec:
    """Parse a `find`-invocation segment into a FindCensusSpec, or raise Unanswerable.

    Recognizes only the shape C0's differential oracle measured faithful: a bare path
    operand (default `.`), at most one `-name PATTERN` (`fnmatch.fnmatchcase`,
    case-sensitive -- matches real `find` on every platform), and at most one
    `-type f` -- exactly. Everything else declines by name; see the module docstring's
    Negative-spec for why `-type d` is a decline here rather than a reproduction of the
    generator's own latent bug.
    """
    if not tokens:
        raise Unanswerable("empty find segment")
    binary = os.path.basename(tokens[0])
    if binary != "find":
        raise Unanswerable("not a find command: %r" % binary)
    if "-exec" in tokens:
        raise Unanswerable("find -exec is check_find_exec_rewrite's shape, not this one")

    pred = list(tokens[1:])
    _reject_redirection_and_substitution(pred)

    path = "."
    i = 0
    if pred and not pred[0].startswith("-"):
        path = pred[0]
        i = 1
    if any(ch in path for ch in ("*", "?", "[")):
        raise Unanswerable("glob path operand %r not supported" % path)

    name_pattern: Optional[str] = None
    only_files = False
    seen_name = False
    seen_type = False
    while i < len(pred):
        tok = pred[i]
        if tok == "-name" and i + 1 < len(pred):
            if seen_name:
                raise Unanswerable("multiple -name predicates not supported")
            name_pattern = pred[i + 1]
            seen_name = True
            i += 2
            continue
        if tok == "-type" and i + 1 < len(pred):
            if seen_type:
                raise Unanswerable("multiple -type predicates not supported")
            if pred[i + 1] != "f":
                raise Unanswerable(
                    "only -type f is a certified census shape, not %r" % pred[i + 1]
                )
            only_files = True
            seen_type = True
            i += 2
            continue
        raise Unanswerable("unrecognized find predicate %r" % tok)

    return FindCensusSpec(path=path, name_pattern=name_pattern, only_files=only_files)


def run(spec: FindCensusSpec, cwd: str = ".") -> List[str]:
    """Execute a FindCensusSpec, returning the paths real `find` would print for it.

    Walks with the process cwd temporarily set to `cwd` (restored in `finally`, even on
    an exception) so `os.walk(spec.path)` joins paths the identical way the guard's own
    generated `python3 -c` script would when it actually ran as a subprocess inheriting
    that same shell cwd -- this is what keeps the emitted path PREFIXES byte-identical
    to real `find`'s own output (`find src -name '*.py'` prints `src/foo.py`, not an
    absolute or differently-rooted path). The hook process this runs in is short-lived
    and single-threaded (same premise `guard_inprocess_search.py`'s own module docstring
    states for its locale/session-latch mutations), so a temporary cwd change is safe
    for the duration of one call.

    Declines (raises Unanswerable) rather than approximating on: a nonexistent path, a
    file operand (not a directory), an unreadable directory, or a walk that exceeds
    `WALK_BUDGET_ENTRIES` before finishing.
    """
    original_cwd = os.getcwd()
    try:
        try:
            os.chdir(cwd)
        except OSError as exc:
            raise Unanswerable("cannot enter cwd %r: %s" % (cwd, exc))

        if not os.path.exists(spec.path):
            raise Unanswerable("find target %r does not exist" % spec.path)
        if not os.path.isdir(spec.path):
            raise Unanswerable("find target %r is not a directory" % spec.path)

        out: List[str] = []
        scanned = 0

        def _matches(name: str) -> bool:
            return spec.name_pattern is None or fnmatch.fnmatchcase(name, spec.name_pattern)

        # Real `find` always visits the starting point itself as its own
        # first entry (a directory, so excluded when `only_files` is set,
        # same as every other directory this walk sees) -- `os.walk` never
        # yields its own root as one of the (root, dirs, files) triples it
        # produces, so it must be added here explicitly or a bare
        # `find .`/`find DIR` census silently drops one entry real `find`
        # prints. `os.path.basename` of the operand itself (not a joined
        # path) is what `-name` matches against, matching real find's own
        # basename-of-the-argument behavior for the start point.
        if not spec.only_files and _matches(os.path.basename(os.path.normpath(spec.path))):
            scanned += 1
            if scanned > WALK_BUDGET_ENTRIES:
                raise Unanswerable(
                    "walk exceeded the %d-entry budget -- bail to None per "
                    "C0's measured bound rather than occupy the hook past "
                    "its slice" % WALK_BUDGET_ENTRIES
                )
            out.append(spec.path)

        try:
            walker = os.walk(spec.path)
            for root, dirs, files in walker:
                entries = files if spec.only_files else files + dirs
                for fn in entries:
                    scanned += 1
                    if scanned > WALK_BUDGET_ENTRIES:
                        raise Unanswerable(
                            "walk exceeded the %d-entry budget -- bail to None per "
                            "C0's measured bound rather than occupy the hook past "
                            "its slice" % WALK_BUDGET_ENTRIES
                        )
                    if _matches(fn):
                        out.append(posixpath.join(root, fn))
        except OSError as exc:
            raise Unanswerable("cannot walk %r: %s" % (spec.path, exc))
        return out
    finally:
        os.chdir(original_cwd)
