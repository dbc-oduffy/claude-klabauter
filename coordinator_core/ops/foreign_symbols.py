"""
coordinator_core.ops.foreign_symbols — lazy-import seam over example-retrieval-repo's
packaged ``symbol_extract``, emitting ``cartography.symbols``'s existing
per-file envelope for non-Python languages (TypeScript, C++, Markdown).

Purpose: ``cartography.symbols`` covers only Python via the stdlib ``ast``
module (see ``coordinator_core.cartography.symbols``). This module is the
adapter that lets the rest of that family's non-Python languages produce a
symbol table in the same shape, backed by ``symbol_extract``'s tree-sitter
extraction — without pulling that dependency into the ``cartography``
package itself. It lives in the ops layer, not
``coordinator_core/cartography/``, precisely so that package's out-degree-0 /
stdlib-only property stays intact: nothing in ``cartography`` imports this
module or ``symbol_extract``.

DR-208 read/return boundary: COMPUTE_ONLY. This module reads source text
(via ``symbol_extract.extract``'s internal walk/parse) and returns a
computed dict; it writes nothing, shells out to nothing, and mutates no
shared state. ``extract()``'s own containment story: it WALKS the whole
tree under ``target_root`` (a cheap ``os.walk``-class directory
enumeration) but PARSES only the files named in ``changed_files`` — so this
module's own read/return boundary stays ``files``-scoped even though the
walk itself is not.

Negative-spec:
  - Does NOT import ``symbol_extract`` at module scope — only inside
    ``_import_symbol_extract()``, so a caller that never reaches this seam
    never pays its cold-import cost, and ``import
    coordinator_core.ops.foreign_symbols`` alone never touches
    ``sys.modules['symbol_extract']``.
  - Does NOT perform any name-matching or containment inference of its
    own kind: a method's ``container_name`` is trusted verbatim from
    ``symbol_extract``'s ``Symbol`` — nesting into a class's ``methods`` is
    keyed off exact-name lookup against classes already produced by this
    same extraction pass, never reconstructed from ``qualified_name`` or
    any other heuristic. A ``container_name`` of ``None``, or one that does
    not name a class symbol in the same file, leaves that method flat under
    ``functions`` — never guessed.
  - Does NOT route ``ExtractionResult.edges`` anywhere — it is frozen but
    empty; cartography's edge story stays Python-only by design (AC5).
  - Does NOT aggregate coverage across extensions or files.
    ``classify_foreign_symbol_coverage`` returns one finding per
    extension/file condition, never a rolled-up "some symbols found" pass.
  - Does NOT swallow an absent dependency into an empty-symbols result —
    ``build_foreign_symbols`` returns a distinct ``{"unavailable": ...}``
    dict, checked first and always, by every caller of this seam.

Spec backlink: pln-cartography-consumes-symbol-ex-3fc0a8
§ chunk C1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from coordinator_core.cartography._guard import path_guard

# Kinds that map onto the envelope's "classes" bucket (ENVELOPE MAPPING table,
# plan § C1). Compared against the string value of `Symbol.kind` (a StrEnum) —
# never against an exact-set assertion of the enum's full member list, whose
# vocabulary grows additively upstream.
_CLASS_KIND_VALUES = frozenset({"class", "struct", "interface", "enum", "namespace"})

# Kinds that map onto the envelope's "functions" bucket.
_FUNCTION_KIND_VALUES = frozenset({"function", "method"})

# Kinds that map onto the envelope's "constants" bucket (chunk C5 fix — the
# ENVELOPE MAPPING table's "not modeled by symbol_extract" claim for
# `constants` was factually wrong; `SymbolKind.CONSTANT` is a live, frequent
# member of their frozen enum).
_CONSTANT_KIND_VALUES = frozenset({"constant"})

# Kinds that map onto the envelope's "type_aliases" bucket — no Python-
# producer analogue key exists, so this is a new key, added only when a file
# yields at least one (chunk C5).
_TYPE_ALIAS_KIND_VALUES = frozenset({"type_alias"})

# Static mirror of ``symbol_extract.languages``' frozen public extension map
# (CPP/PYTHON/TS/MD/JSON/YAML/TOML). This is a copy of an explicitly frozen upstream public
# surface, kept here so routing decisions (which extensions are even
# claimed by the foreign-extraction path) work BEFORE the dependency is
# provisioned — `claimed_extensions()` below prefers the live map whenever
# `symbol_extract` is importable, so an additive extension on their side is
# picked up with no lag on our side; this frozenset is only the fallback.
_STATIC_CLAIMED_EXTENSIONS = frozenset(
    {
        # CPP
        ".cpp", ".cc", ".cxx", ".c", ".h", ".hh", ".hpp", ".inl",
        # PYTHON
        ".py",
        # TS
        ".ts", ".mts", ".cts", ".tsx", ".jsx", ".js", ".mjs", ".cjs",
        # MD
        ".md", ".mdx",
        # JSON/YAML/TOML
        ".json", ".yaml", ".yml", ".toml",
    }
)


def claimed_extensions() -> "frozenset[str]":
    """Return the set of extensions this seam claims to be able to extract.

    Prefers ``symbol_extract.ALL_EXTENSIONS`` when the dependency is
    importable — a frozen, exported name (their ``__all__``), and the union of
    their CPP/PYTHON/TS/MD sets. Reading it wholesale rather than filtering our
    own candidate list means an extension ADDED upstream is picked up with no
    lag on our side, which a filter over a local mirror cannot do.

    Falls back to ``_STATIC_CLAIMED_EXTENSIONS`` — a mirror of that same frozen
    public surface — when the dependency is not installed, so callers can route
    on extension identity alone before the dependency is provisioned. The
    mirror is the only reason routing works pre-provisioning; it is expected to
    lag an upstream addition until the pin moves, and that lag is confined to
    the not-installed case.
    """
    try:
        symbol_extract = _import_symbol_extract()
    except ImportError:
        return _STATIC_CLAIMED_EXTENSIONS

    upstream = getattr(symbol_extract, "ALL_EXTENSIONS", None)
    if upstream:
        return frozenset(upstream)
    return _STATIC_CLAIMED_EXTENSIONS


def _import_symbol_extract():
    """Lazily import and return the ``symbol_extract`` module.

    Isolated in its own function — never imported at module scope (AC7) —
    so tests can monkeypatch this call site, and so any caller that never
    reaches this seam never pays ``symbol_extract``'s cold-import cost.

    Raises:
        ImportError: propagated verbatim if ``symbol_extract`` is not
        installed. Callers (``build_foreign_symbols``) catch this and
        convert it into a structured, non-raising ``{"unavailable": ...}``
        result — this function itself never swallows the error.
    """
    import symbol_extract

    return symbol_extract


def _is_class_kind(kind_value: str) -> bool:
    if kind_value in _CLASS_KIND_VALUES:
        return True
    # "abstract class" / similar compound forms (ENVELOPE MAPPING table:
    # "class/struct/interface/enum/namespace (and any abstract-class form)").
    # Speculative forward-compat, not presently reachable: the installed
    # dependency's frozen `SymbolKind` vocabulary contains no member whose
    # string value includes "abstract" (review dc659900, Nit).
    return "class" in kind_value and "abstract" in kind_value


def _is_function_kind(kind_value: str) -> bool:
    return kind_value in _FUNCTION_KIND_VALUES


def _is_constant_kind(kind_value: str) -> bool:
    return kind_value in _CONSTANT_KIND_VALUES


def _is_type_alias_kind(kind_value: str) -> bool:
    return kind_value in _TYPE_ALIAS_KIND_VALUES


def _class_entry(symbol: Any) -> Dict[str, Any]:
    return {
        "name": symbol.name,
        "docstring": None,
        "methods": [],
        "lineno": symbol.range_start_line,
    }


def _function_entry(symbol: Any) -> Dict[str, Any]:
    return {
        "name": symbol.name,
        "signature": symbol.decl_text,
        "lineno": symbol.range_start_line,
    }


def _constant_entry(symbol: Any) -> Dict[str, Any]:
    # `symbol_extract` carries no literal value — `value: None` is honest
    # and distinguishable from the Python `ast` producer's real literals
    # (chunk C5).
    return {
        "name": symbol.name,
        "value": None,
        "lineno": symbol.range_start_line,
    }


def _type_alias_entry(symbol: Any) -> Dict[str, Any]:
    return {
        "name": symbol.name,
        "signature": symbol.decl_text,
        "lineno": symbol.range_start_line,
    }


def _envelope_for_file(rel_path: str, symbols: List[Any]):
    """Assemble one file's envelope entry from its ``Symbol`` list.

    Class nesting is keyed strictly off ``container_name`` matching a class
    symbol's own ``name`` within THIS file's symbol set — never off
    ``qualified_name`` or any other reconstruction (AC6). A method whose
    ``container_name`` is ``None``, or does not match any class produced in
    this same file, is emitted flat under ``functions``.

    Returns ``(entry, unmapped_kinds)`` — ``unmapped_kinds`` is a
    ``{kind_value: count}`` dict of any ``Symbol.kind`` in this file's
    extraction that matched none of the class/function/constant/type_alias
    buckets (chunk C5). Never silently dropped: each such symbol is ALSO
    appended to ``entry["other_symbols"]`` (key present only when non-empty)
    as ``{"name", "kind", "signature", "lineno"}`` — ``signature`` from
    ``decl_text``, omitted when falsy — and counted by ``_count_symbols``.
    The caller additionally rolls ``unmapped_kinds`` into the top-level
    ``completeness["unmapped_kinds"]`` marker, whose meaning is a census of
    what landed in ``other_symbols`` across the whole result, not (as of the
    P1 fix below) a report of data loss — nothing in this envelope is
    dropped on kind mismatch anymore.
    """
    classes: List[Dict[str, Any]] = []
    class_by_name: Dict[str, Dict[str, Any]] = {}
    functions: List[Dict[str, Any]] = []
    constants: List[Dict[str, Any]] = []
    type_aliases: List[Dict[str, Any]] = []
    other_symbols: List[Dict[str, Any]] = []
    unmapped_kinds: Dict[str, int] = {}

    for symbol in symbols:
        kind_value = str(symbol.kind)
        if _is_class_kind(kind_value):
            entry = _class_entry(symbol)
            classes.append(entry)
            class_by_name.setdefault(symbol.name, entry)

    for symbol in symbols:
        kind_value = str(symbol.kind)
        if _is_class_kind(kind_value):
            continue
        if _is_function_kind(kind_value):
            container = symbol.container_name
            if container is not None and container in class_by_name:
                class_by_name[container]["methods"].append(_function_entry(symbol))
            else:
                functions.append(_function_entry(symbol))
        elif _is_constant_kind(kind_value):
            constants.append(_constant_entry(symbol))
        elif _is_type_alias_kind(kind_value):
            type_aliases.append(_type_alias_entry(symbol))
        else:
            unmapped_kinds[kind_value] = unmapped_kinds.get(kind_value, 0) + 1
            other_entry: Dict[str, Any] = {
                "name": symbol.name,
                "kind": kind_value,
                "lineno": symbol.range_start_line,
            }
            signature = symbol.decl_text
            if signature:
                other_entry["signature"] = signature
            other_symbols.append(other_entry)

    result: Dict[str, Any] = {
        "path": rel_path,
        "module_docstring": None,
        "constants": constants,
        "classes": classes,
        "functions": functions,
    }
    if type_aliases:
        result["type_aliases"] = type_aliases
    if other_symbols:
        # Review: reviewer dc659900 P1 — any Symbol.kind matching none of the
        # class/function/constant/type_alias buckets (e.g. markdown
        # heading/code_fence, both mapped to SymbolKind.UNKNOWN upstream)
        # lands here instead of vanishing from _count_symbols entirely.
        result["other_symbols"] = other_symbols
    return result, unmapped_kinds


def build_foreign_symbols(target_root: "str | Path", files: List["str | Path"]) -> Dict[str, Any]:
    """Build the ``{"files": [...]}`` envelope for a list of non-Python files.

    Args:
        target_root: containment root — every entry in ``files`` must
            resolve under it (``cartography._guard.path_guard``).
        files: absolute or ``target_root``-relative paths to extract symbols
            for. Resolved and passed to ``symbol_extract.extract`` as
            ``changed_files`` (a set of resolved absolute paths), so only
            these files are PARSED even though ``extract`` still WALKS the
            whole tree internally.

    Returns:
        ``{"unavailable": "symbol_extract not installed: <exc>"}`` if
        ``symbol_extract`` cannot be imported — a distinct, loud state,
        never folded into a zero-symbols/empty-diagnostics result.

        Otherwise:
        ``{"files": [<envelope entry per file, including files requested
        but absent from the extraction result>, ...],
        "diagnostics": [<dict per ExtractionDiagnostic>, ...],
        "languages": {<extension>: <language tag or None>, ...} (one entry
        per extension among the REQUESTED files, resolved via
        ``symbol_extract.language_for_extension`` here, at build time —
        so ``classify_foreign_symbol_coverage`` never needs its own
        import of ``symbol_extract`` on this path),
        "completeness": {"authority": "project_treesitter",
        "feature_level": "lite", "edges": False,
        "cross_file_resolution": False,
        "container_name_resolved": False}}``

        A diagnostic carrying a non-None ``file`` is additionally attributed
        onto that file's envelope entry as an ``"error"`` field, mirroring
        ``cartography/symbols.py``'s existing per-file-failure convention —
        EXCEPT a name-invariant drop (``_is_name_invariant_drop`` is True),
        which is routed instead to a separate per-entry key
        ``name_invariant_drops`` (a list of the drop diagnostics' messages,
        created lazily only when such a drop occurs). A name-invariant drop
        means one SYMBOL's name was rejected by upstream's
        ``ExtractionResult.__post_init__`` validating choke point — the file
        itself parsed cleanly, so marking its envelope entry ``"error"``
        would be a defect: it would mark a healthy file as errored.
    """
    try:
        symbol_extract = _import_symbol_extract()
    except ImportError as exc:
        return {"unavailable": f"symbol_extract not installed: {exc}"}

    root = Path(target_root).resolve()
    resolved_files: Dict[str, Path] = {}
    rel_by_resolved: Dict[str, str] = {}
    for f in files:
        resolved = path_guard(root, f)
        resolved_files[str(resolved)] = resolved
        rel_by_resolved[str(resolved)] = resolved.relative_to(root).as_posix()

    result = symbol_extract.extract(
        root,
        changed_files=set(resolved_files.keys()),
    )

    symbols_by_file: Dict[str, List[Any]] = {rel: [] for rel in rel_by_resolved.values()}
    for symbol in result.symbols:
        symbol_path = Path(symbol.file)
        if not symbol_path.is_absolute():
            symbol_path = (root / symbol_path).resolve()
        rel = rel_by_resolved.get(str(symbol_path))
        if rel is None:
            # A symbol outside the requested set — not expected given
            # changed_files scoping. NOTE: this entry is built and
            # path-attributed here, but `ordered_files` below iterates only
            # `rel_by_resolved.values()`, so it is NOT emitted in the
            # returned "files" list — it is not currently visible to any
            # caller (review dc659900, Nit). Low-likelihood given
            # changed_files scoping; tighten if this path is meant to be
            # reachable.
            rel = symbol_path.relative_to(root).as_posix() if symbol_path.is_relative_to(root) else str(symbol_path)
            symbols_by_file.setdefault(rel, [])
        symbols_by_file[rel].append(symbol)

    envelope_by_rel: Dict[str, Dict[str, Any]] = {}
    unmapped_kinds_total: Dict[str, int] = {}
    for rel, syms in symbols_by_file.items():
        entry, file_unmapped = _envelope_for_file(rel, syms)
        envelope_by_rel[rel] = entry
        for kind_value, count in file_unmapped.items():
            unmapped_kinds_total[kind_value] = unmapped_kinds_total.get(kind_value, 0) + count

    diagnostics: List[Dict[str, Any]] = []
    for diag in result.diagnostics:
        diagnostics.append(
            {
                "level": diag.level,
                "message": diag.message,
                "file": diag.file,
                "line": diag.line,
                # `getattr(..., None)` is deliberate and NOT part of the two
                # transitional fallbacks removed above: the pin declares a
                # FLOOR, but an installed environment can still lag it (stale
                # venv, editable install pointing elsewhere). `getattr`
                # degrades that mismatch to `None` -> no attribution -> the
                # `unattributed_diagnostic` arm, whereas direct attribute
                # access would raise `AttributeError` mid-extraction. Do not
                # "finish the cleanup" by converting these to direct access.
                "language": getattr(diag, "language", None),
                "code": getattr(diag, "code", None),
            }
        )
        if diag.file is None:
            continue
        diag_path = Path(diag.file)
        if not diag_path.is_absolute():
            diag_path = (root / diag_path).resolve()
        rel = rel_by_resolved.get(str(diag_path))
        if rel is None and diag_path.is_relative_to(root):
            rel = diag_path.relative_to(root).as_posix()
        if rel is None:
            continue
        entry = envelope_by_rel.get(rel)
        if entry is None:
            entry = {"path": rel}
            envelope_by_rel[rel] = entry
        if _is_name_invariant_drop(
            {"code": getattr(diag, "code", None), "message": diag.message}
        ):
            entry.setdefault("name_invariant_drops", []).append(diag.message)
        else:
            entry["error"] = diag.message

    ordered_files = [envelope_by_rel[rel] for rel in rel_by_resolved.values()]

    languages: Dict[str, Any] = {}
    for rel in rel_by_resolved.values():
        extension = Path(rel).suffix
        if extension not in languages:
            languages[extension] = symbol_extract.language_for_extension(extension)

    completeness: Dict[str, Any] = {
        "authority": "project_treesitter",
        "feature_level": "lite",
        "edges": False,
        "cross_file_resolution": False,
        "container_name_resolved": False,
    }
    if unmapped_kinds_total:
        # A `Symbol.kind` present in the extraction that matched none of the
        # class/function/constant/type_alias buckets self-discloses here
        # rather than vanishing (chunk C5) — their vocabulary grows
        # additively, so this is a count of what fell through, never an
        # exact-set assertion of what is allowed. Since the P1 fix (review
        # dc659900), these symbols are NOT dropped from the envelope: each
        # one also lands in its file entry's `other_symbols` list and is
        # counted by `_count_symbols`. This marker's meaning is therefore a
        # per-kind CENSUS of what landed in `other_symbols`, not a report of
        # data loss.
        completeness["unmapped_kinds"] = unmapped_kinds_total

    return {
        "files": ordered_files,
        "diagnostics": diagnostics,
        "languages": languages,
        "completeness": completeness,
    }


def _diagnostic_names_language(diagnostic: Dict[str, Any], language: Any) -> bool:
    """Decide whether ``diagnostic`` names ``language`` — field-only, exact comparison.

    Floor: ``ExtractionDiagnostic.language`` is guaranteed present by the
    pinned ``example-retrieval-repo-symbol-extract`` wheel
    (``60b109cc85b7f1fedc552c178214601040793111``, ``pyproject.toml``) —
    the transitional prose-substring fallback this helper previously carried
    is deleted now that its documented death condition (the pin move) has
    occurred (memo reply 7e6559f1; debt record
    ``state/debt-backlog/2026-08-11-foreign-symbols-substring-language-match-aw.yaml``).

    This is now a pure EXACT comparison of ``diagnostic["language"]`` against
    ``language`` — the prose ``message`` is never consulted. A diagnostic
    whose ``language`` is ``None`` (attribution genuinely unavailable, or a
    lagging/stale install) returns ``False`` for every candidate language —
    intended, loud degradation: such a diagnostic is caught by arm 7's
    ``unattributed_diagnostic`` finding rather than silently misattributed.
    """
    return diagnostic.get("language") == language


_NAME_INVARIANT_DROP_CODE = "symbol_name_invariant"


def _is_name_invariant_drop(diagnostic: Dict[str, Any]) -> bool:
    """Decide whether ``diagnostic`` records a symbol-name-invariant drop.

    Field-only discriminator for upstream's ``ExtractionResult.__post_init__``
    validating choke point (disclosed by example-retrieval-repo-em, memo
    ``cross-repo/inbox/2026-08-11-example-retrieval-repo-em-symbol-name-invariant-now-drops-rows.md``):
    a ``Symbol`` whose ``name`` carries a line terminator or exceeds 1024
    chars is DROPPED from ``symbols`` and an ``error``-level diagnostic is
    appended recording the drop, carrying a non-None ``file``.

    Floor: ``ExtractionDiagnostic.code`` is guaranteed present by the pinned
    ``example-retrieval-repo-symbol-extract`` wheel
    (``60b109cc85b7f1fedc552c178214601040793111``, ``pyproject.toml``), and
    upstream's ``code`` vocabulary — ``grammar_unavailable``, ``parse_failure``,
    ``unreadable_file``, ``symbol_name_invariant`` (their ``DIAGNOSTIC_CODE_*``
    constants) — is declared closed and stable. This is now a pure EXACT
    comparison of ``diagnostic["code"]`` against ``"symbol_name_invariant"`` —
    the prose ``message`` is never consulted, and the transitional
    case-insensitive prefix fallback this helper previously carried is
    deleted now that its documented death condition (the pin move) has
    occurred. An unrecognised ``code`` value (including ``None``) is simply
    not a drop and falls through to ``parse_failure`` in the caller — it does
    not vanish.
    """
    return diagnostic.get("code") == _NAME_INVARIANT_DROP_CODE


def classify_foreign_symbol_coverage(result: Dict[str, Any], census: Dict[str, int]) -> List[Dict[str, Any]]:
    """Five-arm per-extension coverage discriminator.

    Args:
        result: the return of ``build_foreign_symbols``.
        census: extension (e.g. ``".ts"``) -> number of REQUESTED files
            carrying that extension.

    Returns:
        A list of finding dicts, each
        ``{"state": "dependency_absent" | "missing_grammar" |
        "partial_coverage" | "parse_failure" | "name_invariant_drop" |
        "corpus_fact" | "covered" | "unattributed_diagnostic",
        "extension": str | None, "language": str | None, "detail": str}``.

    Arms (never aggregated — a healthy extension never masks a dead one).
    Every extension in ``census`` gets EXACTLY ONE of the corpus_fact /
    missing_grammar / partial_coverage / covered arms below — a caller must
    never have to infer health from an extension's absence from the list
    (review dc659900, P1):
      1. ``result`` carries ``"unavailable"`` -> a single ``dependency_absent``
         finding; loud, distinct, never reclassified as anything else.
      2. Per extension in ``census``: symbols == 0 and (zero files requested,
         or no diagnostic names that extension's language) -> ``corpus_fact``
         (a PASS state — a zero-file extension is never asserted as coverage,
         it is simply not exercised).
      3. Per extension in ``census`` with at least one requested file:
         symbols == 0 and an ``error``-level diagnostic names that
         extension's language -> ``missing_grammar`` (the fail-loud arm).
         "Names" is decided by ``_diagnostic_names_language``: field-
         preferred, prose-fallback (see below).
      4. Per extension in ``census`` with at least one requested file:
         symbols > 0 and an ``error``-level language-level diagnostic
         (``file is None``) names that extension's language ->
         ``partial_coverage`` — a healthy-looking non-zero symbol count
         with files silently dropped underneath it. The finding's
         ``detail`` carries both the symbol count and the diagnostic's own
         message text, so the skipped-file count survives into the
         finding, unchanged in shape by the field-preferred migration below.

      Arms 3, 4, and 7 all route "does this diagnostic name this language"
      through the single module-level helper ``_diagnostic_names_language``,
      so they can never diverge. That helper is now a pure, field-only EXACT
      comparison of the diagnostic's carried ``"language"`` against the
      candidate language — the prose ``message`` is never consulted. The
      fields are guaranteed by the pinned ``example-retrieval-repo-symbol-extract`` wheel
      (``60b109cc85b7f1fedc552c178214601040793111``, ``pyproject.toml``;
      memo reply 7e6559f1; debt record
      ``state/debt-backlog/2026-08-11-foreign-symbols-substring-language-match-aw.yaml``).
      A diagnostic whose ``language`` is ``None`` matches no candidate
      language and so falls to arm 7 (``unattributed_diagnostic``) rather
      than being silently misattributed.
      5. Per extension in ``census`` with at least one requested file:
         symbols > 0 and no diagnostic names that extension's language ->
         ``covered`` — the ordinary successful-extraction case. Emitted
         explicitly rather than by silence, so "checked and healthy" is
         distinguishable from "never examined" (review dc659900, P1).
      6. Per diagnostic carrying a non-None ``file`` -> a ``parse_failure``
         finding for that file, independent of the per-extension arms —
         UNLESS arm 6a below claims it first.
      6a. Per diagnostic carrying a non-None ``file`` for which
         ``_is_name_invariant_drop`` is True -> a ``name_invariant_drop``
         finding for that file INSTEAD of ``parse_failure``. This arm takes
         precedence over arm 6: a diagnostic recording a symbol-name-
         invariant drop is corpus hygiene (one symbol NAME was rejected;
         the file itself parsed cleanly), never both a drop and a parse
         failure for the same diagnostic — the two states are mutually
         exclusive by construction.
      7. Per ``error``-level, file-less diagnostic that names NO language
         present in ``census`` (per ``_diagnostic_names_language``, same
         field-only exact-comparison contract as arms 3/4) -> a single
         ``unattributed_diagnostic`` finding per such diagnostic, so a
         phrasing mismatch between this seam's substring match and
         symbol_extract's diagnostic text can never silently downgrade a
         real grammar failure to a clean bill of health on the affected
         extension (review dc659900, P2). Kept exactly consistent with arms
         3/4 by construction: a diagnostic is "unattributed" iff
         ``_diagnostic_names_language`` is False for EVERY language present
         in ``census``, so a field-matched diagnostic can never be both
         attributed (arm 3/4) and unattributed (arm 7).

    Exit status is never consulted — it is not evidence.
    """
    if "unavailable" in result:
        return [
            {
                "state": "dependency_absent",
                "extension": None,
                "language": None,
                "detail": result["unavailable"],
            }
        ]

    diagnostics = result.get("diagnostics", [])
    files = result.get("files", [])
    languages = result.get("languages")

    symbols_by_extension: Dict[str, int] = {ext: 0 for ext in census}
    for entry in files:
        path = entry.get("path", "")
        suffix = Path(path).suffix
        if suffix in symbols_by_extension:
            symbols_by_extension[suffix] += _count_symbols(entry)

    language_level_diagnostics = [
        diag
        for diag in diagnostics
        if diag.get("level") == "error" and diag.get("file") is None
    ]

    if languages is None:
        try:
            symbol_extract = _import_symbol_extract()
        except ImportError as exc:
            raise ValueError(
                "classify_foreign_symbol_coverage: result carries no "
                "'languages' mapping and symbol_extract could not be "
                f"imported to resolve one ({exc}). Pass a result produced "
                "by build_foreign_symbols, which populates 'languages' at "
                "build time."
            ) from exc

        def _resolve_language(extension: str) -> Any:
            return symbol_extract.language_for_extension(extension)
    else:

        def _resolve_language(extension: str) -> Any:
            return languages.get(extension)

    findings: List[Dict[str, Any]] = []
    for extension, requested_count in census.items():
        language = _resolve_language(extension)
        symbol_count = symbols_by_extension.get(extension, 0)
        language_flagged = (
            requested_count > 0
            and language is not None
            and any(
                _diagnostic_names_language(diag, language)
                for diag in language_level_diagnostics
            )
        )
        if symbol_count == 0 and language_flagged:
            findings.append(
                {
                    "state": "missing_grammar",
                    "extension": extension,
                    "language": language,
                    "detail": f"{requested_count} file(s) of extension {extension!r} "
                    f"({language}) requested but 0 symbols and a grammar-level error diagnostic",
                }
            )
        elif symbol_count > 0 and language_flagged:
            matching_messages = [
                diag.get("message", "")
                for diag in language_level_diagnostics
                if _diagnostic_names_language(diag, language)
            ]
            findings.append(
                {
                    "state": "partial_coverage",
                    "extension": extension,
                    "language": language,
                    "detail": f"{requested_count} file(s) of extension {extension!r} "
                    f"({language}); {symbol_count} symbol(s) extracted but a "
                    "grammar-level error diagnostic names this language: "
                    f"{'; '.join(matching_messages)}",
                }
            )
        elif symbol_count == 0:
            findings.append(
                {
                    "state": "corpus_fact",
                    "extension": extension,
                    "language": language,
                    "detail": f"{requested_count} file(s) of extension {extension!r} "
                    f"({language}); {symbol_count} symbol(s) extracted",
                }
            )
        else:
            # symbol_count > 0 and not language_flagged — the ordinary
            # successful-extraction case. Emitted explicitly rather than by
            # silence (review dc659900, P1): a caller must never have to
            # infer "checked and healthy" from an extension's absence.
            findings.append(
                {
                    "state": "covered",
                    "extension": extension,
                    "language": language,
                    "detail": f"{requested_count} file(s) of extension {extension!r} "
                    f"({language}); {symbol_count} symbol(s) extracted, no "
                    "grammar-level error diagnostic",
                }
            )

    for diag in diagnostics:
        if diag.get("file") is not None:
            findings.append(
                {
                    "state": (
                        "name_invariant_drop"
                        if _is_name_invariant_drop(diag)
                        else "parse_failure"
                    ),
                    "extension": Path(diag["file"]).suffix or None,
                    "language": None,
                    "detail": f"{diag['file']}: {diag.get('message', '')}",
                }
            )

    # An error-level, file-less diagnostic that names no language present in
    # the census must not silently vanish — it would otherwise leave the
    # affected extension's own per-extension arm (above) to fall through to
    # `corpus_fact`/`covered` with no signal at all that a diagnostic fired
    # (review dc659900, P2).
    census_languages = {
        _resolve_language(extension)
        for extension in census
        if _resolve_language(extension) is not None
    }
    for diag in language_level_diagnostics:
        if not any(
            _diagnostic_names_language(diag, language) for language in census_languages
        ):
            findings.append(
                {
                    "state": "unattributed_diagnostic",
                    "extension": None,
                    "language": None,
                    "detail": diag.get("message", ""),
                }
            )

    return findings


def _count_symbols(entry: Dict[str, Any]) -> int:
    """Count every symbol surfaced in one envelope entry.

    Must stay exhaustive over the envelope's symbol-bearing buckets, because
    ``classify_foreign_symbol_coverage`` reads this as "did this extension
    yield anything." Omitting a bucket makes a file whose symbols live only in
    that bucket count as zero, which the discriminator then reports as a
    corpus fact ("0 symbols extracted") or, with a grammar diagnostic present,
    as a spurious missing_grammar. Counting only functions and classes did
    exactly that to `constants` and `type_aliases` the moment chunk C5 began
    surfacing them — 2,268 constants in one real consumer tree.

    A new symbol-bearing bucket added to the envelope MUST be added here too.
    `other_symbols` (review dc659900, P1) is one such bucket: any extracted
    kind matching none of the four named buckets — markdown heading/
    code_fence mapped to SymbolKind.UNKNOWN being the live case at the
    pinned ref — lands there and is counted here rather than being invisible
    to this discriminator.
    """
    count = len(entry.get("functions", []))
    count += len(entry.get("constants", []))
    count += len(entry.get("type_aliases", []))
    count += len(entry.get("other_symbols", []))
    for cls in entry.get("classes", []):
        count += 1 + len(cls.get("methods", []))
    return count

