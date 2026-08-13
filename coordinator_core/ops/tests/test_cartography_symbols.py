"""
coordinator_core.ops.tests.test_cartography_symbols

Op-level tests for `cartography.symbols` after chunk C3 routes non-Python
files through `coordinator_core.ops.foreign_symbols.build_foreign_symbols`.

Coverage:
  - the `.py` path is byte-identical to `build_symbols` (no regression risk)
  - a mixed Python + TypeScript request merges into one `{"files": [...]}`
    reply, in request order
  - an extension `symbol_extract`'s own `languages` resolution cannot name
    is recorded in-band as `unsupported`, never silently dropped
  - the op raises loud on `missing_grammar` and `partial_coverage` —
    matching classify_foreign_symbol_coverage's own per-extension
    discriminator (coordinator_core/ops/foreign_symbols.py)
  - `dependency_absent` degrades gracefully instead of raising (chunk C4a):
    an "unavailable"-marked entry per extraction-eligible file plus a
    top-level `coverage_note`, never confusable with an empty-but-successful
    extraction

`symbol_extract` is NOT installed in this environment and must not be
installed here — every non-corpus-fact arm is exercised by monkeypatching
`coordinator_core.ops.foreign_symbols.build_foreign_symbols` and
`classify_foreign_symbol_coverage` at the seam `cartography_symbols.py`
imports them through, mirroring `test_foreign_symbols.py`'s own injection
strategy. No skip guards on anything reachable by injection.

Spec backlink: pln-cartography-consumes-symbol-ex-3fc0a8
§ chunk C3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_symbols  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_symbols import CoverageFaultError, _cartography_symbols
from coordinator_core.cartography.symbols import build_symbols

_OP_NAME = "cartography.symbols"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_symbols @register_op did not fire"
)


@pytest.fixture()
def target_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n", encoding="utf-8"
    )
    (root / "app.ts").write_text("export function main() {}\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# .py path unchanged
# ---------------------------------------------------------------------------


def test_python_only_request_matches_build_symbols_verbatim(target_root):
    result = _cartography_symbols({"target_root": str(target_root), "files": ["main.py"]})
    baseline = build_symbols(target_root, ["main.py"])
    assert result == baseline


# ---------------------------------------------------------------------------
# Mixed Python + non-Python merge, request-order preserved
# ---------------------------------------------------------------------------


def test_mixed_python_and_foreign_request_merges_into_one_reply(target_root, monkeypatch):
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {
            "files": [
                {
                    "path": "app.ts",
                    "module_docstring": None,
                    "constants": [],
                    "classes": [],
                    "functions": [{"name": "main", "signature": "function main()", "lineno": 1}],
                }
            ],
            "diagnostics": [],
            "languages": {".ts": "typescript"},
            "completeness": {"authority": "project_treesitter", "feature_level": "lite"},
        }

    def fake_classify(result, census):
        return [
            {
                "state": "corpus_fact",
                "extension": ".ts",
                "language": "typescript",
                "detail": "1 file(s) of extension '.ts'; 1 symbol(s) extracted",
            }
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    result = _cartography_symbols(
        {"target_root": str(target_root), "files": ["main.py", "app.ts"]}
    )

    assert [entry["path"] for entry in result["files"]] == ["main.py", "app.ts"]
    assert result["files"][0] == build_symbols(target_root, ["main.py"])["files"][0]
    assert result["files"][1]["functions"][0]["name"] == "main"


def test_reversed_request_order_is_preserved_in_reply(target_root, monkeypatch):
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {
            "files": [
                {
                    "path": "app.ts",
                    "module_docstring": None,
                    "constants": [],
                    "classes": [],
                    "functions": [],
                }
            ],
            "diagnostics": [],
            "languages": {".ts": "typescript"},
            "completeness": {},
        }

    def fake_classify(result, census):
        return [
            {"state": "corpus_fact", "extension": ".ts", "language": "typescript", "detail": "d"}
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    result = _cartography_symbols(
        {"target_root": str(target_root), "files": ["app.ts", "main.py"]}
    )
    assert [entry["path"] for entry in result["files"]] == ["app.ts", "main.py"]


# ---------------------------------------------------------------------------
# Unsupported extension — recorded in-band, never dropped
# ---------------------------------------------------------------------------


def test_genuinely_unsupported_extension_recorded_without_reaching_adapter(target_root, monkeypatch):
    """The early-partition branch: `.xyz` is not in `claimed_extensions()`,
    so it never reaches `build_foreign_symbols`/`classify_foreign_symbol_coverage`
    at all — the fakes here are trip-wires proving that, not exercised logic."""
    (target_root / "notes.xyz").write_text("whatever", encoding="utf-8")
    import coordinator_core.ops.cartography_symbols as mod

    def boom_build_foreign_symbols(root, files):
        raise AssertionError("build_foreign_symbols must not be called for a non-claimed extension")

    def boom_classify(result, census):
        raise AssertionError("classify_foreign_symbol_coverage must not be called for a non-claimed extension")

    monkeypatch.setattr(mod, "build_foreign_symbols", boom_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", boom_classify)

    result = _cartography_symbols({"target_root": str(target_root), "files": ["notes.xyz"]})

    entry = result["files"][0]
    assert entry["path"] == "notes.xyz"
    assert entry.get("unsupported") is True
    assert entry.get("detail")


def test_claimed_but_unmapped_extension_recorded_as_unsupported_after_adapter_call(
    target_root, monkeypatch
):
    """The post-adapter branch (cartography_symbols.py `languages.get(ext)
    is None`): `.md` IS in `claimed_extensions()`, so it reaches the adapter,
    but the adapter's own `languages` resolution names no language for it —
    this must still be recorded as `unsupported`, and (per the merge-not-
    overwrite fix) a pre-existing per-file "error" diagnostic on that entry
    must survive the re-marking rather than being silently discarded."""
    (target_root / "notes.md").write_text("# heading", encoding="utf-8")
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {
            "files": [
                {
                    "path": "notes.md",
                    "module_docstring": None,
                    "constants": [],
                    "classes": [],
                    "functions": [],
                    "error": "pre-existing per-file diagnostic",
                }
            ],
            "diagnostics": [],
            "languages": {".md": None},
            "completeness": {},
        }

    def fake_classify(result, census):
        return [
            {"state": "corpus_fact", "extension": ".md", "language": None, "detail": "d"}
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    result = _cartography_symbols({"target_root": str(target_root), "files": ["notes.md"]})

    entry = result["files"][0]
    assert entry["path"] == "notes.md"
    assert entry.get("unsupported") is True
    assert entry.get("detail")
    assert entry.get("error") == "pre-existing per-file diagnostic"


# ---------------------------------------------------------------------------
# Fail-loud arms — dependency_absent, missing_grammar, partial_coverage
# ---------------------------------------------------------------------------


def test_dependency_absent_degrades_gracefully_ts_only(target_root, monkeypatch):
    """chunk C4a: dependency_absent must NOT raise — it is an expected,
    benign configuration (symbol_extract ships from a private repo)."""
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {"unavailable": "symbol_extract not installed: No module named 'symbol_extract'"}

    def fake_classify(result, census):
        return [
            {"state": "dependency_absent", "extension": None, "language": None, "detail": result["unavailable"]}
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    result = _cartography_symbols({"target_root": str(target_root), "files": ["app.ts"]})

    entry = result["files"][0]
    assert entry["path"] == "app.ts"
    assert entry.get("unavailable") is True
    assert entry.get("detail")
    # Must not be confusable with a successful-but-empty extraction.
    assert "classes" not in entry
    assert "functions" not in entry
    assert "constants" not in entry

    assert "coverage_note" in result
    assert "symbol_extract" in result["coverage_note"]
    assert "example-retrieval-repo-symbol-extract" in result["coverage_note"]


def test_dependency_absent_mixed_python_still_extracts_python(target_root, monkeypatch):
    """chunk C4a: the .py path is unaffected by a graceful non-Python
    degradation in the same request."""
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {"unavailable": "symbol_extract not installed: No module named 'symbol_extract'"}

    def fake_classify(result, census):
        return [
            {"state": "dependency_absent", "extension": None, "language": None, "detail": result["unavailable"]}
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    result = _cartography_symbols(
        {"target_root": str(target_root), "files": ["main.py", "app.ts"]}
    )

    assert [entry["path"] for entry in result["files"]] == ["main.py", "app.ts"]
    assert result["files"][0] == build_symbols(target_root, ["main.py"])["files"][0]
    assert result["files"][1].get("unavailable") is True
    assert "coverage_note" in result


def test_missing_grammar_raises_loud(target_root, monkeypatch):
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {"files": [], "diagnostics": [], "languages": {".ts": "typescript"}, "completeness": {}}

    def fake_classify(result, census):
        return [
            {
                "state": "missing_grammar",
                "extension": ".ts",
                "language": "typescript",
                "detail": "1 file(s) of extension '.ts' (typescript) requested but 0 symbols",
            }
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    with pytest.raises(ValueError, match="missing_grammar"):
        _cartography_symbols({"target_root": str(target_root), "files": ["app.ts"]})


def test_missing_grammar_raises_loud_but_carries_computed_python_results(target_root, monkeypatch):
    """Regression guard for the P2 fix (coordinatorcode-reviewer-1624a2c9.md):
    a mixed .py + .ts request where the .ts leg faults must still raise, but
    must not discard main.py's already-computed, unrelated symbol table —
    it must be recoverable from the raised exception's `partial_reply`."""
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {"files": [], "diagnostics": [], "languages": {".ts": "typescript"}, "completeness": {}}

    def fake_classify(result, census):
        return [
            {
                "state": "missing_grammar",
                "extension": ".ts",
                "language": "typescript",
                "detail": "1 file(s) of extension '.ts' (typescript) requested but 0 symbols",
            }
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    with pytest.raises(CoverageFaultError, match="missing_grammar") as excinfo:
        _cartography_symbols(
            {"target_root": str(target_root), "files": ["main.py", "app.ts"]}
        )

    assert isinstance(excinfo.value, ValueError)
    partial = excinfo.value.partial_reply
    assert [entry["path"] for entry in partial["files"]] == ["main.py"]
    assert partial["files"][0] == build_symbols(target_root, ["main.py"])["files"][0]


def test_partial_coverage_raises_loud(target_root, monkeypatch):
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {
            "files": [{"path": "app.ts", "module_docstring": None, "constants": [], "classes": [], "functions": [{"name": "main", "signature": "function main()", "lineno": 1}]}],
            "diagnostics": [],
            "languages": {".ts": "typescript"},
            "completeness": {},
        }

    def fake_classify(result, census):
        return [
            {
                "state": "partial_coverage",
                "extension": ".ts",
                "language": "typescript",
                "detail": "1 symbol(s) extracted but a grammar-level error diagnostic names this language: 1474 file(s) skipped",
            }
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    with pytest.raises(ValueError, match="partial_coverage"):
        _cartography_symbols({"target_root": str(target_root), "files": ["app.ts"]})


def test_parse_failure_does_not_raise_and_stays_per_file(target_root, monkeypatch):
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {
            "files": [{"path": "app.ts", "error": "parse error: unexpected token"}],
            "diagnostics": [{"level": "error", "message": "parse error in app.ts", "file": "app.ts", "line": 1}],
            "languages": {".ts": "typescript"},
            "completeness": {},
        }

    def fake_classify(result, census):
        return [
            {"state": "parse_failure", "extension": ".ts", "language": None, "detail": "app.ts: parse error"}
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    result = _cartography_symbols({"target_root": str(target_root), "files": ["app.ts"]})
    assert result["files"][0]["path"] == "app.ts"
    assert result["files"][0]["error"]


# ---------------------------------------------------------------------------
# Required-param errors unchanged
# ---------------------------------------------------------------------------


def test_missing_target_root_raises_descriptive_value_error():
    with pytest.raises(ValueError, match="target_root"):
        _cartography_symbols({"files": ["main.py"]})


def test_missing_files_raises_descriptive_value_error(target_root):
    with pytest.raises(ValueError, match="files"):
        _cartography_symbols({"target_root": str(target_root)})


# ---------------------------------------------------------------------------
# C3-fix — unsupported extensions must not enter the extraction path
# ---------------------------------------------------------------------------


def test_python_and_unsupported_extension_does_not_call_adapter_or_raise(target_root, monkeypatch):
    """A `.py` + a genuinely unsupported extension (e.g. `.txt`) must never
    reach `build_foreign_symbols` — with the real (uninstalled)
    `symbol_extract` dependency, calling it would raise `dependency_absent`
    even though nothing extraction-eligible was requested."""
    (target_root / "notes.txt").write_text("plain text", encoding="utf-8")
    import coordinator_core.ops.cartography_symbols as mod

    def boom_build_foreign_symbols(root, files):
        raise AssertionError("build_foreign_symbols must not be called for unsupported-only files")

    def boom_classify(result, census):
        raise AssertionError("classify_foreign_symbol_coverage must not be called for unsupported-only files")

    monkeypatch.setattr(mod, "build_foreign_symbols", boom_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", boom_classify)

    result = _cartography_symbols(
        {"target_root": str(target_root), "files": ["main.py", "notes.txt"]}
    )

    assert [entry["path"] for entry in result["files"]] == ["main.py", "notes.txt"]
    assert result["files"][0] == build_symbols(target_root, ["main.py"])["files"][0]
    unsupported_entry = result["files"][1]
    assert unsupported_entry["path"] == "notes.txt"
    assert unsupported_entry.get("unsupported") is True
    assert unsupported_entry.get("detail")


def test_only_unsupported_extensions_returns_without_raising(target_root, monkeypatch):
    (target_root / "notes.txt").write_text("plain text", encoding="utf-8")
    import coordinator_core.ops.cartography_symbols as mod

    def boom_build_foreign_symbols(root, files):
        raise AssertionError("build_foreign_symbols must not be called for unsupported-only files")

    def boom_classify(result, census):
        raise AssertionError("classify_foreign_symbol_coverage must not be called for unsupported-only files")

    monkeypatch.setattr(mod, "build_foreign_symbols", boom_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", boom_classify)

    result = _cartography_symbols({"target_root": str(target_root), "files": ["notes.txt"]})

    entry = result["files"][0]
    assert entry["path"] == "notes.txt"
    assert entry.get("unsupported") is True
    assert entry.get("detail")


def test_python_and_extraction_eligible_extension_degrades_gracefully_with_real_seam(
    target_root, monkeypatch
):
    """chunk C4a: a request that genuinely asks for an extraction-eligible
    extension (`.ts`) while `symbol_extract` is absent must return, not raise —
    Python still extracts normally, and the `.ts` entry gets the unavailable
    marker plus a coverage_note.

    Absence is FORCED at the real import seam rather than inherited from the
    ambient environment. Relying on the machine not having the package made
    this test assert the opposite of its own name once the optional extra was
    installed: extraction succeeded and the `.ts` entry came back as a real
    populated envelope. A test whose meaning depends on install state is not a
    test of the degrade path.
    """
    import coordinator_core.ops.foreign_symbols as fs

    def no_symbol_extract():
        raise ImportError("No module named 'symbol_extract'")

    monkeypatch.setattr(fs, "_import_symbol_extract", no_symbol_extract)

    result = _cartography_symbols(
        {"target_root": str(target_root), "files": ["main.py", "app.ts"]}
    )
    assert [entry["path"] for entry in result["files"]] == ["main.py", "app.ts"]
    assert result["files"][0] == build_symbols(target_root, ["main.py"])["files"][0]
    ts_entry = result["files"][1]
    assert ts_entry.get("unavailable") is True
    assert "classes" not in ts_entry
    assert "functions" not in ts_entry
    assert "coverage_note" in result
    assert "example-retrieval-repo-symbol-extract" in result["coverage_note"]


def test_missing_grammar_still_raises_with_dependency_present(target_root, monkeypatch):
    """Regression guard: missing_grammar must still raise loudly — this fix
    only softens dependency_absent, not the other loud coverage faults."""
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {"files": [], "diagnostics": [], "languages": {".ts": "typescript"}, "completeness": {}}

    def fake_classify(result, census):
        return [
            {
                "state": "missing_grammar",
                "extension": ".ts",
                "language": "typescript",
                "detail": "1 file(s) of extension '.ts' (typescript) requested but 0 symbols",
            }
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    with pytest.raises(ValueError, match="missing_grammar"):
        _cartography_symbols({"target_root": str(target_root), "files": ["app.ts"]})


def test_partial_coverage_still_raises_with_dependency_present(target_root, monkeypatch):
    """Regression guard: partial_coverage must still raise loudly — this fix
    only softens dependency_absent, not the other loud coverage faults."""
    import coordinator_core.ops.cartography_symbols as mod

    def fake_build_foreign_symbols(root, files):
        return {
            "files": [{"path": "app.ts", "module_docstring": None, "constants": [], "classes": [], "functions": [{"name": "main", "signature": "function main()", "lineno": 1}]}],
            "diagnostics": [],
            "languages": {".ts": "typescript"},
            "completeness": {},
        }

    def fake_classify(result, census):
        return [
            {
                "state": "partial_coverage",
                "extension": ".ts",
                "language": "typescript",
                "detail": "1 symbol(s) extracted but a grammar-level error diagnostic names this language: 1474 file(s) skipped",
            }
        ]

    monkeypatch.setattr(mod, "build_foreign_symbols", fake_build_foreign_symbols)
    monkeypatch.setattr(mod, "classify_foreign_symbol_coverage", fake_classify)

    with pytest.raises(ValueError, match="partial_coverage"):
        _cartography_symbols({"target_root": str(target_root), "files": ["app.ts"]})


def test_real_typescript_extraction_end_to_end(target_root):
    """AC1, end to end through the real seam: a TypeScript file yields a
    non-empty symbol table in the same envelope Python already emits.

    Every other test in this module forces the dependency's absence, so none
    of them can prove the payoff — that the adapter actually extracts. This
    one is the only test that exercises the real `symbol_extract`, so it is
    the only legitimate use of a skip guard here: it is unrunnable, not
    failing, when the optional `symbols` extra is not installed.
    """
    pytest.importorskip(
        "symbol_extract",
        reason="the optional [symbols] extra is not installed in this environment",
    )

    result = _cartography_symbols(
        {"target_root": str(target_root), "files": ["main.py", "app.ts"]}
    )

    ts_entry = next(e for e in result["files"] if e["path"] == "app.ts")
    assert "unavailable" not in ts_entry, "real extraction should not degrade"
    assert "error" not in ts_entry
    assert ts_entry["functions"], "expected at least one TypeScript function symbol"
    assert {"path", "module_docstring", "constants", "classes", "functions"} <= set(ts_entry)

    py_entry = next(e for e in result["files"] if e["path"] == "main.py")
    assert py_entry["functions"], "the Python path must be unaffected"
