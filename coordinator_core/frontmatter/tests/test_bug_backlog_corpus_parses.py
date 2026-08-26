"""
Corpus lint: every state/bug-backlog/*.yaml must parse as YAML.

Rationale: state/bug-backlog/ is read via
coordinator_core.ops.records_query._load_record, which silently skips a file
whose YAML fails to parse (mirrors query-records.js's default
includeUnparseable=false behavior — see
coordinator_core/ops/ceremony/records_query.py's query_records docstring).
That means a bug record broken by a stray out-of-band append (a CORRECTION
paragraph pasted in after the frontmatter block, at an indentation YAML reads
as a continuation of the wrong key) becomes invisible to the grind, the
census, and every other consumer of this corpus — while still reading fine
to a human skimming the file. Nothing upstream of this test asserted the
corpus parses at all; this closes that gap directly, at the read boundary
consumers actually hit, without touching the (deliberately) fail-open
records_query seam itself.

Schema-validation is run and reported for every file that DOES parse, but
only fails the corpus-wide gate for a STRUCTURAL failure (not a dict at top
level). A full-strictness schema gate is deliberately NOT added here: a
scoped pass on this corpus (2026-08-26) found 22 pre-existing, unrelated
schema-validation failures (status values like "resolved"/"withdrawn"/
"retracted"/"in_progress"/"unknown" outside the closed
open/closed/deferred/wontfix enum, plus a few missing-required-field
records) that predate and are independent of the parse-failure repair this
test was written to guard — see
state/bug-backlog/2026-08-26-bug-backlog-corpus-has-22-schema-invalid-records.yaml.
Folding that fix into this guard would silently paper over a distinct,
larger cleanup this task was not scoped to perform.

Negative-spec:
  - Does NOT change records_query's fail-open skip-unparseable behavior —
    that behavior is documented and intentional for a live read path serving
    many callers; this test polices the *authored corpus*, not the reader.
  - Does NOT validate any other queue family (debt-backlog, improvement-queue)
    — bug-backlog is this test's sole remit; a sibling corpus needs its own
    guard if this pattern is worth repeating there.
  - Does NOT hard-fail on schema-enum/required-field drift — see rationale
    above; that is a separate, larger cleanup tracked in its own record.
"""
from __future__ import annotations

import functools
import json
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKLOG_DIR = _REPO_ROOT / "state" / "bug-backlog"
_SCHEMA_PATH = (
    _REPO_ROOT / "coordinator_core" / "frontmatter" / "schemas" / "bug-backlog.schema.json"
)


def _iter_bug_backlog_files() -> list[Path]:
    if not _BACKLOG_DIR.is_dir():
        return []
    return sorted(_BACKLOG_DIR.glob("*.yaml"))


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple[tuple, ...]:
    """Read + parse the whole corpus ONCE per process.

    Cached rather than re-walked per test: PyYAML's per-file parse over ~620
    records is the entire cost of this module, so three tests each doing their
    own walk tripled it (measured 2.43s for the module, ~800ms per walk). One
    walk, three readers.

    Returns one `(repo_rel, record, parse_error, elapsed_ms)`-shaped row set;
    `record` is None exactly when `parse_error` is set.
    """
    rows = []
    start = time.perf_counter()
    for path in _iter_bug_backlog_files():
        repo_rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            record = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            rows.append((repo_rel, None, str(exc)))
            continue
        rows.append((repo_rel, record, None))
    elapsed_ms = (time.perf_counter() - start) * 1000
    return tuple(rows), elapsed_ms


def test_bug_backlog_corpus_parses():
    """Every state/bug-backlog/*.yaml must yaml.safe_load to a dict.

    This is the hard gate: a parse failure makes a record invisible to the
    records_query read seam (fail-open, by design — see module docstring),
    which is the concrete defect this test exists to catch."""
    rows, _ = _corpus()
    assert rows, f"no *.yaml records found under {_BACKLOG_DIR} — corpus glob is likely wrong"

    parse_failures: list[str] = []

    for repo_rel, record, parse_error in rows:
        if parse_error is not None:
            parse_failures.append(f"{repo_rel}: {parse_error}")
        elif not isinstance(record, dict):
            parse_failures.append(f"{repo_rel}: top-level YAML is not a mapping")

    assert not parse_failures, (
        f"{len(parse_failures)} state/bug-backlog/*.yaml record(s) fail to parse as YAML "
        "-- invisible to every consumer of this corpus (records_query silently skips an "
        "unparseable file). Repair structure only, preserving every byte of authored "
        "meaning:\n  " + "\n  ".join(parse_failures)
    )


def test_bug_backlog_corpus_schema_validation_is_reported():
    """Schema-validate every record that parses; report count only, do not fail
    the suite on pre-existing drift (see module docstring) -- printed to stdout
    so `-s` surfaces it, never silently discarded."""
    rows, _ = _corpus()
    schema_failures: list[str] = []
    for repo_rel, record, _parse_error in rows:
        if not isinstance(record, dict):
            continue
        errors = validate_frontmatter(record, _SCHEMA_PATH)
        if errors:
            schema_failures.append(f"{repo_rel}: {json.dumps(errors)}")
    if schema_failures:
        print(
            f"\n{len(schema_failures)} state/bug-backlog/*.yaml record(s) parse but fail "
            f"schema validation (pre-existing, not gated by this test):\n  "
            + "\n  ".join(schema_failures)
        )


def test_bug_backlog_corpus_parse_cost_is_measured():
    """Report the ONE corpus walk's cost — no second walk to measure the first.

    Pure in-process read + YAML parse, no git spawn. Reported rather than gated at
    the 500ms brightline: the cost is PyYAML's per-file overhead across the corpus's
    current size, so a tight assert would make this guard the thing needing a perf
    plan before it could ship. The loose ceiling below is a sanity bound on something
    pathological, never a budget.
    """
    rows, elapsed_ms = _corpus()
    print(f"\nbug-backlog corpus parse: {elapsed_ms:.1f}ms over {len(rows)} files")
    assert elapsed_ms < 5000, (
        f"corpus parse cost {elapsed_ms:.1f}ms over {len(rows)} files -- "
        "sanity ceiling exceeded; something pathological happened (not the expected "
        "PyYAML per-file overhead)."
    )
