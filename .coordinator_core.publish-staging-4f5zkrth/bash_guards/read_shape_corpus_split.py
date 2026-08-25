"""coordinator_core.bash_guards.read_shape_corpus_split -- standing, re-runnable
measurement of how much of a real Bash command corpus is read-shaped (the
`cat`/`head`/`tail`/`sed -n`/`ls` vocabulary `search/sources_read.py` and
`search/sources_listdir.py` model), how much of THAT `search.answer.plan_for`
now answers in-process, and -- for the remainder -- WHY each command declined,
bucketed by cause.

Spec backlink: docs/plans/2026-08-22-a-bash-call-stops-costing-a-second-and-a-half.md
row C5 / AC7. C5 is deliberately sequenced ahead of the serve chunks (C1-C4):
"Independent of C1-C4 -- it measures the corpus, not the implementation, and
can run first." Whatever `search.answer.plan_for` answers AT THE TIME this
module runs is what gets counted as "answered" -- this module never
re-implements `plan_for`'s predicate, so its "answered" count is honest even
before C1-C4 land (today, that count is the grep-only baseline; once C1-C4
ship, re-running this module against the same corpus moves the split without
this module changing at all).

The remainder bucketing is a SEPARATE, independent classifier -- not a
re-derivation of `plan_for`'s internal logic, but a structural read of each
declined command against the vocabulary C1/C2's chunk bodies describe (the
allowed flags, the single-vs-multiple-operand rule per family, the
redirect/substitution/glob decline list, the `sed -n` range-program shape).
Its purpose is diagnostic, not a second implementation of the guard: a
remainder dominated by one bucket is the actionable signal C6 consumes, not a
claim that this module could itself serve the command.

Negative-spec:
  - Never substitutes a synthetic/sample corpus for `main()`'s real-corpus
    run, and never reports a percentage computed over a corpus it could not
    read -- same discipline as `_guard_coverage.py`, reusing its exact
    resolution path (`iter_corpus_commands`, `_default_corpus_path`,
    `$COORDINATOR_GUARD_COVERAGE_CORPUS`) rather than inventing a second one.
  - Does not decide whether a Windows deny leg (C6) should exist. This module
    reports counts and cause buckets; the disposition is C6's and the PM's.
  - The `nonexistent_path` / `not_a_directory` / `not_a_regular_file` causes
    are evaluated against the FILESYSTEM OF THE BOX THIS MODULE RUNS ON, not
    the filesystem of the session that originally typed the command -- a
    corpus built from historical transcripts will show many operands that
    "don't exist" simply because they belonged to a different session's `cwd`
    at a different point in time. `main()`'s report and the audit doc both
    name this explicitly; it is not evidence that agents frequently read
    missing files, only that this module cannot re-create their original
    working directories.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _segments,
    token_matches_binary,
    tokenize_full_command,
)
from coordinator_core.bash_guards._guard_coverage import (
    _default_corpus_path,
    iter_corpus_commands,
)
from coordinator_core.search import answer as _answer

# The C1/C2 vocabulary (docs/plans/2026-08-22-...md rows C1/C2), whatever the
# flags. Order does not matter -- membership is basename-based via
# `token_matches_binary`, which already resolves argv[0] the way a real
# dispatch would (path prefix, .exe suffix, etc).
READ_BASENAMES: Tuple[str, ...] = ("cat", "head", "tail", "sed", "ls")

_REDIRECT_OR_SUBSTITUTION_TOKENS = {
    ">",
    ">>",
    "<",
    "<<",
    "<<<",
    "2>",
    "&>",
    "|&",
}

_GLOB_OR_BRACE_RE = re.compile(r"[*?\[\]{}]")

# `sed -n '<a>,<b>p'` / `sed -n '<a>p'`, 1-indexed inclusive, `$` accepted as
# the end bound (C1 body). Quotes are already stripped by the tokenizer.
_SED_RANGE_RE = re.compile(r"^\d+(,(\d+|\$))?p$")

_LS_ALLOWED_FLAG_CHARS = frozenset("a1")


def _has_redirect_or_substitution(tokens: Sequence[str]) -> bool:
    """C1/C2's shared operand fail-closed check: decline, by name, any
    first-segment token that is a redirection or substitution operator or
    contains one -- checked before any per-shape parsing, exactly as both
    chunk bodies require."""
    for tok in tokens:
        if tok in _REDIRECT_OR_SUBSTITUTION_TOKENS:
            return True
        if "$(" in tok or "`" in tok or "<(" in tok or "$" in tok:
            return True
    return False


def _has_glob_or_brace(tokens: Sequence[str]) -> bool:
    return any(_GLOB_OR_BRACE_RE.search(tok) for tok in tokens)


def is_read_shaped(cmd: str) -> Optional[str]:
    """Target-class predicate: is `cmd`'s first (non-piped-into) segment
    headed by a token invoking one of `READ_BASENAMES`? Returns the family
    name, or None. Fails CLOSED (returns None) on an unparseable command or
    one whose first segment is itself the receiving end of a pipe (its input
    does not exist independent of the upstream command)."""
    toks = tokenize_full_command(cmd)
    if toks is None:
        return None
    segs = _segments(toks)
    if not segs:
        return None
    first_tokens, piped_into = segs[0]
    if piped_into or not first_tokens:
        return None
    for name in READ_BASENAMES:
        if token_matches_binary(first_tokens[0], name):
            return name
    return None


def _parse_cat_args(args: Sequence[str]) -> Tuple[bool, List[str]]:
    unmodelled = False
    operands: List[str] = []
    for tok in args:
        if tok.startswith("-") and tok != "-":
            unmodelled = True
            continue
        operands.append(tok)
    return unmodelled, operands


def _parse_head_tail_args(args: Sequence[str]) -> Tuple[bool, List[str]]:
    unmodelled = False
    operands: List[str] = []
    i = 0
    n = len(args)
    while i < n:
        tok = args[i]
        if tok == "-n":
            i += 1
            if i >= n:
                unmodelled = True
            i += 1
            continue
        if re.match(r"^-\d+$", tok):
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            unmodelled = True
            i += 1
            continue
        operands.append(tok)
        i += 1
    return unmodelled, operands


def _parse_ls_args(args: Sequence[str]) -> Tuple[bool, List[str]]:
    unmodelled = False
    operands: List[str] = []
    for tok in args:
        if tok.startswith("-") and tok != "-":
            chars = tok[1:]
            if chars and all(c in _LS_ALLOWED_FLAG_CHARS for c in chars):
                continue
            unmodelled = True
            continue
        operands.append(tok)
    return unmodelled, operands


def _parse_sed_args(args: Sequence[str]) -> Tuple[bool, bool, List[str]]:
    """Returns (has_dash_n, unmodelled_other_flag, non_flag_tokens)."""
    has_n = False
    unmodelled = False
    nonflag: List[str] = []
    for tok in args:
        if tok == "-n":
            has_n = True
            continue
        if tok.startswith("-") and tok != "-":
            unmodelled = True
            continue
        nonflag.append(tok)
    return has_n, unmodelled, nonflag


def decline_cause(cmd: str, family: str) -> str:
    """Bucket WHY a read-shaped command that `plan_for` declined is not
    served, per the C1/C2 vocabulary. Only meaningful for a command that
    already tested True under `is_read_shaped`; called only on the
    remainder (see `measure_split`).

    Bucket names, in the order they are checked:
      unparseable, shell_construct, redirect_or_substitution, glob_or_brace,
      unmodelled_flag, missing_operand, multiple_operands, sed_program_shape,
      nonexistent_path, not_a_directory, not_a_regular_file,
      not_yet_implemented (structurally servable per the C1/C2 model; today's
      decline is only because that source has not shipped yet).
    """
    toks = tokenize_full_command(cmd)
    if toks is None:
        return "unparseable"
    segs = _segments(toks)
    if not segs:
        return "unparseable"
    first_tokens, piped_into = segs[0]
    if piped_into or not first_tokens:
        return "shell_construct"
    if not all(piped for _tokens, piped in segs[1:]):
        return "shell_construct"

    args = first_tokens[1:]
    if _has_redirect_or_substitution(args):
        return "redirect_or_substitution"
    if _has_glob_or_brace(args):
        return "glob_or_brace"

    if family == "cat":
        unmodelled, operands = _parse_cat_args(args)
    elif family in ("head", "tail"):
        unmodelled, operands = _parse_head_tail_args(args)
    elif family == "ls":
        unmodelled, operands = _parse_ls_args(args)
    elif family == "sed":
        has_n, unmodelled, operands = _parse_sed_args(args)
        if not has_n:
            return "unmodelled_flag"
    else:  # pragma: no cover -- READ_BASENAMES is exhaustive
        return "not_yet_implemented"

    if unmodelled:
        return "unmodelled_flag"

    if family == "sed":
        if len(operands) < 2:
            return "missing_operand"
        if len(operands) > 2:
            return "multiple_operands"
        program, path = operands
        if not _SED_RANGE_RE.match(program):
            return "sed_program_shape"
        operand_paths = [path]
        require_dir = False
    elif family == "ls":
        if len(operands) > 1:
            return "multiple_operands"
        if not operands or operands == ["-"]:
            return "not_yet_implemented"  # bare `ls` / `ls -a` of cwd
        operand_paths = operands
        require_dir = True
    else:  # cat, head, tail
        if not operands or "-" in operands:
            return "missing_operand"
        if family in ("head", "tail") and len(operands) > 1:
            return "multiple_operands"
        operand_paths = operands
        require_dir = False

    cwd = os.getcwd()
    for op in operand_paths:
        path = op if os.path.isabs(op) else os.path.join(cwd, op)
        if not os.path.exists(path):
            return "nonexistent_path"
        if require_dir:
            if not os.path.isdir(path):
                return "not_a_directory"
        else:
            if not os.path.isfile(path):
                return "not_a_regular_file"

    return "not_yet_implemented"


@dataclass
class SplitReport:
    """The whole measurement: corpus size, read-shaped share, answered
    share, and the remainder's cause buckets."""

    corpus_size: int
    read_shaped_count: int
    answered_count: int
    family_counts: Dict[str, int] = field(default_factory=dict)
    cause_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def remainder_count(self) -> int:
        return self.read_shaped_count - self.answered_count

    @property
    def read_shaped_pct(self) -> float:
        return _pct(self.read_shaped_count, self.corpus_size)

    @property
    def answered_pct_of_read_shaped(self) -> float:
        return _pct(self.answered_count, self.read_shaped_count)

    @property
    def answered_pct_of_corpus(self) -> float:
        return _pct(self.answered_count, self.corpus_size)

    @property
    def remainder_pct_of_read_shaped(self) -> float:
        return _pct(self.remainder_count, self.read_shaped_count)


def _pct(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def measure_split(commands: Sequence[str]) -> SplitReport:
    """Run the whole measurement over `commands`: which are read-shaped,
    of those how many the REAL `search.answer.plan_for` answers right now,
    and -- for the remainder -- the decline-cause bucket distribution."""
    family_counts: Dict[str, int] = {}
    cause_counts: Dict[str, int] = {}
    read_shaped = 0
    answered = 0
    for cmd in commands:
        family = is_read_shaped(cmd)
        if family is None:
            continue
        read_shaped += 1
        family_counts[family] = family_counts.get(family, 0) + 1
        if _answer.plan_for(cmd) is not None:
            answered += 1
            continue
        cause = decline_cause(cmd, family)
        cause_counts[cause] = cause_counts.get(cause, 0) + 1
    return SplitReport(
        corpus_size=len(commands),
        read_shaped_count=read_shaped,
        answered_count=answered,
        family_counts=family_counts,
        cause_counts=cause_counts,
    )


def format_report(report: SplitReport) -> str:
    lines = [
        "corpus_size | read_shaped | read_shaped_% | answered | answered_%_of_read_shaped | answered_%_of_corpus",
        "%d | %d | %.2f%% | %d | %.2f%% | %.2f%%"
        % (
            report.corpus_size,
            report.read_shaped_count,
            report.read_shaped_pct,
            report.answered_count,
            report.answered_pct_of_read_shaped,
            report.answered_pct_of_corpus,
        ),
        "",
        "family | count",
    ]
    for family, count in sorted(report.family_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("%s | %d" % (family, count))
    lines.append("")
    lines.append("decline_cause | count | %%_of_remainder (remainder=%d)" % report.remainder_count)
    for cause, count in sorted(report.cause_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("%s | %d | %.2f%%" % (cause, count, _pct(count, report.remainder_count)))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description=(
            "Report the read-shape corpus split: what share of a real Bash "
            "command corpus is read-shaped (cat/head/tail/sed -n/ls), what "
            "share of that `search.answer.plan_for` answers right now, and "
            "the unservable remainder bucketed by decline cause."
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
            "A corpus is produced by streaming every Bash/PowerShell "
            'tool_use command out of ~/.claude/projects/*/*.jsonl into '
            '{"c": "<command>"} lines. This tool never invents a corpus: a '
            "split measured against a corpus nobody can point to is worse "
            "than no number.",
            file=sys.stderr,
        )
        return 2

    commands = list(iter_corpus_commands(corpus_path))
    if not commands:
        print("Corpus at %r contained zero commands." % corpus_path, file=sys.stderr)
        return 2

    report = measure_split(commands)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
