"""coordinator_core.bash_guards._guard_coverage -- standing, re-runnable
measurement of how much of its own target command class each of the three
machine-tax-shaped guards (``check_probe_spray``, ``check_runaway_find``,
``check_offer_git_c``) actually fires on.

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md
row BX-11 / AC-6 (example-doctrine-repo). BX-11's own words are the reason this module
exists rather than a one-off comment: "A guard that cannot state its own
reach is how this family got to 0.44% without anyone noticing." The
one-time hand analysis that FIRST produced 0.44% / 1.4% / 3.2% lives at
state/plan-sidecars/2026-07-28-bash-tax-negative-space.md (example-doctrine-repo) --
an ad-hoc scratchpad script run once against a 62,487-command, 1,389-
transcript corpus, never re-run and never callable on demand. This module
is that same measurement made standing: importable, re-runnable against any
corpus shaped the same way, and driven by the SHIPPED guard functions
themselves rather than a hand-copied re-implementation of their logic.

Negative-space note on WHY the measured percentages below will not be
bit-identical to the 0.44%/1.4%/3.2% baseline, and why that is not a bug:
the baseline's target-class denominators (2,259 ``find`` calls; 18,685
``cd``-prefixed calls) were counted with a raw regex over untokenized
command text -- exactly the class of quote-blind matcher this package's own
history (five such matchers found and fixed in a single day) argues against
building more of. This module counts target-class membership with the
canonical tokenizer (``_command_tokenizer.tokenize_full_command``) instead,
which fails CLOSED (does not count a command as a target-class member) on
an unparseable/multiline-malformed command rather than guessing at it with
a regex -- so its denominators are usually smaller and its guard-body
invocations are exactly the shipped functions, not a re-derivation of their
logic. The baseline numbers remain the reference point this module reports
against (``BASELINE_PCT``); they are not treated as ground truth to be
reproduced bit-for-bit.

This module ships NO real corpus. The 62,487-command transcript corpus
behind the original baseline is real session data (working directories,
file paths, command history) and must never be committed. Every entrypoint
here takes a corpus path or an iterable of command strings; ``main()``'s
default corpus resolution is an explicit env var
(``COORDINATOR_GUARD_COVERAGE_CORPUS``) that is required to point at a real
file -- there is no bundled fallback, and a missing corpus is reported as a
hard failure (a coverage number measured against an invented corpus is
worse than no number at all).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from coordinator_core.bash_guards import dispatch_checks as _checks
from coordinator_core.bash_guards import guard_offer_git_c as _guard_offer_git_c
from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_simple,
    token_matches_binary,
    tokenize_full_command,
)

# The 2026-07-28 negative-space baseline this module measures against --
# state/plan-sidecars/2026-07-28-bash-tax-negative-space.md (example-doctrine-repo)
# § Existing-guard coverage detail. Do not "correct" these to match a later
# re-measurement; they are the fixed historical reference point, not a
# rolling value.
BASELINE_PCT: Dict[str, float] = {
    "check_probe_spray": 0.44,
    "check_runaway_find": 1.4,
    "check_offer_git_c": 3.2,
}


def iter_corpus_commands(path: str) -> Iterator[str]:
    """Yield each Bash command string from a JSONL corpus file, one JSON
    object per line, command text under the ``"c"`` key (the shape the
    2026-07-28 baseline extraction produced) with ``"command"``/``"cmd"``
    accepted as fallback keys for a hand-built corpus. Lines that fail to
    parse as JSON, or whose command field is not a string, are skipped
    rather than raising -- a single malformed line must not abort a
    62,487-line corpus read."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            cmd = row.get("c")
            if cmd is None:
                cmd = row.get("command")
            if cmd is None:
                cmd = row.get("cmd")
            if isinstance(cmd, str):
                yield cmd


def is_find_invocation(cmd: str) -> bool:
    """Target-class predicate for ``check_runaway_find``: does ``cmd``
    invoke ``find`` anywhere in its tokenized form. Tokenizer-based, never a
    raw-text regex, so a quoted string or heredoc body containing the
    literal word "find" is not miscounted as a find invocation. Fails
    CLOSED (returns False) when the command does not tokenize (unterminated
    quote, trailing backslash) -- consistent with the fail-closed direction
    every guard in this package already takes on the same condition."""
    toks = tokenize_full_command(cmd)
    if toks is None:
        return False
    return any(token_matches_binary(t, "find") for t in toks)


def is_leading_cd(cmd: str) -> bool:
    """Target-class predicate for ``check_offer_git_c``: does ``cmd`` open
    with ``cd`` as the very first token of its first segment -- the
    "cd-prefixed command" class the 2026-07-28 baseline's 18,685-command
    denominator was measured against. Tokenizer-based; fails CLOSED on an
    unparseable command."""
    toks = tokenize_full_command(cmd)
    if toks is None:
        return False
    segs = segments_from_tokens_simple(toks)
    if not segs or not segs[0]:
        return False
    return token_matches_binary(segs[0][0], "cd")


@dataclass
class CoverageResult:
    """One guard's measured reach against its own target command class."""

    guard: str
    target_class_size: int
    fired_count: int
    corpus_size: int
    measured_pct: float
    baseline_pct: float

    @property
    def delta_pct(self) -> float:
        """Positive: this run reaches further than the 2026-07-28 baseline.
        Negative: reach has regressed relative to it."""
        return self.measured_pct - self.baseline_pct


def _pct(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def measure_runaway_find(commands: Sequence[str]) -> CoverageResult:
    """Measure ``check_runaway_find``'s reach: of every command that
    invokes ``find`` at all, how many does the shipped guard function
    actually deny. Stateless -- each command is checked independently, no
    corpus ordering assumed."""
    target = [c for c in commands if is_find_invocation(c)]
    fired = sum(1 for c in target if _checks.check_runaway_find(c) is not None)
    return CoverageResult(
        guard="check_runaway_find",
        target_class_size=len(target),
        fired_count=fired,
        corpus_size=len(commands),
        measured_pct=_pct(fired, len(target)),
        baseline_pct=BASELINE_PCT["check_runaway_find"],
    )


def measure_offer_git_c(commands: Sequence[str]) -> CoverageResult:
    """Measure ``check_offer_git_c``'s reach: of every leading-``cd``
    command, how many does the shipped guard function actually rewrite or
    deny (any non-``None`` return -- allow-rewrite and deny both count as
    "reached", since both mean the guard recognized and acted on the
    shape). Stateless."""
    target = [c for c in commands if is_leading_cd(c)]
    fired = sum(1 for c in target if _guard_offer_git_c.check_offer_git_c(c) is not None)
    return CoverageResult(
        guard="check_offer_git_c",
        target_class_size=len(target),
        fired_count=fired,
        corpus_size=len(commands),
        measured_pct=_pct(fired, len(target)),
        baseline_pct=BASELINE_PCT["check_offer_git_c"],
    )


def measure_probe_spray(commands: Sequence[str]) -> CoverageResult:
    """Measure ``check_probe_spray``'s reach against its full target class
    (every command in the corpus -- the guard's charter is any probe-shaped
    command, not a pre-filtered subset).

    ``check_probe_spray`` is the one guard in this trio carrying real
    cross-call state (a same-session recurrence ring plus a threshold/
    cooldown window -- see its own docstring in ``dispatch_checks.py``).
    The 2026-07-28 baseline's "273 of 62,487" figure measured shape
    RECOGNITION, not real multi-call sequencing (it was a static
    classification pass, not a session replay). To measure the same thing
    here without hand-duplicating the guard's internal is-probe regex
    (which would silently drift from the shipped classifier the moment
    either copy changed), each command is replayed through the REAL
    ``check_probe_spray`` function: a fresh session id per call, inside one
    shared scratch tempdir for the whole measurement run (state filenames
    are keyed by session id, so no cross-call state leak occurs despite the
    directory being shared), with the module's weak-probe threshold
    and cooldown window patched to 1/0 for the duration of this call --
    generalizing the guard's own existing ``is_strong_probe`` short-circuit
    (which already sets ``effective_threshold = 1``) to every probe shape,
    for measurement purposes only. The patch is undone in a ``finally``
    block on every exit path, so a caller never observes a mutated guard
    module afterward."""
    scratch = tempfile.mkdtemp(prefix="guard-coverage-probe-spray-")
    orig_tempdir = tempfile.tempdir
    orig_threshold = _checks._THRESHOLD
    orig_cooldown = _checks._COOLDOWN
    tempfile.tempdir = scratch
    _checks._THRESHOLD = 1
    _checks._COOLDOWN = 0
    try:
        fired = 0
        for cmd in commands:
            sid = uuid.uuid4().hex
            if _checks.check_probe_spray(cmd, session_id=sid) is not None:
                fired += 1
    finally:
        tempfile.tempdir = orig_tempdir
        _checks._THRESHOLD = orig_threshold
        _checks._COOLDOWN = orig_cooldown
        shutil.rmtree(scratch, ignore_errors=True)

    n = len(commands)
    return CoverageResult(
        guard="check_probe_spray",
        target_class_size=n,
        fired_count=fired,
        corpus_size=n,
        measured_pct=_pct(fired, n),
        baseline_pct=BASELINE_PCT["check_probe_spray"],
    )


MEASURERS: Dict[str, Callable[[Sequence[str]], CoverageResult]] = {
    "check_runaway_find": measure_runaway_find,
    "check_offer_git_c": measure_offer_git_c,
    "check_probe_spray": measure_probe_spray,
}


def measure_all(commands: Sequence[str]) -> List[CoverageResult]:
    """Run every registered guard's coverage measurement against the same
    corpus, in a stable, deterministic order."""
    return [MEASURERS[name](commands) for name in ("check_probe_spray", "check_runaway_find", "check_offer_git_c")]


def format_report(results: Iterable[CoverageResult]) -> str:
    lines = ["guard | target_class | fired | measured_% | baseline_% | delta_%"]
    for r in results:
        lines.append(
            "%s | %d | %d | %.2f%% | %.2f%% | %+.2f%%"
            % (r.guard, r.target_class_size, r.fired_count, r.measured_pct, r.baseline_pct, r.delta_pct)
        )
    return "\n".join(lines)


def _default_corpus_path() -> Optional[str]:
    env = os.environ.get("COORDINATOR_GUARD_COVERAGE_CORPUS")
    if env and os.path.isfile(env):
        return env
    return None


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Report measured per-guard coverage of check_probe_spray, "
            "check_runaway_find, and check_offer_git_c against a real "
            "Bash command corpus, relative to the 2026-07-28 negative-"
            "space baseline (0.44% / 1.4% / 3.2%)."
        )
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help=(
            'Path to a JSONL corpus (one {"c": "<command>"} object per '
            "line). Falls back to $COORDINATOR_GUARD_COVERAGE_CORPUS."
        ),
    )
    args = parser.parse_args(argv)

    corpus_path = args.corpus or _default_corpus_path()
    if not corpus_path or not os.path.isfile(corpus_path):
        if corpus_path:
            print(
                "Corpus path %r does not exist or is not a file." % corpus_path,
                file=sys.stderr,
            )
        print(
            "No corpus supplied -- pass --corpus <path.jsonl> or set "
            "$COORDINATOR_GUARD_COVERAGE_CORPUS.\n\n"
            "A corpus is produced by streaming every Bash tool_use command "
            'out of ~/.claude/projects/*/*.jsonl into {"c": "<command>"} '
            "lines. This tool never invents a corpus: a coverage "
            "number measured against a corpus nobody can point to is worse "
            "than no number.",
            file=sys.stderr,
        )
        return 2

    commands = list(iter_corpus_commands(corpus_path))
    if not commands:
        print("Corpus at %r contained zero commands." % corpus_path, file=sys.stderr)
        return 2

    results = measure_all(commands)
    print(format_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
