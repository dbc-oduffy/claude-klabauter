"""coordinator_core.source_edit_gate.selection -- pick the source-edit test tier.

Given the set of files a source edit touched, `select_test_files(repo_root,
edited_files)` picks the subset of a repo's own test files that could possibly
regress: a test file is selected when it (a) imports an edited module (static
scan -- `ast` for Python, a regex import/require scan for TS/JS/TSX/JSX with
relative specifiers resolved against the test file's own directory), or (b)
mentions an edited file's repo-relative path, its basename, or its parent
directory's repo-relative path as a string literal anywhere in its source
(covers hash-pinned vendor headers, line/content-hash allowlists, glob-over-dir
readers, and golden/schema comparisons of an imported model that never
literally `import`s it).

Computed once from the ORIGINAL bytes (i.e. call this before any edit lands,
or against a snapshot of pre-edit content) -- selection answers "what could
this edit regress", which only makes sense against the tree as it stood before
the edit changed import graphs or literal strings out from under it.

Cost: one `os.walk`/`Path.rglob` over the repo's own test files, one read +
one parse/regex pass per test file. No subprocess spawns.

Runner-foreign candidates are excluded, not grouped and refused: a test-
filename-shaped file (e.g. `foo.test.js`) is only selected if it matches its
OWN nearest runner's collection patterns at that runner's root
(`runner.find_runner_root`) -- pytest: `test_*.py`/`*_test.py`; vitest:
`*.test|spec.{ts,tsx,js,jsx,mts,cts}`. A file whose nearest marker resolves to
a DIFFERENT runner (a `.test.js` file whose nearest marker is a pytest
`conftest.py`, no vitest marker anywhere above it) is not selected --
selecting it would land it in that runner's group, which cannot collect it,
and manifest as a false `"indeterminate"` for an edit this file was never
going to verify anyway. Stated blind spot: this checks only the NEAREST
marker: if a second, correct runner exists further up the tree past the
mismatched nearest one, this scan does not search past it to find it. A file
whose nearest walk finds NO marker at all (`find_runner_root` returns
`(None, None)`) is left selected -- that is `gate.py`'s own indeterminate
contract to enforce, not this module's to silently narrow.

Known blind spots (state them, don't hide them):
  - Dynamic imports (`importlib.import_module(f"...")`, `require(someVar)`)
    are invisible to a static scan by construction.
  - A test that reaches an edited module only through a re-export chain (test
    imports package `a`, `a/__init__.py` re-exports from edited `a/b.py`) is
    NOT selected unless the test file itself also imports `a.b` or mentions
    `b.py` -- this scan does not follow re-export graphs.
  - Path-literal matching is string-constant-only, matched as a path segment
    (full path, basename, or parent directory -- never a raw substring of
    arbitrary source text; see `_literal_matches_edited`): a test file that
    builds the same path by string concatenation/f-string interpolation at
    runtime (`base + "/" + name`), or that names a short top-level directory
    alone (a single segment under 6 chars, e.g. `"bin"`), has no literal this
    scan will match against.
  - TS/JS resolution assumes conventional extensions (`.ts`, `.tsx`, `.js`,
    `.jsx`, directory `index.*`); a custom resolver/alias config (webpack
    `alias`, tsconfig `paths`) is not consulted.

Change classification (WHAT changed, not just which file): `classify_edit`
diffs an edited file's original vs edited text. For Python, it compares
`ast.dump` of the parsed original/edited trees (`type_comments=True`, so a
`# type:` comment participates) rather than a positional token diff -- a
structural comparison correctly classifies an ADDED or REMOVED docstring,
which a token-stream zip (same length assumed) cannot. It returns one of
`"comment-only"` (the raw AST dump is identical -- nothing runtime-visible
differs, so only a source-text reader can see it), `"doc-only"` (the AST
dump is identical once every module/class/def docstring is stripped from
both sides -- the first-statement string literal of a module,
class, or def, on BOTH sides of the edit), `"string"` (some other string
literal changed, or a differing token is a docstring on only one side --
the import rule applies, same as `"code"`), or `"code"` (any other token
changed).

A `"comment-only"`/`"doc-only"` edit ALSO narrows its path/basename/dir
literal hit (both language families): the literal counts only when it is
used in a READ context in the test/helper source, never merely mentioned --
execution (`subprocess`/`runpy`/`importlib`/an argv the file is only passed
to as `sys.executable` argv) can't observe a comment or run-time-invisible
docstring, so a literal reaching only an exec call must not select on a
comment-only/doc-only edit alone (`"string"`/`"code"` edits keep the
unconditional literal hit -- some other runtime-visible change may still be
read). Read context: `open(`, `.read_text(`, `.read_bytes(`, `.open(`,
`hashlib.`, `ast.parse(`, `tokenize.`, `linecache.`, `inspect.getsource(`, or
a directory reader (`.glob(`, `.rglob(`, `.iterdir(`, `os.walk(`,
`os.listdir(`, `.scandir(`) -- see `_READ_CONTEXT_MARKERS`. For Python,
checked per function scope (`_literal_in_read_context`: a FunctionDef/
AsyncFunctionDef's own line range, plus a module-level scope of every line
NOT covered by a function -- a stated conservative approximation, not a
real data-flow trace: a read-marker call and the literal merely sharing a
scope counts, whether or not the literal actually flows into that call).
For the non-Python family (no local parser), the same markers are checked
against the whole file -- coarser still, same stated direction (a false
positive over-selects, never silently narrows).

`"doc-only"` gets a narrower selection than `"string"`/`"code"`: a
docstring is runtime-invisible except through code that actually reads it,
so plain importers of the edited module are not selected on a docstring
edit alone. `select_test_files` selects, for a `"doc-only"` edit: (a) every
literal reader (path/basename/dir string match, same as `"comment-only"`,
unconditional); (b) every importer, IF the edited file itself reads its own
docstrings at runtime -- `_DOC_SELF_READER_MARKERS` (`__doc__`,
`inspect.getdoc`/`getdoc(`, an argparse/click/typer CLI, a pydantic
`BaseModel`/`@dataclass`/`Field` whose class docstring becomes a
JSON-schema description) -- checked against the file's own (edited) source,
heuristically (a false positive over-selects, the safe direction; a false
negative under-selects and is a stated blind spot); (c) an importer whose
OWN source references a doc reader -- `_DOC_TEST_READER_MARKERS` (`__doc__`,
`getdoc(`, `help(`, `--help`, `json_schema`, `.schema(`, `description`,
`snapshot`/`golden`) -- even when the edited module itself does not match
(b), since the test may pull the docstring through `inspect.getdoc` on the
imported object itself. Stated blind spot: a docstring reached only through
a third-party library not named in either marker list (an unlisted CLI
framework, ORM, or doc-generator) is invisible to both checks and
under-selects silently.

For the JS/TS family, there is no
token-level lexer (a hand-rolled comment/regex-literal lexer is unsound by
construction -- see `_classify_js_edit`'s own docstring) -- it applies a
conservative line rule instead and returns only `"comment-only"` or
`"code"`: every changed line (both sides of a `difflib` line-diff) must,
after stripping whitespace, be empty or start with `//`, `/*`, `*`, or `*/`,
and none of those changed lines may carry a semantic comment (a directive a
tool reads, e.g. `// @ts-expect-error`, `/// <reference`, an
`eslint-disable`/`eslint-enable` line, a `//# sourceMappingURL` pragma, a
`/* webpack` magic comment, `@jsx`, `/* istanbul`, or `prettier-ignore`) --
any other changed line, including a trailing comment on an otherwise-code
line, classifies as `"code"` (the safe direction). Both language paths fall
back to `"code"` -- the conservative default -- and it is also what every
caller gets when it supplies no before/after text at all. A
`"comment-only"`-classified edited file is selected by path/basename/dir
literal only; a `"doc-only"`-classified one gets the narrower selection
described above. `select_test_files`'s `file_texts` parameter (`path ->
(original_text, edited_text)`) is how a caller opts into either -- omitted,
every edited file classifies as `"code"` (the pre-existing, import-rule-
applying behaviour).

Spec backlink: docs/plans/2026-09-27-source-edit-test-guardrail.md (gate
redesign -- computed tier, no per-repo declaration).
"""

from __future__ import annotations

import ast
import copy
import difflib
import io
import posixpath
import re
import tokenize
from pathlib import Path

from .runner import find_runner_root

__all__ = ["select_test_files", "classify_edit"]

_TS_TEST_SUFFIXES = (
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.mts", ".test.cts",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", ".spec.mts", ".spec.cts",
)

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[^'"]*?\s+from\s+)?|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]"""
)

# Heuristic markers for `"doc-only"` selection (see module docstring): a
# false positive over-selects (safe direction), a false negative is a stated
# blind spot. Case-sensitive by design -- `description`/`schema`/`snapshot`
# as bare lowercase identifiers are common enough that a case-insensitive
# match on the self-reader side would swamp selection with unrelated hits;
# the test-reader side stays case-insensitive since it only gates an
# already-import-scanned test.
_DOC_SELF_READER_MARKERS = re.compile(
    r"__doc__|inspect\.getdoc|getdoc\(|argparse\.ArgumentParser|click\.|typer\.|"
    r"BaseModel|@dataclass|pydantic\.Field|\.Field\("
)
_DOC_TEST_READER_MARKERS = re.compile(
    r"__doc__|getdoc\(|help\(|--help|json_schema|\.schema\(|description|snapshot|golden",
    re.IGNORECASE,
)


def _is_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name
    if name.endswith(_TS_TEST_SUFFIXES):
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    return False


def _module_suffixes(edited_rel: str) -> set:
    """Every dotted-name suffix of a `.py` file's repo-relative path, dropping
    leading path components one at a time, and dropping a trailing
    `__init__` -- so `a/b/__init__.py` yields `{"a.b", "b"}` and
    `a/b/c.py` yields `{"a.b.c", "b.c", "c"}`."""
    parts = list(Path(edited_rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()
    suffixes = set()
    for i in range(len(parts)):
        suffixes.add(".".join(parts[i:]))
    return suffixes


def _python_imports(source: str) -> set:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def _suffixes_for(edited_py_files: list) -> set:
    """Union of `_module_suffixes` across every file in `edited_py_files`,
    computed once by the caller (see `select_test_files`) rather than
    per-test-file -- `_module_suffixes` depends only on the edited path, not
    on which test file is being checked, so recomputing it inside the
    per-test-file scan is pure waste over a large test corpus."""
    suffixes = set()
    for edited_rel in edited_py_files:
        suffixes.update(_module_suffixes(edited_rel))
    return suffixes


def _leaf_tokens(suffixes: set) -> frozenset:
    """The rightmost dotted component of every suffix (`"a.b.c"` -> `"c"`) --
    every valid `import`/`from ... import` statement that could match one of
    these suffixes necessarily contains its leaf component as a literal
    substring of the source text (the parser has to see the name to bind
    it), so a source with none of these substrings present cannot possibly
    import any of the edited modules and a full `ast.parse` on it is
    provably wasted work. Used only to BUILD the import-context prefilter
    regex (see `_build_import_prefilter_regex`), never as a raw substring
    test itself -- a bare substring check (the predecessor of this function)
    matches a common English leaf name (`derive`, `stamp`) inside ordinary
    prose/identifiers across most of a large test corpus, forcing `ast.parse`
    on every one of those files for nothing."""
    return frozenset(s.rsplit(".", 1)[-1] for s in suffixes)


def _build_import_prefilter_regex(suffixes: set) -> re.Pattern | None:
    """One combined regex (never one-regex-per-leaf) that matches a leaf
    token only when it appears in actual import-statement CONTEXT --
    `import ...leaf`, `from ... import (...leaf...)` (parenthesised,
    multi-line -- `re.DOTALL`), `from ...leaf` (leaf named as the imported
    module itself, including a relative `from .leaf import x` or
    `from ..pkg.leaf import x`), or a dynamic
    `importlib.import_module("...leaf...")` / `__import__("...leaf...")`
    string. Root fix for the `derive`/`stamp` leaf-token false-positive
    class: the old prefilter (`_leaf_tokens` used as a raw substring test)
    matched those names anywhere in a file's prose/identifiers, forcing a
    wasted `ast.parse` on every one of ~1000+ files that merely mentioned the
    word. This regex is still only a PREFILTER -- a match here means "worth
    an `ast.parse` to confirm", never the hit decision itself; the real
    `_python_imports`/`_import_hit_against_suffixes` check downstream is
    what confirms a genuine import. Returns `None` when there are no leaves
    to look for (the caller then always parses, preserving the pre-existing
    behaviour rather than silently narrowing)."""
    leaves = _leaf_tokens(suffixes)
    if not leaves:
        return None
    alts = []
    for leaf in sorted(leaves):
        esc = re.escape(leaf)
        alts.append(rf"import\s+[\w.]*{esc}\b(?:\s+as\s+\w+)?")
        alts.append(rf"from\s+[\w.]+\s+import\s*\([^)]{{0,4000}}?\b{esc}\b[^)]{{0,4000}}?\)")
        alts.append(rf"from\s+[\w.]+\s+import\s+(?!\()[^\n(]{{0,1000}}?\b{esc}\b")
        alts.append(rf"from\s+[\w.]*{esc}\b")
        alts.append(rf"import_module\s*\(\s*['\"][^'\"]*\b{esc}\b[^'\"]*['\"]")
        alts.append(rf"__import__\s*\(\s*['\"][^'\"]*\b{esc}\b[^'\"]*['\"]")
    pattern = r"\b(?:" + "|".join(alts) + ")"
    return re.compile(pattern, re.MULTILINE | re.DOTALL)


def _import_hit_against_suffixes(imported: set, suffixes: set) -> bool:
    if not imported or not suffixes:
        return False
    for suffix in suffixes:
        for mod in imported:
            if mod == suffix or mod.startswith(suffix + "."):
                return True
    return False


def _python_import_hit(test_source: str, edited_py_files: list) -> bool:
    """Convenience wrapper kept for callers (and tests) that pass a raw file
    list rather than a precomputed suffix set -- `select_test_files` itself
    uses `_import_hit_against_suffixes` directly with a suffix set computed
    once per call (see `_suffixes_for`)."""
    imported = _python_imports(test_source)
    return _import_hit_against_suffixes(imported, _suffixes_for(edited_py_files))


def _resolve_js_relative(test_rel: str, specifier: str) -> set:
    """Resolves a relative specifier against the test file's own directory,
    returning every plausible repo-relative extension variant."""
    if not (specifier.startswith("./") or specifier.startswith("../")):
        return set()
    joined = posixpath.join(Path(test_rel).parent.as_posix(), specifier)
    base = posixpath.normpath(joined)
    candidates = {base}
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        candidates.add(base + ext)
        candidates.add(posixpath.join(base, "index" + ext))
    return candidates


def _js_import_hit(test_rel: str, test_source: str, edited_js_files: set) -> bool:
    for specifier in _JS_IMPORT_RE.findall(test_source):
        for candidate in _resolve_js_relative(test_rel, specifier):
            if candidate in edited_js_files:
                return True
    return False


_STRING_LITERAL_RE = re.compile(r"""(['"`])((?:(?!\1)[^\\]|\\.)*)\1""")


def _generic_string_literals(source: str) -> set:
    """Conservative quoted/template-string-content extraction for the JS/TS
    family (and any other non-Python source): every run of text between a
    matching pair of `'`, `"`, or `` ` `` quotes, escapes respected. Not a
    real lexer -- a quote inside a regex literal or an already-malformed
    file can mismatch -- but unlike the raw substring scan it replaced, a
    false EXTRA literal here still has to pass the segment-equality rule in
    `_literal_matches_edited` before it can select anything, so a stray
    mismatch over-selects at worst, never silently misses on its own."""
    return {m.group(2) for m in _STRING_LITERAL_RE.finditer(source)}


def _div_chain_strings(node) -> list | None:
    """Resolves a `Path(...) / "a" / "b"` `BinOp` chain (left-associative
    `/` on `ast.BinOp`/`ast.Div`) into its ordered string segments, or
    `None` if any operand is not a plain string constant or a single-arg
    `Path("...")` call -- e.g. a variable or f-string in the chain makes it
    unresolvable statically, same blind spot as any other dynamic path
    build (see module docstring)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _div_chain_strings(node.left)
        right = _div_chain_strings(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Path"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return [node.args[0].value]
    return None


def _python_string_literals(tree: ast.AST) -> set:
    """Every string `Constant` value in the tree, plus every resolvable
    `Path(...) / "a" / "b"` join chain reassembled into one `"a/b"`-shaped
    string (see `_div_chain_strings`) -- the AST-native replacement for the
    old raw-substring-over-source-text scan: a literal is now something the
    parser actually recognises as a string value, not any text that happens
    to appear in the file (a prose word containing a directory name as a
    sub-string, an identifier, a docstring sentence)."""
    literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            segs = _div_chain_strings(node)
            if segs:
                literals.add("/".join(segs))
    return literals


_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
_FSTRING_IGNORED = {tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
_STRING_PREFIX_QUOTE_RE = re.compile(r'^([A-Za-z]*)(\'\'\'|"""|\'|")')
_FSTRING_FIELD_RE = re.compile(r"\{[^{}]*\}")


def _string_token_literal_segments(text: str) -> list:
    """The literal (non-interpolated) content of one Python STRING token's
    raw source text (quotes and prefix included, as `tokenize` hands it
    over). A plain string literal yields exactly one segment -- its value,
    unescaped. An f-string yields zero or more segments: the text between
    its `{...}` replacement fields, conservatively -- a nested brace inside
    a replacement field (`f"{d['a']}"`) is not resolved correctly by this
    non-nested split, same stated blind spot as any other dynamic path
    build (see module docstring); it only has to find the literal segments
    that ARE plain text, never resolve the expression parts.

    Fast path: a quoted body containing no backslash is exactly its own
    value (Python string escaping only ever triggers on `\\`), so it is
    returned by direct slicing -- `ast.literal_eval` (which `compile()`s a
    throwaway expression per call) is reserved for the escape-bearing
    minority, the dominant residual cost of a naive per-token
    `literal_eval` over a large test corpus."""
    m = _STRING_PREFIX_QUOTE_RE.match(text)
    if not m:
        return []
    prefix = m.group(1).lower()
    quote = m.group(2)
    if "b" in prefix:
        return []
    inner = text[len(m.group(1)) + len(quote):]
    if inner.endswith(quote):
        inner = inner[: -len(quote)]
    if "f" not in prefix:
        if "\\" not in inner:
            return [inner]
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return []
        return [value] if isinstance(value, str) else []
    segments = []
    for seg in _FSTRING_FIELD_RE.split(inner):
        seg = seg.replace("{{", "{").replace("}}", "}")
        if seg:
            segments.append(seg)
    return segments


def _python_literals_via_tokenize(source: str) -> set | None:
    """Python string-literal extraction using the stdlib `tokenize` module's
    STRING tokens instead of a full `ast.parse` -- the speed fix for the
    field measurement's second cost (an `ast.parse`+walk on every
    prefilter-positive test file): `tokenize` alone is materially cheaper
    than a full parse, and every literal this function needs to find is
    directly visible at the token level.

    Reconstructs a `Path(...) / "a" / "b"`-shaped join chain from the raw
    token sequence (a `STRING`/`Path("...")` "atom", followed by one or more
    `OP('/')` + atom pairs) instead of walking a `BinOp`/`ast.Div` tree --
    same joined-with-`"/"` result as the old AST-based `_div_chain_strings`.
    Every individual string-literal token (chained or not) is also added on
    its own, matching the old `ast.Constant`-walk behaviour of capturing
    every string constant in the file regardless of whether it participates
    in a join chain.

    Returns `None` (never an empty set) on a token stream the tokenizer
    itself cannot get through (`tokenize.TokenError`/`IndentationError`) --
    the caller's cue to report "no literals", the same outcome the old
    `ast.parse`+`SyntaxError` path produced."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None

    sig = [t for t in tokens if t.type not in _FSTRING_IGNORED]
    literals = set()

    if _FSTRING_MIDDLE is not None:
        for tok in sig:
            if tok.type == _FSTRING_MIDDLE and tok.string:
                literals.add(tok.string)

    n = len(sig)

    def _atom_at(i: int):
        if i >= n:
            return None
        tok = sig[i]
        if tok.type == tokenize.NAME and tok.string == "Path" and i + 3 < n:
            if (
                sig[i + 1].type == tokenize.OP and sig[i + 1].string == "("
                and sig[i + 2].type == tokenize.STRING
                and sig[i + 3].type == tokenize.OP and sig[i + 3].string == ")"
            ):
                segs = _string_token_literal_segments(sig[i + 2].string)
                if len(segs) == 1:
                    literals.add(segs[0])
                    return segs[0], i + 4
            return None
        if tok.type == tokenize.STRING:
            segs = _string_token_literal_segments(tok.string)
            for seg in segs:
                literals.add(seg)
            if len(segs) == 1:
                return segs[0], i + 1
            return None
        return None

    i = 0
    while i < n:
        result = _atom_at(i)
        if result is None:
            i += 1
            continue
        first_val, next_i = result
        chain = [first_val]
        j = next_i
        while j < n and sig[j].type == tokenize.OP and sig[j].string == "/":
            nxt = _atom_at(j + 1)
            if nxt is None:
                break
            val2, next_j = nxt
            chain.append(val2)
            j = next_j
        if len(chain) >= 2:
            literals.add("/".join(chain))
        i = next_i

    return literals


def _extract_literals(source: str, rel_path: str) -> set:
    """String-constant literals for `rel_path`'s source, keyed off its own
    suffix -- `tokenize` STRING tokens (Python, see
    `_python_literals_via_tokenize`) or a conservative quoted/template-
    string regex (everything else, notably JS/TS). Computed once per test
    file by the caller (never once per edited file) -- see
    `select_test_files` for why the old per-edited-file substring scan,
    which also re-lowercased the FULL source text on every one of those
    calls, was the dominant cost over a large test corpus."""
    if rel_path.endswith(".py"):
        literals = _python_literals_via_tokenize(source)
        return literals if literals is not None else set()
    return _generic_string_literals(source)


def _literal_matches_edited(
    literal: str, edited_rel: str, include_dir: bool = True, include_basename: bool = True,
) -> bool:
    """A string-constant literal counts as "reading" `edited_rel` only as a
    PATH SEGMENT match, never a raw substring: this is the root fix for the
    field measurement (a `bin`/`ops`-shaped short directory needle matching
    inside ordinary prose words, since the old rule was substring-anywhere-
    in-source-text). Normalises `\\` -> `/` and strips a leading `./` on the
    literal before comparing (a literal is compared as written; the edited
    path is already repo-relative forward-slash).

    - **Full path**: `literal == edited_rel` or `literal` ends with
      `"/" + edited_rel` (a literal carrying an extra prefix segment, e.g.
      an absolute-feeling `"src/pkg/mod.py"` mention of a repo-relative
      `"pkg/mod.py"` file).
    - **Basename** (`include_basename`): same, against the bare filename --
      gated on the basename containing a `.` or being >= 6 chars, so a
      short extensionless name (rare, but possible for a script/binary)
      cannot alone trigger a hit; in practice almost every source basename
      carries an extension and so qualifies, which is the intended
      permissiveness -- a bare filename is a much more specific needle than
      a bare directory name.
    - **Directory** (`include_dir`): `literal == parent`, `literal ==
      parent + "/"`, or `literal` starts with `parent + "/"` AND the
      remainder contains a glob metacharacter (`*`, `?`, `[`) -- gated on
      the parent having >= 2 path segments or being >= 6 chars. A literal
      naming a specific SIBLING file under `parent` (no glob metachar in the
      remainder) does not count as reading `edited_rel` -- that file is not
      `edited_rel`, and the earlier `startswith(parent + "/")`-without-a-
      glob-check rule counted every sibling-file mention as a hit, which is
      the field-measurement over-selection this rule fixes. The stated
      blind spot is a test globbing a short top-level directory by that
      bare name alone, which this rule deliberately treats as too ambiguous
      to count as "reading" the file.
    """
    if not literal:
        return False
    lit = literal.replace("\\", "/")
    if lit.startswith("./"):
        lit = lit[2:]
    if not lit:
        return False

    edited_path = Path(edited_rel)
    basename = edited_path.name
    parent = edited_path.parent.as_posix()

    def _eq_or_bounded(candidate: str) -> bool:
        return lit == candidate or lit.endswith("/" + candidate)

    if _eq_or_bounded(edited_rel):
        return True

    if include_basename and (len(basename) >= 6 or "." in basename):
        if _eq_or_bounded(basename):
            return True

    if include_dir and parent and parent != ".":
        dir_ok = ("/" in parent) or (len(parent) >= 6)
        if dir_ok:
            if lit == parent or lit == parent + "/":
                return True
            if lit.startswith(parent + "/"):
                remainder = lit[len(parent) + 1:]
                if any(ch in remainder for ch in "*?["):
                    return True

    return False


def _literal_prefilter_tokens(edited_files: list) -> frozenset:
    """Cheap skip-the-parse tokens for the literal scan, analogous to
    `_leaf_tokens` for the import scan: every edited file's bare basename
    plus its parent directory's full repo-relative form and bare last
    segment. Any literal that could possibly match `_literal_matches_edited`
    necessarily contains one of these as raw substring text (a full-path or
    basename match contains the basename; a directory match contains at
    least the directory's own last segment) -- so a source containing NONE
    of them as raw text cannot possibly produce a literal hit against any
    edited file, and `_extract_literals`'s `ast.parse`/regex pass is
    provably wasted work on it. Used only to skip extraction, never as the
    hit decision itself."""
    tokens = set()
    for f in edited_files:
        p = Path(f)
        tokens.add(p.name)
        parent = p.parent.as_posix()
        # Only add the parent as a prefilter token when it could actually
        # satisfy `_literal_matches_edited`'s `dir_ok` gate (>= 2 segments or
        # >= 6 chars) -- a single short top-level segment (e.g. "bin") can
        # never itself produce a dir hit, so admitting it here would only
        # cost every subsequent file a wasted `ast.parse`/regex pass for a
        # token common enough to appear in unrelated prose constantly.
        if parent and parent != "." and (("/" in parent) or len(parent) >= 6):
            tokens.add(parent)
    return frozenset(t for t in tokens if t)


def _build_literal_prefilter_regex(tokens: frozenset) -> re.Pattern | None:
    """Boundary-tightened replacement for a raw `tok in source` scan over
    `_literal_prefilter_tokens`: a bare substring test still counts a dir
    token like `"coordinator/bin"` as present inside an unrelated longer
    segment (`"coordinator/bin_helpers"`), which can never pass the real
    `_meta_matches_literal` segment-equality check downstream -- so on a
    repo where a token is a common path PREFIX, the raw-substring prefilter
    was itself forcing a wasted `ast.parse`/regex `_extract_literals` pass
    on every file containing that longer, unrelated segment. Requiring a
    non-identifier/non-path character (or start/end of file) on both sides
    of the token rules those out while still catching every string that
    could actually satisfy `_meta_matches_literal` (which only ever compares
    on `/`-bounded segments). One combined regex, case-insensitive, built
    once per `select_test_files` call."""
    if not tokens:
        return None
    alts = sorted((re.escape(t) for t in tokens), key=len, reverse=True)
    pattern = r"(?<![\w.-])(?:" + "|".join(alts) + r")(?![\w.-])"
    return re.compile(pattern, re.IGNORECASE)


def _edited_literal_meta(edited_files: list) -> list:
    """Precomputes, ONCE PER `select_test_files` CALL (never once per
    literal), the `(edited_rel, edited_rel_lower, basename, basename_ok,
    parent, parent_slash, dir_ok)` tuple `_literal_matches_edited` needs for
    every edited file -- `Path(...)`/`.name`/`.parent.as_posix()` are cheap
    once, but the literal scan calls into this data on the order of
    `len(literals) x len(edited_files)` times per test file (tens to
    hundreds of thousands over a large corpus), so recomputing them inside
    that inner loop (the field measurement's residual cost after the
    substring-vs-string-constant fix) was still the dominant remaining
    cost."""
    meta = []
    for edited_rel in edited_files:
        edited_path = Path(edited_rel)
        basename = edited_path.name
        basename_ok = len(basename) >= 6 or "." in basename
        parent = edited_path.parent.as_posix()
        if parent == ".":
            parent = ""
        dir_ok = bool(parent) and (("/" in parent) or len(parent) >= 6)
        meta.append((
            edited_rel, edited_rel.lower(), basename, basename_ok, parent, dir_ok,
        ))
    return meta


def _meta_matches_literal(lit: str, meta_entry: tuple, include_dir: bool, include_basename: bool) -> bool:
    """Same rule as `_literal_matches_edited`, against a precomputed
    `_edited_literal_meta` entry instead of a raw `edited_rel` -- no
    `Path()` construction, no nested-closure call, per comparison."""
    edited_rel, _edited_rel_lower, basename, basename_ok, parent, dir_ok = meta_entry

    if lit == edited_rel or lit.endswith("/" + edited_rel):
        return True

    if include_basename and basename_ok and (lit == basename or lit.endswith("/" + basename)):
        return True

    if include_dir and dir_ok:
        if lit == parent or lit == parent + "/":
            return True
        if lit.startswith(parent + "/"):
            remainder = lit[len(parent) + 1:]
            if any(ch in remainder for ch in "*?["):
                return True

    return False


def _literal_matches_edited(
    literal: str, edited_rel: str, include_dir: bool = True, include_basename: bool = True,
) -> bool:
    """Single-edited-file convenience wrapper kept for direct callers/tests
    -- `_path_literal_hit` itself uses `_edited_literal_meta` +
    `_meta_matches_literal` directly, precomputed once per
    `select_test_files` call (see that function)."""
    if not literal:
        return False
    lit = literal.replace("\\", "/")
    if lit.startswith("./"):
        lit = lit[2:]
    if not lit:
        return False
    meta_entry = _edited_literal_meta([edited_rel])[0]
    return _meta_matches_literal(lit, meta_entry, include_dir, include_basename)


def _path_literal_hit(
    literals: set, edited_meta: list, include_dir: bool = True, include_basename: bool = True,
) -> bool:
    """`literals` is the PRECOMPUTED (see `_extract_literals`) set of string
    constants found in a test file's own source -- extracted once per test
    file by the caller, not once per (edited file, test file) pair.
    `edited_meta` is the PRECOMPUTED (see `_edited_literal_meta`) per-
    edited-file metadata, computed once per `select_test_files` call rather
    than once per literal. Case-insensitive fallback kept for a literal
    that differs only in case (a live miss on a case-insensitive filesystem
    -- Windows, default macOS) -- applied per-literal, never as a whole-
    source lowercase pass, since `literals` is already the small extracted
    set, not the full source text."""
    for literal in literals:
        if not literal:
            continue
        lit = literal.replace("\\", "/")
        if lit.startswith("./"):
            lit = lit[2:]
        if not lit:
            continue
        lit_lower = lit.lower()
        case_sensitive_only = lit_lower == lit
        for meta_entry in edited_meta:
            if _meta_matches_literal(lit, meta_entry, include_dir, include_basename):
                return True
            if not case_sensitive_only:
                lowered_meta = (
                    meta_entry[1], None, meta_entry[2].lower(), meta_entry[3],
                    meta_entry[4].lower(), meta_entry[5],
                )
                if _meta_matches_literal(lit_lower, lowered_meta, include_dir, include_basename):
                    return True
    return False


_READ_CONTEXT_MARKERS = re.compile(
    r"\bopen\s*\(|\.read_text\s*\(|\.read_bytes\s*\(|\.open\s*\(|\bhashlib\.|"
    r"\bast\.parse\s*\(|\btokenize\.|\blinecache\.|inspect\.getsource\s*\(|"
    r"\.glob\s*\(|\.rglob\s*\(|\.iterdir\s*\(|\bos\.walk\s*\(|\bos\.listdir\s*\(|"
    r"\.scandir\s*\("
)


def _literal_in_read_context(source: str, literal: str) -> bool:
    """Conservative, documented-blind-spot approximation (see module
    docstring) for "does `literal` flow into a read call in `source`":
    Python-only (caller falls back to a whole-file check for other
    languages) -- true when some FunctionDef/AsyncFunctionDef's own line
    range (nested functions included, over-inclusive by construction), or
    the module-level lines left over once every function's range is
    excluded, contains BOTH a `_READ_CONTEXT_MARKERS` match and `literal` as
    raw text. Not a real data-flow trace: the read call and the literal
    merely sharing a scope is enough, whichever direction it flows.
    Unparseable `source` -> `False` (the caller's literal-hit path already
    handled the unconditional/ungated case; this is only the gate for
    comment-only/doc-only edits)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    lines = source.splitlines()
    total = len(lines)
    func_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            func_ranges.append((node.lineno, end))

    for start, end in func_ranges:
        segment = "\n".join(lines[start - 1:end])
        if literal in segment and _READ_CONTEXT_MARKERS.search(segment):
            return True

    covered = set()
    for start, end in func_ranges:
        covered.update(range(start, end + 1))
    module_lines = [lines[i - 1] for i in range(1, total + 1) if i not in covered]
    module_segment = "\n".join(module_lines)
    if literal in module_segment and _READ_CONTEXT_MARKERS.search(module_segment):
        return True
    return False


def _normalize_literal(literal: str) -> str:
    lit = literal.replace("\\", "/")
    if lit.startswith("./"):
        lit = lit[2:]
    return lit


def _gated_literal_hit(
    source: str, rel_path: str, literals: set, edited_meta_gated: list,
    include_dir: bool, include_basename: bool,
) -> bool:
    """The comment-only/doc-only literal gate (see module docstring): a
    literal that segment-matches one of `edited_meta_gated`'s entries counts
    only when it is also used in a read context. `edited_meta_gated` is
    already restricted (by the caller, `select_test_files`) to edited files
    classified `"comment-only"`/`"doc-only"` -- `"string"`/`"code"` edits
    never reach this function; their literal hit stays unconditional via
    `_path_literal_hit`."""
    if not edited_meta_gated or not literals:
        return False
    if not _READ_CONTEXT_MARKERS.search(source):
        return False
    matched_literals = set()
    for literal in literals:
        if not literal:
            continue
        lit = _normalize_literal(literal)
        if not lit:
            continue
        for meta_entry in edited_meta_gated:
            if _meta_matches_literal(lit, meta_entry, include_dir, include_basename):
                matched_literals.add(literal)
                break
    if not matched_literals:
        return False
    is_py = rel_path.endswith(".py")
    for literal in matched_literals:
        if is_py:
            if _literal_in_read_context(source, literal):
                return True
        else:
            # No local parser for the non-Python family -- a whole-file
            # read-marker match (already confirmed above) plus the literal
            # being present in `source` at all (guaranteed, since `literals`
            # was extracted from this very source) is the stated coarser
            # fallback.
            return True
    return False


def _matches_runner_patterns(rel_path: str, runner: str) -> bool:
    """Would `runner` actually collect `rel_path` as a test file, per its own
    conventional collection patterns (see module docstring)?"""
    name = Path(rel_path).name
    if runner == "pytest":
        return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")
    if runner == "vitest":
        return name.endswith(_TS_TEST_SUFFIXES)
    return False


def _runner_collectible(repo_root: str, test_rel: str) -> bool:
    """False only when a runner IS detected at `test_rel`'s nearest marker
    and that runner's own patterns would not collect it (a runner-foreign
    file). No marker found anywhere -> True, deferring to `gate.py`'s own
    indeterminate contract rather than silently narrowing selection here."""
    runner, _root = find_runner_root(repo_root, test_rel)
    if runner is None:
        return True
    return _matches_runner_patterns(test_rel, runner)


_JS_SEMANTIC_COMMENT_MARKERS = (
    "// @ts-",
    "/// <reference",
    "eslint-disable",
    "eslint-enable",
    "//# sourcemappingurl",
    "/* webpack",
    "@jsx",
    "/* istanbul",
    "prettier-ignore",
)


def _js_changed_lines(original: str, edited: str) -> list:
    """Every line touched by an original->edited edit, both the removed and
    the added side, via a line-level `difflib` diff -- an unchanged
    (`"  "`-prefixed) or hint (`"? "`-prefixed) line is not "changed"."""
    changed = []
    for line in difflib.ndiff(original.splitlines(), edited.splitlines()):
        if line[:2] in ("+ ", "- "):
            changed.append(line[2:])
    return changed


def _classify_js_edit(original: str, edited: str) -> str:
    """Conservative line rule for the JS/TS family: no token-level lexer,
    because a hand-rolled comment/string/regex-literal lexer for this
    family is unsound by construction (a regex literal containing an
    internal `//`, e.g. `/\\/\\//`, is indistinguishable from a line
    comment without a real parser -- see `comment_strip`'s own TS-AST
    prover, which is what actually owns correctness here). Instead: a JS/TS
    edit classifies `"comment-only"` iff every changed line (see
    `_js_changed_lines`), after stripping whitespace, is empty or starts
    with `//`, `/*`, `*`, or `*/` -- and none of those changed lines carries
    a semantic comment a tool reads (`_JS_SEMANTIC_COMMENT_MARKERS`).
    Anything else, including a trailing comment appended to an otherwise
    unchanged code line, classifies `"code"` -- the safe direction, since a
    code-shaped line can hide a regex literal, template expression, or any
    other runtime-visible change this rule cannot see past."""
    for line in _js_changed_lines(original, edited):
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in _JS_SEMANTIC_COMMENT_MARKERS):
            return "code"
        if not stripped.startswith(("//", "/*", "*", "*/")):
            return "code"
    return "comment-only"


_HASH_SEMANTIC_COMMENT_MARKERS = (
    "#!",
    "# -*- coding",
    "#requires",
    "# shellcheck",
    "# yaml-language-server",
)

# Suffixes classified via the conservative hash-comment line rule (see
# `_classify_hash_comment_edit`) -- everything using `#` as its sole comment
# marker and lacking a local structural parser here, same treatment JS/TS
# gets for `//`. `.dockerfile`/bare `Dockerfile` is matched by filename, not
# suffix (see `classify_edit`), so it is not listed here.
_HASH_COMMENT_SUFFIXES = (
    ".ps1", ".psm1", ".sh", ".bash", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".r",
)

# Suffixes (or the bare `Dockerfile` name, handled separately) whose syntax
# can hide a changed line inside a construct a line-by-line `#`-prefix check
# cannot see past: a PowerShell `<# ... #>` block comment or `@'...'@`/
# `@"..."@` here-string can contain a line that LOOKS like code (or even
# `#`-prefixed text with no comment meaning) while a bash/dockerfile heredoc
# body can contain arbitrary code-shaped text under a comment-free banner
# line. `_hash_special_lines` computes, for a given side's full text, every
# line number inside such a span; any CHANGED line landing in that set
# forces `"code"` regardless of its own `#`-prefix shape.
_PS1_SPECIAL_SUFFIXES = (".ps1", ".psm1")
_SHELL_HEREDOC_SUFFIXES = (".sh", ".bash")

_PS1_BLOCK_COMMENT_RE = re.compile(r"<#.*?#>", re.DOTALL)
_PS1_HERESTRING_SQ_RE = re.compile(r"@'.*?'@", re.DOTALL)
_PS1_HERESTRING_DQ_RE = re.compile(r'@".*?"@', re.DOTALL)
_SHELL_HEREDOC_START_RE = re.compile(r"<<-?\s*([\"']?)(\w+)\1")


def _ps1_special_lines(text: str) -> set:
    """Line numbers (1-indexed) inside a PowerShell `<# ... #>` block comment
    or `@'...'@`/`@"..."@` here-string -- see `_PS1_SPECIAL_SUFFIXES` above.
    Not anchored to line-start for the here-string markers (a true PowerShell
    here-string requires `@'`/`@"` to be the last token on its opening line
    and the closing `'@`/`"@` to be the first token on its own line); this is
    a deliberately looser match -- over-matching only means more lines are
    conservatively treated as `"code"`, never fewer."""
    special = set()
    for pattern in (_PS1_BLOCK_COMMENT_RE, _PS1_HERESTRING_SQ_RE, _PS1_HERESTRING_DQ_RE):
        for m in pattern.finditer(text):
            start_line = text.count("\n", 0, m.start()) + 1
            end_line = text.count("\n", 0, m.end()) + 1
            special.update(range(start_line, end_line + 1))
    return special


def _shell_heredoc_lines(text: str) -> set:
    """Line numbers (1-indexed) inside a bash/dockerfile heredoc body
    (`<<EOF ... EOF`, `<<'EOF' ... EOF`, `<<-EOF ... EOF` with an indented
    terminator) -- from the `<<...` opening line through its terminator
    line, inclusive. An unterminated heredoc (malformed/truncated source)
    conservatively spans to end-of-file rather than matching nothing."""
    lines = text.splitlines()
    special = set()
    n = len(lines)
    i = 0
    while i < n:
        m = _SHELL_HEREDOC_START_RE.search(lines[i])
        if m is None:
            i += 1
            continue
        delim = m.group(2)
        allow_indent = "<<-" in lines[i]
        start = i
        j = i + 1
        while j < n:
            candidate = lines[j].strip() if allow_indent else lines[j]
            if candidate == delim:
                break
            j += 1
        end = j if j < n else n - 1
        for k in range(start, end + 1):
            special.add(k + 1)
        i = end + 1
    return special


def _hash_special_lines(text: str, suffix: str, is_dockerfile: bool) -> set:
    if suffix in _PS1_SPECIAL_SUFFIXES:
        return _ps1_special_lines(text)
    if suffix in _SHELL_HEREDOC_SUFFIXES or is_dockerfile:
        return _shell_heredoc_lines(text)
    return set()


def _hash_changed_lines(original: str, edited: str) -> list:
    """Like `_js_changed_lines`, but also returns each changed line's own
    1-indexed line number and which side (`"orig"`/`"new"`) it belongs to --
    `_classify_hash_comment_edit` needs the line number to check it against
    `_hash_special_lines`, which `_js_changed_lines`'s content-only output
    can't provide."""
    changed = []
    orig_no = 0
    new_no = 0
    for line in difflib.ndiff(original.splitlines(), edited.splitlines()):
        tag = line[:2]
        content = line[2:]
        if tag == "  ":
            orig_no += 1
            new_no += 1
        elif tag == "- ":
            orig_no += 1
            changed.append(("orig", orig_no, content))
        elif tag == "+ ":
            new_no += 1
            changed.append(("new", new_no, content))
        # "? " hint lines carry no line number of their own -- skipped.
    return changed


def _classify_hash_comment_edit(original: str, edited: str, suffix: str, is_dockerfile: bool) -> str:
    """Conservative line rule for the hash-comment family (`.ps1`, `.psm1`,
    `.sh`, `.bash`, `.toml`, `.yaml`, `.yml`, `.cfg`, `.ini`, `.r`, and bare
    `Dockerfile`/`.dockerfile`) -- the same treatment `_classify_js_edit`
    gives `//`-comment languages, adapted to `#`: an edit classifies
    `"comment-only"` iff every changed line (see `_hash_changed_lines`),
    after stripping whitespace, is empty or starts with `#`, none of those
    changed lines carries a semantic comment a tool reads
    (`_HASH_SEMANTIC_COMMENT_MARKERS` -- a shebang, an encoding cookie, a
    PowerShell `#requires`, a `# shellcheck`/`# yaml-language-server`
    directive), and none of those changed lines falls inside a PowerShell
    block-comment/here-string span or a shell/dockerfile heredoc body on
    EITHER side of the edit (`_hash_special_lines`) -- a line inside one of
    those spans can look like a `#`-prefixed comment, or be entirely
    code-shaped, while carrying the opposite runtime meaning, which this
    line-by-line rule alone cannot distinguish. Anything else classifies
    `"code"` -- the safe direction, same as the JS/TS family."""
    orig_special = _hash_special_lines(original, suffix, is_dockerfile)
    new_special = _hash_special_lines(edited, suffix, is_dockerfile)
    for side, line_no, content in _hash_changed_lines(original, edited):
        if line_no in (orig_special if side == "orig" else new_special):
            return "code"
        stripped = content.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in _HASH_SEMANTIC_COMMENT_MARKERS):
            return "code"
        if not stripped.startswith("#"):
            return "code"
    return "comment-only"


class _DocstringStripper(ast.NodeTransformer):
    """Drops the first-statement docstring `Expr(Constant(str))` from the
    `body` of every `Module`/`ClassDef`/`FunctionDef`/`AsyncFunctionDef`
    node -- so `ast.dump` on the result compares structure with docstrings
    ignored entirely."""

    def _strip(self, node):
        body = getattr(node, "body", None)
        if body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:]
        return node

    def visit_Module(self, node):
        self.generic_visit(node)
        return self._strip(node)

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        return self._strip(node)

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        return self._strip(node)

    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        return self._strip(node)


class _StringConstantNormalizer(ast.NodeTransformer):
    """Replaces every string `Constant` value (docstrings included) with a
    fixed placeholder -- comparing `ast.dump` after this pass isolates
    "did any string literal's VALUE change" from "did anything else about
    the structure change", independent of length/position, unlike a
    positional token-stream comparison."""

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value="", kind=node.kind), node)
        return node


def _parse_with_type_comments(source: str) -> ast.AST | None:
    """`type_comments=True` so a `# type:` comment -- runtime-invisible but
    mypy-visible -- surfaces as a `type_comment` field on the AST and thus
    participates in the `ast.dump` structural comparison below, rather than
    silently vanishing as an ordinary comment would. Any parse failure
    (including a `SyntaxError` `type_comments=True` raises on a malformed
    `# type:` comment that plain `ast.parse` would tolerate) -> `None`, the
    caller's cue to fall back to `"code"`.

    `TypeIgnore` nodes carry the line they sit on, so their `lineno` is
    zeroed: otherwise any comment inserted above a `# type: ignore` shifts
    it and a comment-only edit compares as `"code"`."""
    try:
        tree = ast.parse(source, type_comments=True)
        for ignore in tree.type_ignores:
            ignore.lineno = 0
        return tree
    except (SyntaxError, ValueError):
        try:
            return ast.parse(source)
        except SyntaxError:
            return None


def _python_change_kind(original: str, edited: str) -> str:
    """AST-based classification -- see module docstring for the four
    outcomes. Replaces an earlier `tokenize`-based positional token-stream
    comparison, which silently misclassified any edit that changed the
    NUMBER of tokens (e.g. adding a docstring where none existed) as
    `"code"`: it zipped the two streams position-by-position and only ever
    considered a REPLACED string token, never an inserted/removed one, so a
    docstring ADD or REMOVE could never reach the `"doc-only"` branch."""
    orig_tree = _parse_with_type_comments(original)
    edit_tree = _parse_with_type_comments(edited)
    if orig_tree is None or edit_tree is None:
        return "code"

    orig_dump = ast.dump(orig_tree)
    edit_dump = ast.dump(edit_tree)
    if orig_dump == edit_dump:
        # Structurally identical AST, differing bytes -> comments/whitespace.
        return "comment-only"

    orig_stripped = ast.dump(_DocstringStripper().visit(copy.deepcopy(orig_tree)))
    edit_stripped = ast.dump(_DocstringStripper().visit(copy.deepcopy(edit_tree)))
    if orig_stripped == edit_stripped:
        return "doc-only"

    orig_normalized = ast.dump(_StringConstantNormalizer().visit(copy.deepcopy(orig_tree)))
    edit_normalized = ast.dump(_StringConstantNormalizer().visit(copy.deepcopy(edit_tree)))
    if orig_normalized == edit_normalized:
        return "string"

    return "code"


_CLASSIFIABLE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def classify_edit(original: str, edited: str, rel_path: str) -> str:
    """Classifies WHAT changed between an edited file's `original` and
    `edited` text: `"comment-only"`, `"doc-only"`, `"string"`, or `"code"`
    (see module docstring). Byte-identical `original`/`edited` text is
    `"comment-only"` (nothing runtime-visible changed, whatever the
    suffix). Falls back to `"code"` -- the conservative, import-rule-
    applying classification -- for a suffix this module does not classify,
    or anything unparseable, so a classification failure never silently
    narrows selection."""
    if original == edited:
        return "comment-only"
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".py":
        return _python_change_kind(original, edited)
    if suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return _classify_js_edit(original, edited)
    name_lower = Path(rel_path).name.lower()
    is_dockerfile = name_lower == "dockerfile" or suffix == ".dockerfile"
    if suffix in _HASH_COMMENT_SUFFIXES or is_dockerfile:
        return _classify_hash_comment_edit(original, edited, suffix, is_dockerfile)
    return "code"


def _is_conftest(rel_path: str) -> bool:
    return Path(rel_path).name == "conftest.py"


def _is_helper_module(rel_path: str, test_dirs: set) -> bool:
    """A non-test `.py` file that sits in the same directory as at least one
    test file -- a plausible shared fixture/helper module that a test file
    reaches without a literal import (e.g. via a pytest fixture defined in
    it), so it needs the same import/literal scan a test file gets."""
    if not rel_path.endswith(".py") or _is_test_file(rel_path) or _is_conftest(rel_path):
        return False
    parent = Path(rel_path).parent.as_posix()
    return parent in test_dirs


def _hit_against_edited(
    source: str,
    rel_path: str,
    edited_py_suffixes: set,
    edited_js_candidates: set,
    edited_meta_ungated: list,
    edited_py_doc_conditional_suffixes: set | None = None,
    include_dir_literal: bool = True,
    include_basename_literal: bool = True,
    import_regex_full: re.Pattern | None = None,
    import_regex_doc_conditional: re.Pattern | None = None,
    literals: set | None = None,
    edited_meta_gated: list | None = None,
) -> bool:
    """`edited_py_suffixes`/`edited_py_doc_conditional_suffixes` are the
    PRECOMPUTED union of `_module_suffixes` across the relevant edited-file
    lists (see `_suffixes_for`, computed once per `select_test_files` call,
    not per test file) -- and the `.py` source is parsed for its own import
    set exactly once here, reused against both suffix sets, rather than
    re-parsing the same source per edited-file-group as the file-list-based
    `_python_import_hit` would.

    `include_dir_literal=False`/`include_basename_literal=False` (the
    conftest/helper CASCADE call site) drop the parent-directory and bare-
    basename literal needles from `_path_literal_hit` -- see that function's
    own docstring for why either is too coarse to trust as a whole-subtree
    cascade trigger.

    `import_regex_full`/`import_regex_doc_conditional` are the PRECOMPUTED
    (`_build_import_prefilter_regex`) import-CONTEXT prefilter regexes for
    each suffix set, built once by the caller -- a `None` regex means "no
    suffixes to look for", never "skip the parse". The doc-conditional
    regex is checked ONLY when `_DOC_TEST_READER_MARKERS` already matched
    `source` (checked first, before any import scanning) -- for a
    `"doc-only"` edit, the import rule only matters to a test that itself
    reads docstrings at runtime, so a test with no doc-reader marker never
    pays for an import scan on the doc-conditional suffixes at all, prefilter
    or parse. `ast.parse` (`_python_imports`) still runs at most once per
    file, gated on EITHER prefilter regex having matched -- a regex match is
    "worth parsing to confirm", never the hit decision itself; a coincidental
    substring inside a non-import context (a leaf name in prose) no longer
    reaches `ast.parse`, unlike the raw-substring `_leaf_tokens` prefilter it
    replaces.

    `literals` is the PRECOMPUTED (`_extract_literals`) string-constant set
    for `source`, extracted once per test/shared-module file by the caller
    (see `select_test_files`) rather than once per (edited file, source)
    pair -- the fix for the field measurement's speed blowup, where the
    predecessor of this function re-lowercased the entire source text once
    per edited file. `edited_meta_ungated`/`edited_meta_gated` are the
    PRECOMPUTED (`_edited_literal_meta`) per-edited-file metadata, split by
    the caller into `"string"`/`"code"`-classified files (unconditional
    literal hit, via `_path_literal_hit`) and `"comment-only"`/`"doc-only"`-
    classified files (gated on read context, via `_gated_literal_hit`) --
    see module docstring."""
    hit = False
    is_py = rel_path.endswith(".py")
    has_doc_marker = (
        is_py and bool(edited_py_doc_conditional_suffixes) and _DOC_TEST_READER_MARKERS.search(source)
    )
    wants_full_scan = is_py and bool(edited_py_suffixes) and (
        import_regex_full is None or import_regex_full.search(source)
    )
    wants_doc_conditional_scan = has_doc_marker and (
        import_regex_doc_conditional is None or import_regex_doc_conditional.search(source)
    )
    if wants_full_scan or wants_doc_conditional_scan:
        imported = _python_imports(source)
    else:
        imported = set()
    if edited_py_suffixes:
        hit = _import_hit_against_suffixes(imported, edited_py_suffixes)
    if not hit and has_doc_marker:
        hit = _import_hit_against_suffixes(imported, edited_py_doc_conditional_suffixes)
    if not hit and edited_js_candidates and rel_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        hit = _js_import_hit(rel_path, source, edited_js_candidates)
    if not hit:
        if literals is None:
            literals = _extract_literals(source, rel_path)
        hit = _path_literal_hit(
            literals, edited_meta_ungated,
            include_dir=include_dir_literal, include_basename=include_basename_literal,
        )
    if not hit and edited_meta_gated:
        hit = _gated_literal_hit(
            source, rel_path, literals if literals is not None else _extract_literals(source, rel_path),
            edited_meta_gated,
            include_dir=include_dir_literal, include_basename=include_basename_literal,
        )
    return hit


def select_test_files(
    repo_root: str, edited_files: list, all_files: list,
    file_texts: dict | None = None,
) -> list:
    """`all_files` is the repo's tracked-file list (repo-relative, forward
    slash), computed once by the caller from `git ls-files` -- kept as a
    parameter rather than re-shelled here so this stays a zero-spawn pure
    function testable without touching a real git tree.

    `file_texts` (optional) is `edited_rel -> (original_text, edited_text)`
    for as many `edited_files` as the caller can supply -- drives
    `classify_edit` (see module docstring) so a comment-only edit narrows to
    literal-only selection instead of running the import rule. An edited file
    absent from `file_texts`, or `file_texts` omitted entirely, classifies as
    `"code"` -- the pre-existing, import-rule-applying behaviour, so an
    ad-hoc caller with no before/after text never silently under-selects.

    Also scans `conftest.py` files and colocated non-test helper modules under
    a test directory using the same import/path-literal rules a test file
    gets -- a test that consumes a shared fixture (defined in `conftest.py`
    or a sibling helper module) neither imports nor literally mentions the
    edited file itself, so scanning only test files misses that fixture's own
    hit entirely and silently zero-selects. A hit on `conftest.py` selects
    every test file in that conftest's directory subtree (pytest conftest
    visibility cascades downward); a hit on a colocated helper module selects
    every test file in that helper's own directory subtree."""
    root = Path(repo_root)
    file_texts = file_texts or {}
    test_files = [
        f for f in all_files
        if _is_test_file(f) and _runner_collectible(repo_root, f)
    ]
    test_dirs = {Path(f).parent.as_posix() for f in test_files}

    def _classification(f: str) -> str:
        pair = file_texts.get(f)
        if pair is None:
            return "code"
        return classify_edit(pair[0], pair[1], f)

    def _read(rel_path: str) -> str | None:
        try:
            return (root / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    py_classifications = {f: _classification(f) for f in edited_files if f.endswith(".py")}
    edited_py_full = [f for f, c in py_classifications.items() if c not in ("comment-only", "doc-only")]
    edited_py_doc_only = [f for f, c in py_classifications.items() if c == "doc-only"]

    def _self_reads_own_docstrings(rel_path: str) -> bool:
        pair = file_texts.get(rel_path)
        edited_text = pair[1] if pair is not None else _read(rel_path)
        return bool(edited_text) and bool(_DOC_SELF_READER_MARKERS.search(edited_text))

    edited_py_doc_self_reader = [f for f in edited_py_doc_only if _self_reads_own_docstrings(f)]
    edited_py_doc_conditional = [f for f in edited_py_doc_only if f not in edited_py_doc_self_reader]

    edited_py = edited_py_full + edited_py_doc_self_reader
    edited_js_candidates = {
        f.replace("\\", "/") for f in edited_files
        if f.endswith((".ts", ".tsx", ".js", ".jsx")) and _classification(f) != "comment-only"
    }

    # Computed ONCE per call, not per test file -- see `_suffixes_for` and
    # `_hit_against_edited` for why recomputing these inside the per-file
    # scan was the dominant cost over a large test corpus.
    edited_py_suffixes = _suffixes_for(edited_py)
    edited_py_doc_conditional_suffixes = _suffixes_for(edited_py_doc_conditional)
    import_regex_full = _build_import_prefilter_regex(edited_py_suffixes)
    import_regex_doc_conditional = _build_import_prefilter_regex(edited_py_doc_conditional_suffixes)
    literal_prefilter_regex = _build_literal_prefilter_regex(_literal_prefilter_tokens(edited_files))

    all_classifications = {f: _classification(f) for f in edited_files}
    gated_files = [
        f for f, c in all_classifications.items() if c in ("comment-only", "doc-only")
    ]
    ungated_files = [f for f in edited_files if f not in gated_files]
    edited_meta_gated = _edited_literal_meta(gated_files)
    edited_meta_ungated = _edited_literal_meta(ungated_files)

    def _literals_for(source: str, rel_path: str) -> set:
        if literal_prefilter_regex is not None and not literal_prefilter_regex.search(source):
            return set()
        return _extract_literals(source, rel_path)

    selected: set = set()
    for test_rel in test_files:
        source = _read(test_rel)
        if source is None:
            continue
        if _hit_against_edited(
            source, test_rel, edited_py_suffixes, edited_js_candidates, edited_meta_ungated,
            edited_py_doc_conditional_suffixes,
            import_regex_full=import_regex_full,
            import_regex_doc_conditional=import_regex_doc_conditional,
            literals=_literals_for(source, test_rel),
            edited_meta_gated=edited_meta_gated,
        ):
            selected.add(test_rel)

    shared_modules = [f for f in all_files if _is_conftest(f) or _is_helper_module(f, test_dirs)]
    for shared_rel in shared_modules:
        source = _read(shared_rel)
        if source is None:
            continue
        # The doc-conditional import branch (an importer selected merely
        # because IT reads docstrings, per the loose `_DOC_TEST_READER_MARKERS`
        # heuristic) is trusted as a whole-directory CASCADE TRIGGER only for
        # `conftest.py` -- pytest's own fixture-visibility mechanism is the
        # thing that actually justifies treating a hit as "every test below
        # here may depend on this", and that mechanism is conftest-specific.
        # An ordinary colocated non-test module (`_is_helper_module`) has no
        # such transitive visibility: a large multi-purpose script (a CLI
        # entry point with argparse `--help`/`description=` text, common in
        # this repo) satisfies the doc-marker heuristic constantly and, once
        # trusted as a cascade trigger, selected its ENTIRE directory subtree
        # -- 3014 of 3443 test files for a 32-file docstring-only edit, none
        # of which import or mention the edited files at all. A real
        # cross-test dependency on a colocated helper still surfaces the
        # normal way: any test file that actually imports/mentions the helper
        # is picked up directly by the per-test-file scan above, or by this
        # same helper-module scan's `edited_py_suffixes`/literal-full-path
        # branches, neither of which is narrowed here.
        is_conftest = _is_conftest(shared_rel)
        doc_conditional_suffixes = edited_py_doc_conditional_suffixes if is_conftest else set()
        doc_conditional_regex = import_regex_doc_conditional if is_conftest else None
        if not _hit_against_edited(
            source, shared_rel, edited_py_suffixes, edited_js_candidates, edited_meta_ungated,
            doc_conditional_suffixes,
            include_dir_literal=False, include_basename_literal=False,
            import_regex_full=import_regex_full,
            import_regex_doc_conditional=doc_conditional_regex,
            literals=_literals_for(source, shared_rel),
            edited_meta_gated=edited_meta_gated,
        ):
            continue
        shared_dir = Path(shared_rel).parent.as_posix()
        for test_rel in test_files:
            test_dir = Path(test_rel).parent.as_posix()
            if shared_dir == "." or test_dir == shared_dir or test_dir.startswith(shared_dir + "/"):
                selected.add(test_rel)

    # Preserve `all_files`/`test_files` order for a stable, deterministic result.
    order = {f: i for i, f in enumerate(test_files)}
    return sorted(selected, key=lambda f: order[f])
