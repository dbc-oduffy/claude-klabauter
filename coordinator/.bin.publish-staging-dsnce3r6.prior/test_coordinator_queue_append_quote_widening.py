"""
test_coordinator_queue_append_quote_widening.py — quoter start-char widening +
byte-parity regression for `_yaml_quote_string` (C1).

Spec backlink: pln-queue-append-quoter-gap-and-th-20af07 § C1

Covers AC1 (both copies quote every newly-covered start char, plus a lone/
trailing `:`, and round-trip through `yaml.safe_load`) and AC2 (the two
`needs_quoting` expressions are clause-for-clause identical — asserted via a
shared input corpus run through BOTH functions).

Both live implementations are loaded and called DIRECTLY (no subprocess):
  - coordinator_core.ops.queue_append._yaml_quote_string (native op, proper
    package module)
  - coordinator/bin/coordinator-queue-append._yaml_quote_string (legacy CLI,
    no `.py` extension — loaded via `importlib.machinery.SourceFileLoader`,
    matching the sibling parity test's pattern
    (test_queue_append_central_root_parity.py). `if __name__ == "__main__":`
    guards `main()`, so import alone triggers no I/O or subprocess calls.

Run with: python3 -m pytest coordinator/bin/test_coordinator_queue_append_quote_widening.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

import yaml


def _repo_bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(_repo_bin_dir()))


def _legacy_cli_path() -> str:
    return os.path.join(_repo_bin_dir(), "coordinator-queue-append.py")


def _load_legacy_cli_module():
    """Load coordinator-queue-append as a module via SourceFileLoader.

    Mirrors test_queue_append_central_root_parity.py's loader exactly — the
    file has no `.py` extension, so `importlib.util.spec_from_file_location`
    cannot infer a loader from the path alone.
    """
    path = _legacy_cli_path()
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_queue_append_cli_quote_widening_probe", path
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_native_op_module():
    repo_root = _repo_root()
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import coordinator_core.ops.queue_append as native_module

    return native_module


_LEGACY = _load_legacy_cli_module()
_NATIVE = _load_native_op_module()

_BOTH_QUOTERS = (
    ("legacy CLI", _LEGACY._yaml_quote_string),
    ("native op", _NATIVE._yaml_quote_string),
)


def _assert_round_trips(name: str, quote_fn, value: str) -> None:
    """Assert quote_fn(value) round-trips through yaml.safe_load back to value."""
    emitted = quote_fn(value)
    doc = f"key: {emitted}\n"
    parsed = yaml.safe_load(doc)
    if parsed["key"] != value:
        raise AssertionError(
            f"{name}: _yaml_quote_string({value!r}) = {emitted!r}, which parses "
            f"back as {parsed['key']!r} (expected {value!r}). Full doc: {doc!r}"
        )


# ---------------------------------------------------------------------------
# AC1: per-newly-covered-character round-trip, both copies
# ---------------------------------------------------------------------------

# (label, value) — one case per newly-covered start char, plus the lone/
# trailing-`:` clause. Each value is deliberately UNquoted-looking except for
# the one offending character, so a failure to quote it would previously have
# produced an unparseable document (the exact defect class this plan closes).
_NEWLY_COVERED_CASES: list[tuple[str, str]] = [
    ("leading apostrophe", "'tis a title with a leading apostrophe"),
    ("leading backtick", "`backtick-prefixed value"),
    ("leading double-quote", '"already-looks-quoted but is not'),
    ("leading percent", "%CI_VAR% expansion in a title"),
    ("leading at-sign", "@mention-style title"),
    # Review: staff-eng — `key: ?foo` parses unquoted to the identical
    # string, so this case can't independently observe a round-trip failure
    # (it passes whether or not `?` is in the quoter's start-char set). `?`
    # is kept in the quoter's start-char set defensively (it IS YAML
    # shorthand syntax in other positions), but this case is documentation
    # of that defensive inclusion, not a bite-if-broken regression test.
    ("leading question-mark (defensive-only, not independently observable)",
     "?ambiguous YAML-shorthand start"),
    ("leading comma", ",comma-led value"),
    ("lone colon", ":"),
    ("trailing colon", "a value ending in a colon:"),
    # Review: staff-eng — reserved-scalar and all-digit values are the exact
    # drift this plan closes; previously pinned only by copy-vs-copy parity
    # (would stay green if the clause were deleted from both copies). These
    # bite the real quoter behavior directly: unquoted `true` parses to the
    # bool True, not the string "true".
    ("reserved scalar 'true'", "true"),
    ("reserved scalar 'FALSE' (case-insensitive)", "FALSE"),
    ("reserved scalar '~' (null)", "~"),
    ("all-digit value", "123"),
]


def test_newly_covered_start_chars_round_trip_legacy_cli() -> None:
    for label, value in _NEWLY_COVERED_CASES:
        _assert_round_trips(f"legacy CLI ({label})", _LEGACY._yaml_quote_string, value)


def test_newly_covered_start_chars_round_trip_native_op() -> None:
    for label, value in _NEWLY_COVERED_CASES:
        _assert_round_trips(f"native op ({label})", _NATIVE._yaml_quote_string, value)


def test_hash_start_char_still_not_in_quote_gate() -> None:
    """Negative-spec (plan Anti-scope): `#` stays OUT of the start-char set —
    the existing `(^|\\s)#` scan is the sole `#` gate and its `^` branch
    already covers a leading `#`. A leading `#` must still round-trip.
    """
    value = "#123 is a leading-hash value"
    for name, quote_fn in _BOTH_QUOTERS:
        _assert_round_trips(f"{name} (leading #)", quote_fn, value)


# ---------------------------------------------------------------------------
# AC2: clause-for-clause parity across a shared input corpus
# ---------------------------------------------------------------------------

_PARITY_CORPUS: list[str] = [
    "",
    "plain unquoted value",
    "  leading and trailing whitespace  ",
    "colon: space",
    "|pipe start",
    ">gt start",
    "!bang start",
    "&amp start",
    "*star start",
    "{brace start",
    "}close-brace start",
    "[bracket start",
    "]close-bracket start",
    "'apostrophe start",
    "`backtick start",
    '"double-quote start',
    "%percent start",
    "@at start",
    "?question start",
    ",comma start",
    ":",
    "trailing colon:",
    "no trailing colon here",
    "- dash-space start",
    "multi\nline\nvalue",
    "true",
    "FALSE",
    "Null",
    "yes",
    "No",
    "~",
    "123",
    "-45",
    "3.14",
    "-3.14",
    "not-a-number-123abc",
    "PR #123 mid-string hash",
    "#123 leading hash",
    'has a backslash \\ and a quote "',
]


def test_needs_quoting_parity_across_corpus() -> None:
    """AC2: both copies emit IDENTICAL output for every value in the shared
    corpus — the parity test that stops the next drift.
    """
    mismatches = []
    for value in _PARITY_CORPUS:
        legacy_out = _LEGACY._yaml_quote_string(value)
        native_out = _NATIVE._yaml_quote_string(value)
        if legacy_out != native_out:
            mismatches.append((value, legacy_out, native_out))
    if mismatches:
        lines = "\n".join(
            f"  value={v!r}: legacy={l!r} native={n!r}" for v, l, n in mismatches
        )
        raise AssertionError(
            f"_yaml_quote_string parity broken for {len(mismatches)} corpus value(s):\n{lines}"
        )
