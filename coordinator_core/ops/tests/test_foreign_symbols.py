"""
coordinator_core.ops.tests.test_foreign_symbols

The assert-on-empty gate for the non-Python symbol-extraction adapter
(chunk C2 of docs/plans/2026-08-08-cartography-consumes-symbol-extract.md).
The workstream exists because `symbol_extract.extract()` returns 0 symbols
and exit 0 when a grammar is missing — the same silent-empty shape that hid
Example-cockpit-repo's empty atlas. This module is the discriminator: five
distinct states, asserted PER EXTENSION, never folded into one aggregate
`symbols == 0` check that a healthy language could mask a dead one behind.

States asserted (AC2/AC3/AC4):
  - dependency_absent  — symbol_extract not importable; its own loud state,
    never a corpus fact, never skipped (runs on every machine).
  - corpus_fact        — zero symbols, zero diagnostics naming the
    extension's language: a legitimate "no files of this kind", PASS.
  - missing_grammar     — zero symbols, a diagnostic naming the extension's
    language: FAIL LOUD.
  - partial_coverage    — symbols > 0 AND a language-level diagnostic
    (`file is None`) names the extension's language: a healthy-looking
    non-zero symbol count with files silently dropped underneath it. This
    is the state a naive `symbols == 0` check cannot see — the whole
    reason it is asserted as its own arm rather than folded into
    "healthy".
  - parse_failure       — a diagnostic carrying a non-None `file`,
    attributed onto that file's envelope entry as an `"error"` field; a
    healthy language total must not excuse a silently-dropped file.

Spec backlink: docs/plans/2026-08-08-cartography-consumes-symbol-extract.md
chunk C2 (AC2, AC3, AC4).

Anti-scope this file enforces (mirrors the plan's own Anti-scope section):
  - no aggregate `symbols == 0` assertion anywhere below — every assertion
    is filtered to a single extension's findings first
  - no assertion on process/exit status (none of these functions run a
    subprocess; exit status is not evidence and is never checked)
  - no exact-set assertion over `SymbolKind` or `dataclasses.fields(Symbol)`
    — their vocabulary grows additively by memo and must not turn our tests
    red
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.foreign_symbols import (
    build_foreign_symbols,
    classify_foreign_symbol_coverage,
)
from coordinator_core.ops.foreign_symbols import _envelope_for_file

# ---------------------------------------------------------------------------
# Cockpit's real per-extension file census (chunk C2 body, verbatim): three
# legitimate zeros (.cts, .jsx, .cjs) that must read as corpus facts, and a
# live .mjs (106 files) that must not be excused by a healthy .ts total.
# Derived here as a fixture constant, never hardcoded as a language list —
# classify_foreign_symbol_coverage is exercised per EXTENSION.
# ---------------------------------------------------------------------------
_COCKPIT_CENSUS = {
    ".ts": 1292,
    ".tsx": 405,
    ".js": 338,
    ".mjs": 106,
    ".mts": 5,
    ".cts": 0,
    ".jsx": 0,
    ".cjs": 0,
}


_COCKPIT_LANGUAGES = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".jsx": "javascript",
    ".cjs": "javascript",
}


def _result(files=None, diagnostics=None, languages=None):
    """Hand-build a `build_foreign_symbols` return value at its pinned
    dict shape: `{"files": [...], "diagnostics": [...], "languages": {...},
    "completeness": {...}}`.

    classify_foreign_symbol_coverage's contract is `(result: dict, census)
    -> list[dict]` — pinned independent of C1's internal mapping from
    symbol_extract's raw ExtractionResult onto this dict. Building the dict
    directly tests against the pinned contract, not a guess at C1's
    internals. `languages` defaults to cockpit's real per-extension
    resolution (chunk brief) so these fakes classify without requiring the
    real `symbol_extract` dependency.
    """
    return {
        "files": files or [],
        "diagnostics": diagnostics or [],
        "languages": _COCKPIT_LANGUAGES if languages is None else languages,
        "completeness": {"authority": "project_treesitter", "feature_level": "lite"},
    }


# ---------------------------------------------------------------------------
# Arm 1 — DEPENDENCY ABSENT. Never skipped: this must be loud on every
# machine, installed or not — it is the arm that catches the degrade path
# being wired to look like success. Exercised through build_foreign_symbols
# itself via the pinned monkeypatch seam, `_import_symbol_extract`.
# ---------------------------------------------------------------------------


def test_dependency_absent_is_its_own_loud_state(monkeypatch):
    import coordinator_core.ops.foreign_symbols as mod

    def _raise_import_error():
        raise ImportError("No module named 'symbol_extract'")

    monkeypatch.setattr(mod, "_import_symbol_extract", _raise_import_error)

    result = build_foreign_symbols(target_root="/nonexistent", files=["app.ts", "app.mjs"])

    assert "unavailable" in result
    assert isinstance(result["unavailable"], str) and result["unavailable"]
    assert "files" not in result

    findings = classify_foreign_symbol_coverage(result, census={".ts": 1, ".mjs": 1})

    dependency_absent = [f for f in findings if f["state"] == "dependency_absent"]
    assert len(dependency_absent) == 1
    assert not any(f["state"] == "corpus_fact" for f in findings)
    assert not any(f["state"] == "missing_grammar" for f in findings)
    assert not any(f["state"] == "parse_failure" for f in findings)


def test_dependency_absent_never_folded_into_corpus_fact(monkeypatch):
    import coordinator_core.ops.foreign_symbols as mod

    def _raise_import_error():
        raise ImportError("symbol_extract not installed")

    monkeypatch.setattr(mod, "_import_symbol_extract", _raise_import_error)

    result = build_foreign_symbols(target_root="/nonexistent", files=["a.tsx"])
    findings = classify_foreign_symbol_coverage(result, census={".tsx": 1})

    states = {f["state"] for f in findings}
    assert states == {"dependency_absent"}


# ---------------------------------------------------------------------------
# Arms 2-4 operate on classify_foreign_symbol_coverage's pinned dict-in,
# list-out contract directly, injecting fake diagnostics/envelope entries at
# that seam per the chunk brief's injection strategy — no second venv, no
# corrupted fixture, no dependency on C1's internal ExtractionResult
# mapping.
#
# classify_foreign_symbol_coverage resolves each extension's language from
# `result["languages"]` (populated by build_foreign_symbols at build time,
# and by `_result()`'s cockpit-derived default here) — it never needs its
# own `symbol_extract` import on this path, so these arms run unskipped on
# every machine, installed or not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extension", [".cts", ".jsx", ".cjs"])
def test_zero_census_extension_is_corpus_fact_not_missing_grammar(extension):
    # No files carry this extension in cockpit's real corpus, and no
    # diagnostic names it — a legitimate corpus fact, not a failure.
    result = _result(files=[], diagnostics=[])
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    this_ext = [f for f in findings if f["extension"] == extension]
    assert len(this_ext) == 1
    assert this_ext[0]["state"] == "corpus_fact"


def test_live_extension_with_symbols_produces_no_zero_state_finding():
    # .ts carries 1292 census files; a symbol-bearing entry with no
    # diagnostic naming it must not be classified corpus_fact or
    # missing_grammar.
    result = _result(
        files=[{"path": "src/app.ts", "functions": [{"name": "main", "lineno": 1}]}],
        diagnostics=[],
    )
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    ts_zero_state = [
        f
        for f in findings
        if f["extension"] == ".ts" and f["state"] in ("corpus_fact", "missing_grammar")
    ]
    assert ts_zero_state == []


def test_missing_grammar_uses_real_diagnostic_text_shape():
    # Their real diagnostic text, measured live (chunk brief, verbatim);
    # language-level diagnostics carry file=None.
    diagnostic = {
        "level": "error",
        "message": (
            "grammar unavailable for typescript: No module named "
            "'tree_sitter_typescript' — 1474 file(s) skipped"
        ),
        "file": None,
        "line": None,
    }
    result = _result(files=[], diagnostics=[diagnostic])
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    ts_findings = [f for f in findings if f["extension"] == ".ts"]
    assert len(ts_findings) == 1
    assert ts_findings[0]["state"] == "missing_grammar"
    assert ts_findings[0]["state"] != "corpus_fact"
    assert ts_findings[0]["detail"]


# ---------------------------------------------------------------------------
# Arm — PARTIAL COVERAGE. symbols > 0 for an extension whose language is
# nonetheless named by an error-level, language-level (file=None)
# diagnostic — the shape symbol_extract produces when a grammar loads for
# some files but not others: a healthy-looking non-zero symbol count with a
# skipped-file count silently dropped underneath it.
# ---------------------------------------------------------------------------


def test_symbols_present_with_language_diagnostic_is_partial_coverage_not_silent():
    diagnostic = {
        "level": "error",
        "message": (
            "grammar unavailable for typescript: No module named "
            "'tree_sitter_typescript' — 1474 file(s) skipped"
        ),
        "file": None,
        "line": None,
    }
    result = _result(
        files=[{"path": "src/app.ts", "functions": [{"name": "main", "lineno": 1}]}],
        diagnostics=[diagnostic],
    )
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    ts_findings = [f for f in findings if f["extension"] == ".ts"]
    assert len(ts_findings) == 1
    assert ts_findings[0]["state"] == "partial_coverage"
    assert ts_findings[0]["state"] != "corpus_fact"

    # The skipped-file count from the diagnostic message must survive into
    # the finding's own detail — not just a bare symbol count.
    assert "1474" in ts_findings[0]["detail"]
    assert "skipped" in ts_findings[0]["detail"]


def test_healthy_extension_with_no_diagnostic_yields_no_partial_coverage_finding():
    # Guard against over-firing: symbols > 0 and no diagnostic naming the
    # language at all must not classify as partial_coverage.
    result = _result(
        files=[{"path": "src/app.ts", "functions": [{"name": "main", "lineno": 1}]}],
        diagnostics=[],
    )
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    assert not any(f["extension"] == ".ts" and f["state"] == "partial_coverage" for f in findings)


def test_aggregate_symbol_count_does_not_mask_per_extension_gap():
    # The non-negotiable, made concrete: a healthy .ts symbol count must
    # not excuse .mjs (106 census files) from reporting missing_grammar. A
    # test asserting only an aggregate `symbols == 0` across both
    # extensions would pass here even though .mjs is silently broken —
    # this test would fail under such an implementation.
    javascript_grammar_missing = {
        "level": "error",
        "message": (
            "grammar unavailable for javascript: No module named "
            "'tree_sitter_javascript' — 106 file(s) skipped"
        ),
        "file": None,
        "line": None,
    }
    result = _result(
        files=[{"path": "src/app.ts", "functions": [{"name": "main", "lineno": 1}]}],
        diagnostics=[javascript_grammar_missing],
    )
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    mjs_findings = [f for f in findings if f["extension"] == ".mjs"]
    assert len(mjs_findings) == 1
    assert mjs_findings[0]["state"] == "missing_grammar"

    ts_findings = [f for f in findings if f["extension"] == ".ts"]
    assert all(f["state"] not in ("missing_grammar", "corpus_fact") for f in ts_findings)


def test_healthy_language_total_does_not_excuse_a_silently_dropped_file():
    # A per-file parse failure must attribute onto that file's own envelope
    # entry as an "error" field, even while the extension's aggregate
    # symbol count elsewhere is healthy — the exact assertion an
    # aggregate-zero check cannot make.
    diagnostic = {
        "level": "error",
        "message": "parse error in src/broken.ts: unexpected token",
        "file": "src/broken.ts",
        "line": 42,
    }
    result = _result(
        files=[
            {"path": "src/app.ts", "functions": [{"name": "main", "lineno": 1}]},
            {
                "path": "src/broken.ts",
                "functions": [],
                "error": "parse error: unexpected token",
            },
        ],
        diagnostics=[diagnostic],
    )
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    parse_failures = [f for f in findings if f["state"] == "parse_failure"]
    assert len(parse_failures) == 1
    assert parse_failures[0]["extension"] == ".ts"
    assert parse_failures[0]["detail"]

    broken_entry = next(f for f in result["files"] if f["path"] == "src/broken.ts")
    assert broken_entry.get("error")

    healthy_entry = next(f for f in result["files"] if f["path"] == "src/app.ts")
    assert "error" not in healthy_entry

    # The healthy .ts entry existing alongside the failure must not turn
    # the parse failure into a corpus_fact or suppress it entirely.
    ts_zero_state = [
        f
        for f in findings
        if f["extension"] == ".ts" and f["state"] in ("corpus_fact", "missing_grammar")
    ]
    assert ts_zero_state == []


# ---------------------------------------------------------------------------
# Chunk C5 — the envelope silently dropped `constant`/`type_alias` kinds
# (2,980 real symbols over cockpit's tree). These arms exercise
# `_envelope_for_file` directly at the injection seam it already offers —
# a `Symbol`-shaped fake, never a second venv or a corrupted fixture.
# ---------------------------------------------------------------------------


class _FakeSymbol:
    def __init__(self, name, kind, decl_text=None, range_start_line=1, container_name=None):
        self.name = name
        self.kind = kind
        self.decl_text = decl_text
        self.range_start_line = range_start_line
        self.container_name = container_name


def test_constant_kind_lands_in_constants_with_none_value():
    entry, unmapped = _envelope_for_file(
        "src/consts.ts", [_FakeSymbol("MAX", "constant", range_start_line=3)]
    )

    assert entry["constants"] == [{"name": "MAX", "value": None, "lineno": 3}]
    assert unmapped == {}


def test_type_alias_kind_gets_its_own_key_with_decl_text_as_signature():
    entry, unmapped = _envelope_for_file(
        "src/types.ts",
        [_FakeSymbol("Id", "type_alias", decl_text="type Id = string", range_start_line=7)],
    )

    assert entry["type_aliases"] == [
        {"name": "Id", "signature": "type Id = string", "lineno": 7}
    ]
    assert unmapped == {}


def test_type_aliases_key_absent_when_no_type_alias_symbols():
    entry, unmapped = _envelope_for_file(
        "src/app.ts", [_FakeSymbol("main", "function", decl_text="function main()")]
    )

    assert "type_aliases" not in entry
    assert unmapped == {}


def test_unmapped_kind_is_counted_not_swallowed():
    entry, unmapped = _envelope_for_file(
        "src/enum.ts", [_FakeSymbol("A", "enum_member", range_start_line=2)]
    )

    assert unmapped == {"enum_member": 1}
    # Never fabricated into any known bucket.
    assert entry["constants"] == []
    assert entry["classes"] == []
    assert entry["functions"] == []
    assert "type_aliases" not in entry


def test_nothing_dropped_yields_empty_unmapped_kinds():
    entry, unmapped = _envelope_for_file(
        "src/app.ts",
        [
            _FakeSymbol("main", "function", decl_text="function main()"),
            _FakeSymbol("MAX", "constant"),
        ],
    )

    assert unmapped == {}


def test_build_foreign_symbols_surfaces_unmapped_kinds_in_completeness(monkeypatch, tmp_path):
    import coordinator_core.ops.foreign_symbols as mod

    target = tmp_path / "src.ts"
    target.write_text("// fixture", encoding="utf-8")

    class _FakeExtractionResult:
        symbols = [_FakeSymbol("A", "enum_member", range_start_line=1)]
        symbols[0].file = str(target)
        diagnostics = []

    class _FakeSymbolExtract:
        @staticmethod
        def extract(root, changed_files):
            return _FakeExtractionResult()

        @staticmethod
        def language_for_extension(extension):
            return "typescript"

    monkeypatch.setattr(mod, "_import_symbol_extract", lambda: _FakeSymbolExtract)

    result = build_foreign_symbols(target_root=tmp_path, files=[str(target)])

    assert result["completeness"]["unmapped_kinds"] == {"enum_member": 1}


def test_build_foreign_symbols_omits_unmapped_kinds_when_nothing_dropped(monkeypatch, tmp_path):
    import coordinator_core.ops.foreign_symbols as mod

    target = tmp_path / "src.ts"
    target.write_text("// fixture", encoding="utf-8")

    class _FakeExtractionResult:
        symbols = [_FakeSymbol("main", "function", decl_text="function main()", range_start_line=1)]
        symbols[0].file = str(target)
        diagnostics = []

    class _FakeSymbolExtract:
        @staticmethod
        def extract(root, changed_files):
            return _FakeExtractionResult()

        @staticmethod
        def language_for_extension(extension):
            return "typescript"

    monkeypatch.setattr(mod, "_import_symbol_extract", lambda: _FakeSymbolExtract)

    result = build_foreign_symbols(target_root=tmp_path, files=[str(target)])

    assert "unmapped_kinds" not in result["completeness"]


def test_constants_only_extension_is_not_reported_as_zero_symbols():
    """Regression (EM, C5 follow-up): an extension whose symbols are ALL
    constants must not classify as a corpus fact claiming zero extraction.

    `_count_symbols` fed the per-extension discriminator while counting only
    functions and classes, so the 2,268 constants measured in one real
    consumer tree counted as nothing. The detector built to catch a silently
    thin result was itself producing one.
    """
    from coordinator_core.ops.foreign_symbols import classify_foreign_symbol_coverage

    result = {
        "files": [
            {
                "path": "consts.ts",
                "module_docstring": None,
                "constants": [
                    {"name": "A", "value": None, "lineno": 1},
                    {"name": "B", "value": None, "lineno": 2},
                ],
                "classes": [],
                "functions": [],
            }
        ],
        "diagnostics": [],
        "languages": {".ts": "typescript"},
        "completeness": {},
    }

    findings = classify_foreign_symbol_coverage(result, {".ts": 1})

    assert not [f for f in findings if f["state"] == "missing_grammar"]
    corpus = [f for f in findings if f["state"] == "corpus_fact"]
    assert not corpus, f"constants-only file misreported as yielding nothing: {corpus}"


def test_type_aliases_only_extension_is_not_reported_as_zero_symbols():
    """Same regression for the `type_aliases` bucket (712 in the real tree)."""
    from coordinator_core.ops.foreign_symbols import classify_foreign_symbol_coverage

    result = {
        "files": [
            {
                "path": "types.ts",
                "module_docstring": None,
                "constants": [],
                "classes": [],
                "functions": [],
                "type_aliases": [{"name": "T", "signature": "type T = string", "lineno": 1}],
            }
        ],
        "diagnostics": [],
        "languages": {".ts": "typescript"},
        "completeness": {},
    }

    findings = classify_foreign_symbol_coverage(result, {".ts": 1})
    assert not [f for f in findings if f["state"] == "corpus_fact"]


# ---------------------------------------------------------------------------
# Review dc659900 P1 (no healthy arm) — every extension in the census gets
# an explicit finding, including the ordinary healthy case, never silence.
# ---------------------------------------------------------------------------


def test_healthy_extension_yields_explicit_covered_finding_not_silence():
    result = _result(
        files=[{"path": "src/app.ts", "functions": [{"name": "main", "lineno": 1}]}],
        diagnostics=[],
    )
    findings = classify_foreign_symbol_coverage(result, census={".ts": 1})

    ts_findings = [f for f in findings if f["extension"] == ".ts"]
    assert len(ts_findings) == 1
    assert ts_findings[0]["state"] == "covered"


# ---------------------------------------------------------------------------
# Review dc659900 P1 (markdown/unknown dropped) — the markdown regression
# guard. This is the most important test in this batch: a file whose
# symbols are ALL of an unmapped kind (e.g. markdown heading/code_fence,
# both real SymbolKind.UNKNOWN entries at the pinned ref) must land those
# symbols in `other_symbols`, be counted by `_count_symbols`, and must NOT
# classify as `corpus_fact` ("0 symbols extracted") — the exact defect that
# hid a fully-successful markdown extraction as an empty corpus.
# ---------------------------------------------------------------------------


def test_unknown_kind_symbols_land_in_other_symbols_and_are_counted():
    entry, unmapped = _envelope_for_file(
        "README.md",
        [
            _FakeSymbol("Intro", "unknown", decl_text=None, range_start_line=1),
            _FakeSymbol("unknown", "unknown", decl_text="```python\n...\n```", range_start_line=5),
        ],
    )

    assert entry["other_symbols"] == [
        {"name": "Intro", "kind": "unknown", "lineno": 1},
        {
            "name": "unknown",
            "kind": "unknown",
            "signature": "```python\n...\n```",
            "lineno": 5,
        },
    ]
    assert unmapped == {"unknown": 2}


def test_markdown_all_unknown_kind_file_does_not_classify_as_corpus_fact():
    from coordinator_core.ops.foreign_symbols import _count_symbols

    entry, unmapped = _envelope_for_file(
        "README.md",
        [
            _FakeSymbol("Heading", "unknown", range_start_line=1),
            _FakeSymbol("code_fence", "unknown", decl_text="```\ncode\n```", range_start_line=3),
        ],
    )

    assert unmapped == {"unknown": 2}
    assert _count_symbols(entry) == 2

    result = {
        "files": [entry],
        "diagnostics": [],
        "languages": {".md": "markdown"},
        "completeness": {"unmapped_kinds": unmapped},
    }
    findings = classify_foreign_symbol_coverage(result, census={".md": 1})

    md_findings = [f for f in findings if f["extension"] == ".md"]
    assert len(md_findings) == 1
    assert md_findings[0]["state"] == "covered"
    assert md_findings[0]["state"] != "corpus_fact"


def test_other_symbols_key_absent_when_nothing_unmapped():
    entry, unmapped = _envelope_for_file(
        "src/app.ts", [_FakeSymbol("main", "function", decl_text="function main()")]
    )

    assert "other_symbols" not in entry
    assert unmapped == {}


def test_other_symbols_signature_key_absent_when_decl_text_falsy():
    entry, unmapped = _envelope_for_file(
        "README.md", [_FakeSymbol("Heading", "unknown", decl_text=None, range_start_line=1)]
    )

    assert entry["other_symbols"] == [{"name": "Heading", "kind": "unknown", "lineno": 1}]
    assert "signature" not in entry["other_symbols"][0]


# ---------------------------------------------------------------------------
# Review dc659900 P2 — an error-level, file-less diagnostic that names no
# language present in the census must not silently vanish, letting the
# affected extension fall through to `corpus_fact`/`covered` with no signal.
# ---------------------------------------------------------------------------


def test_unattributable_error_diagnostic_produces_loud_finding_not_corpus_fact():
    diagnostic = {
        "level": "error",
        "message": "grammar unavailable for rust: No module named 'tree_sitter_rust'",
        "file": None,
        "line": None,
    }
    result = _result(files=[], diagnostics=[diagnostic])
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    unattributed = [f for f in findings if f["state"] == "unattributed_diagnostic"]
    assert len(unattributed) == 1
    assert "rust" in unattributed[0]["detail"]

    # No extension's per-extension arm must silently absorb this diagnostic
    # as a clean-bill-of-health corpus_fact.
    corpus_facts_matching = [
        f for f in findings if f["state"] == "corpus_fact" and diagnostic["message"] in f["detail"]
    ]
    assert corpus_facts_matching == []


# ---------------------------------------------------------------------------
# Review 60d298e7 (peer sidecar) P2 — all eight JS/TS extensions get their
# own independently asserted per-extension arm (AC4), not just .ts/.mjs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extension", [".ts", ".tsx", ".js", ".mjs", ".mts", ".cts", ".jsx", ".cjs"])
def test_every_js_ts_extension_gets_its_own_asserted_finding(extension):
    result = _result(files=[], diagnostics=[])
    findings = classify_foreign_symbol_coverage(result, census=_COCKPIT_CENSUS)

    this_ext = [f for f in findings if f["extension"] == extension]
    assert len(this_ext) == 1
    assert this_ext[0]["state"] == "corpus_fact"
