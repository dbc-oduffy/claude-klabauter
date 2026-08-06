"""coordinator_core.learn_lessons_assemble -- candidate-restatement generator
for a doctrine-wiki edit.

Purpose: doctrine wikis restate the same rule in more than one place; when
one statement changes and its siblings do not, the file gives two careful
readers opposite answers (23 such contradictions were found across 229 wiki
files in a 2026-07 survey). The fix computed here is CHEAP AND MECHANICAL
ONLY: at the moment a wiki edit is about to land, surface the set of places
in the TARGET file that already say something adjacent to the text being
added, so the acting agent dispositions a slot already in front of it
rather than being asked to remember to grep.

THE SINGLE MOST IMPORTANT PROPERTY -- this is a candidate GENERATOR, never
an ADJUDICATOR. Empirically only 2 of the 23 known findings were decidable
by text alone; the other 21 needed a reading agent's semantic judgment.
This module never decides whether two passages contradict, restate, or
merely share vocabulary -- it only ever answers "where should a reading
agent look", tuned for recall over precision. Enforced structurally, not
just by convention: every function in this module returns candidate
`{line, excerpt}` records and NOTHING that resembles a verdict field
(no `contradicts`, no `is_duplicate`, no `severity`) -- see
`test_generator_never_emits_a_verdict_shaped_field` in this package's test
file for the pin.

Two signals, both cheap and mechanical (no LLM call, no embedding):

  1. Lexical phrase-overlap -- word n-grams (`_PHRASE_NGRAM_SIZE`) shared
     between the incoming text and a sliding window over the target file's
     lines.
  2. Duplicate/near-duplicate section-heading detection -- markdown
     headings (`#`..`######`) within the SAME target file whose normalized
     token sets overlap above `_HEADING_JACCARD_THRESHOLD`.

Matches the sibling computed-skill assemblers' shape
(`coordinator_core.pickup_assemble`, `coordinator_core.baton_assemble`):
read-only compute, returns a plain 8-key decision object via
`coordinator_core.contract.decision_object.envelope.build_envelope`. This
module's use of the envelope's `gates` key for read-only inventory/evidence
follows `consolidate_assemble`'s precedent for that key -- but unlike EVERY
existing member of this assembler family, including `consolidate_assemble`
(whose `apply.py` dispatches a real, non-empty `directives[]` table for
branch delete/cherry-pick/merge/worktree/fetch-prune), this module emits NO
`directives[]` entry ever and ships no `apply.py` half at all: there is no
mutating action a candidate-restatement generator could ever authorize, so
the candidates themselves are the entire product. This is the first
assembler in the family whose `directives[]` is unconditionally empty --
not a case that matches an existing empty-directives sibling.

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md

Negative-spec:
    - Do NOT add a mutating code path or an `apply.py` module here -- this
      generator never authorizes a mutation; there is nothing for a
      directive to name.
    - Do NOT add any field, anywhere in the returned envelope, that reads
      as an adjudication ("contradicts", "is_duplicate", "verdict",
      "severity", "should_fix"). If you find yourself computing whether
      two passages actually conflict, stop -- that is out of scope.
    - Do NOT make either signal semantic (no embeddings, no LLM call, no
      synonym/paraphrase detection). Both signals are pure string/token
      arithmetic, deliberately tuned toward recall with a tolerable
      false-positive rate -- a reading agent, not this module, prunes the
      false positives.
"""
from __future__ import annotations

import re
import string
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

from coordinator_core.contract.decision_object.envelope import build_envelope

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

# Recall-oriented, hand-tuned constants -- not derived from a corpus fit.
# Widening these (smaller n-gram size, lower Jaccard threshold) trades
# precision for recall; per the module's own charter, recall is the side
# to err on.
#
# _PHRASE_NGRAM_SIZE was 4 through the module's first dogfood run
# (2026-07-27, against coordinator/docs/wiki/cross-repo-communication.md)
# and measured unusable: 13/13 candidates were false positives, every one
# a `shared_ngrams: 1` hit on a single incidental 4-word run of generic
# corpus vocabulary ("a cross-repo memo") that recurs dozens of times
# across the target file -- a 4-gram is short enough that shared
# subject-matter boilerplate collides with it by chance, so `shared_ngrams
# >= _MIN_SHARED_NGRAMS` stopped discriminating "adjacent passage" from
# "same corpus". A 5-word run colliding by chance is far less likely (each
# added token divides the collision probability by roughly the
# vocabulary's branching factor); re-run against the same file at
# `_PHRASE_NGRAM_SIZE=5` returns zero candidates, and a second target file
# (`scoped-safety-commits.md`, self-referential doctrine with the same
# repeated-vocabulary shape) keeps its genuine same-topic hits at 5+
# shared tokens while losing exactly the single-4-gram noise. Recall is
# preserved deliberately: `_MIN_SHARED_NGRAMS` stays at 1 rather than also
# rising, so one real 5-word verbatim run is still enough to surface a
# candidate -- the fix targets the collision-prone window width, not the
# count of matches required.
_PHRASE_NGRAM_SIZE = 5
_MIN_SHARED_NGRAMS = 1
_WINDOW_LINES = 3  # a candidate window is this many consecutive target lines
_HEADING_JACCARD_THRESHOLD = 0.6
_EXCERPT_MAX_CHARS = 200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})


class BriefResult(NamedTuple):
    decision_object: dict[str, Any]
    exit_code: int


# ---------------------------------------------------------------------------
# Tokenization / n-gram helpers -- pure string arithmetic, no semantics.
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    normalized = text.lower().translate(_PUNCT_TABLE)
    return normalized.split()


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _excerpt(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= _EXCERPT_MAX_CHARS:
        return stripped
    return stripped[: _EXCERPT_MAX_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Signal 1 -- lexical phrase-overlap between the incoming text and a sliding
# window over the target file's lines.
# ---------------------------------------------------------------------------


def _phrase_overlap_candidates(
    lines: list[str], incoming_ngrams: set[tuple[str, ...]]
) -> list[dict[str, Any]]:
    if not incoming_ngrams:
        return []

    hits: dict[int, int] = {}  # 1-indexed window-start line -> shared count
    for start in range(len(lines)):
        window = lines[start : start + _WINDOW_LINES]
        window_tokens = _tokenize(" ".join(window))
        window_ngrams = _ngrams(window_tokens, _PHRASE_NGRAM_SIZE)
        shared = incoming_ngrams & window_ngrams
        if len(shared) >= _MIN_SHARED_NGRAMS:
            hits[start + 1] = len(shared)

    # Merge contiguous/overlapping window-starts into one candidate per
    # run, anchored at the run's first line -- a `_WINDOW_LINES`-wide
    # sliding window fires on every overlapping start over a matching
    # passage, and reporting each one separately would flood the caller
    # with near-duplicate candidates for the SAME passage.
    candidates: list[dict[str, Any]] = []
    sorted_starts = sorted(hits)
    run_start: Optional[int] = None
    run_end: Optional[int] = None
    run_best_shared = 0

    def _flush() -> None:
        if run_start is None:
            return
        excerpt_lines = lines[run_start - 1 : run_end]
        candidates.append(
            {
                "line": run_start,
                "excerpt": _excerpt(" ".join(excerpt_lines)),
                "signal": "phrase-overlap",
                "shared_ngrams": run_best_shared,
            }
        )

    for line_no in sorted_starts:
        if run_start is None:
            run_start = run_end = line_no
            run_best_shared = hits[line_no]
            continue
        if line_no <= run_end + _WINDOW_LINES:
            run_end = max(run_end, line_no + _WINDOW_LINES - 1)
            run_best_shared = max(run_best_shared, hits[line_no])
        else:
            _flush()
            run_start = run_end = line_no
            run_best_shared = hits[line_no]
    _flush()

    return candidates


# ---------------------------------------------------------------------------
# Signal 2 -- duplicate/near-duplicate section headings WITHIN the target
# file (no comparison against the incoming text -- this signal is entirely
# about the target file restating itself).
# ---------------------------------------------------------------------------


def _extract_headings(lines: list[str]) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((idx, match.group(2)))
    return headings


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Review: code-reviewer — Finding 10. Jaccard on raw heading tokens with no
# genericity weighting means two single- or two-word generic headings ("## Overview"
# / "## Overview", "## Notes" / "## Notes") repeated legitimately in unrelated
# sections score 1.0 and fire as a duplicate -- indistinguishable from a genuine
# near-duplicate section. This is a small denylist of common structural-heading
# words, not a semantic filter: a heading whose tokens are ENTIRELY generic (after
# removing these) carries no topical content to compare, so it is skipped rather
# than flagged.
_GENERIC_HEADING_WORDS = {
    "overview",
    "notes",
    "note",
    "summary",
    "introduction",
    "background",
    "details",
    "detail",
    "context",
    "purpose",
    "scope",
    "usage",
    "example",
    "examples",
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "for",
}


def _heading_duplicate_candidates(lines: list[str]) -> list[dict[str, Any]]:
    headings = _extract_headings(lines)
    normalized = [(line_no, text, set(_tokenize(text))) for line_no, text in headings]

    flagged: dict[int, dict[str, Any]] = {}
    for i in range(len(normalized)):
        line_i, text_i, tokens_i = normalized[i]
        for j in range(i + 1, len(normalized)):
            line_j, text_j, tokens_j = normalized[j]
            if not (tokens_i - _GENERIC_HEADING_WORDS) or not (tokens_j - _GENERIC_HEADING_WORDS):
                # Either heading is entirely generic structural vocabulary -- no
                # topical content to compare, so don't flag it as a duplicate.
                continue
            score = _jaccard(tokens_i, tokens_j)
            if score >= _HEADING_JACCARD_THRESHOLD:
                for line_no, text, other_line, other_text in (
                    (line_i, text_i, line_j, text_j),
                    (line_j, text_j, line_i, text_i),
                ):
                    existing = flagged.get(line_no)
                    if existing is None or score > existing["heading_jaccard"]:
                        flagged[line_no] = {
                            "line": line_no,
                            "excerpt": _excerpt(text),
                            "signal": "heading-duplicate",
                            "matched_line": other_line,
                            "matched_excerpt": _excerpt(other_text),
                            "heading_jaccard": round(score, 3),
                        }
    return sorted(flagged.values(), key=lambda c: c["line"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_candidates(
    target_path: str, incoming_text: str, *, repo_root: Optional[Path] = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`(target_path, incoming_text) -> (candidates, meta)`.

    `candidates` is a list of `{line, excerpt, signal, ...}` records, sorted
    by line number -- ALWAYS a generator output, never a verdict (see the
    module docstring's negative-spec). `meta` carries `target_exists` and a
    per-signal count, for the caller's own narration/logging -- it is
    informational, not gating.

    A nonexistent `target_path` is NOT an error: a wiki edit dispatched
    against a file that does not yet exist on disk has nothing to restate
    against, so this returns `([], {"target_exists": False, ...})` rather
    than raising -- mirrors this module's own read-only-compute charter
    (never fail loud over an input shape a real caller routinely produces:
    a brand-new wiki file).
    """
    fs_path = Path(target_path)
    if not fs_path.is_absolute() and repo_root is not None:
        fs_path = repo_root / target_path

    if not fs_path.is_file():
        return [], {
            "target_exists": False,
            "phrase_overlap_count": 0,
            "heading_duplicate_count": 0,
        }

    text = fs_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    incoming_ngrams = _ngrams(_tokenize(incoming_text), _PHRASE_NGRAM_SIZE)
    phrase_candidates = _phrase_overlap_candidates(lines, incoming_ngrams)
    heading_candidates = _heading_duplicate_candidates(lines)

    candidates = sorted(phrase_candidates + heading_candidates, key=lambda c: c["line"])
    meta = {
        "target_exists": True,
        "phrase_overlap_count": len(phrase_candidates),
        "heading_duplicate_count": len(heading_candidates),
    }
    return candidates, meta


# Review: code-reviewer — Finding 7. Mirrors `pickup_assemble`/`baton_assemble`'s
# `_emit` -- the single validation chokepoint `brief()` routes through, rather than
# constructing `BriefResult` directly. Low risk today since `narration`/`next_move`
# are literal always-non-empty strings in every current code path, but the safety
# net disappears silently the moment a future edit makes either computed/conditional
# (as they already are in both siblings) -- this chokepoint is what would catch that.
def _emit(decision_object: dict[str, Any], exit_code: int) -> BriefResult:
    """The single validation chokepoint this module's `brief()` routes through --
    mirrors `pickup_assemble`/`baton_assemble`'s `_emit` fail-loud discipline
    (non-empty `narration`; every `judgment_points[]` entry carries a
    `recommendation` key, even though this module's `judgment_points[]` is always
    empty by construction -- the check still guards against a future regression)."""
    narration = decision_object.get("narration")
    if not narration:
        raise ValueError("_emit: decision object missing non-empty 'narration'")
    for jp in decision_object.get("judgment_points") or []:
        if "recommendation" not in jp:
            raise ValueError(
                f"_emit: judgment_points entry {jp.get('id', '<no id>')!r} missing "
                "required 'recommendation' key"
            )
    return BriefResult(decision_object, exit_code)


def brief(
    target_path: str, incoming_text: str, *, repo_root: Optional[Path] = None
) -> BriefResult:
    """`brief <target-wiki-path> <incoming-text>` -- the single-shot,
    read-only decision-object computation. Mutates nothing; emits no
    `directives[]` (there is never a mutating action for this generator to
    name -- see module docstring) and no `judgment_points[]` (there is no
    dispatch-gating decision here either; the candidates ARE the whole
    product). Candidates + signal counts are carried on `gates` -- mirrors
    `consolidate_assemble`'s use of that key for read-only inventory/
    evidence rather than an executable list.
    """
    candidates, meta = generate_candidates(target_path, incoming_text, repo_root=repo_root)

    if not meta["target_exists"]:
        narration = (
            f"{target_path!r} does not exist on disk -- nothing to check "
            "restatements against."
        )
        next_move = "Proceed; this generator has no prior passages to compare."
    elif candidates:
        narration = (
            f"Found {len(candidates)} candidate location(s) in {target_path!r} "
            "that already say something adjacent to the incoming text "
            f"({meta['phrase_overlap_count']} phrase-overlap, "
            f"{meta['heading_duplicate_count']} heading-duplicate)."
        )
        next_move = (
            "Read each candidate excerpt and decide whether it restates, "
            "contradicts, or is unrelated to the incoming text -- this "
            "generator surfaces WHERE to look, it does not adjudicate."
        )
    else:
        narration = f"No candidate restatements found in {target_path!r}."
        next_move = "Proceed; no adjacent passages were surfaced."

    return _emit(
        build_envelope(
            artifact={"path": target_path, "kind": "wiki-edit-target"},
            preflight={"incoming_text_length": len(incoming_text)},
            gates={"candidates": candidates, **meta},
            directives=[],
            judgment_points=[],
            decisions={},
            narration=narration,
            next_move=next_move,
        ),
        EXIT_OK,
    )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _usage(prog: str) -> int:
    print(
        f"usage: {prog} <target-wiki-path> <incoming-text> "
        "| <target-wiki-path> --text-file <path>",
        file=sys.stderr,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    import json

    if not argv:
        return _usage("learn-lessons-reconcile-candidates")

    target_path = argv[0]
    tail = argv[1:]

    if tail[:1] == ["--text-file"]:
        if len(tail) < 2:
            return _usage("learn-lessons-reconcile-candidates")
        text_file = Path(tail[1])
        try:
            # Review: code-reviewer — Finding 9. `generate_candidates`' own file read
            # uses `errors="replace"` (lenient); this path used to read strict
            # (no `errors=`), so a non-UTF-8 --text-file raised `UnicodeDecodeError`
            # -- a `ValueError` subclass, not `OSError` -- uncaught by the except
            # below. Match the lenient decode so both text-reading paths behave the
            # same way on the same class of bad input.
            incoming_text = text_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(
                f"learn-lessons-reconcile-candidates: could not read "
                f"--text-file {tail[1]!r}: {exc}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    elif len(tail) == 1:
        incoming_text = tail[0]
    else:
        return _usage("learn-lessons-reconcile-candidates")

    # Review: code-reviewer — Finding 6. Matches `pickup_assemble`'s `main()`
    # backstop: the contract's "a decision object is emitted on every exit, never
    # a bare traceback" guarantee must not rest on having enumerated every raise
    # site in `brief()`/`generate_candidates()` correctly (e.g. an `OSError` from a
    # permission/race condition between `Path.is_file()` and `Path.read_text()`).
    try:
        result = brief(target_path, incoming_text)
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors pickup_assemble
        print(
            f"learn-lessons-reconcile-candidates: unexpected failure: {exc}",
            file=sys.stderr,
        )
        failure = _emit(
            {
                "error": str(exc),
                "transport_failure": True,
                "narration": f"brief() raised an unexpected exception: {exc}.",
                "next_move": (
                    "Re-run against the same artifact path; if this repeats, report the "
                    "traceback — this is a structural backstop firing, not an enumerated "
                    "failure mode."
                ),
            },
            EXIT_TRANSPORT_FAIL,
        )
        print(json.dumps(failure.decision_object, indent=2))
        return failure.exit_code

    print(json.dumps(result.decision_object, indent=2))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
