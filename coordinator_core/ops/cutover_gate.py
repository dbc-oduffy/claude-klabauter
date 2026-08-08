"""
coordinator_core.ops.cutover_gate — pure derivation executor for cutover-record
``gate_source`` specs.

Purpose: given a cutover record's ``gate_source`` (a DATA spec — kind + pattern
+ paths[] + repos[] — never a command; see "gate_source is DATA" below) and a
map of which sibling repos this process can actually reach on disk, derive the
CURRENT consumer set at call time. This module is deliberately import-only —
it registers no op (``@register_op``) and performs no I/O beyond reading
already-resolved source files. Op registration, the two-way agreement test
against ``confirmed_consumers``, and REFUSE/PASS/INDETERMINATE classification
are C4b's concern, landing in a later wave of the SAME module file so this
chunk leaves that seam clean rather than pre-guessing its shape.

Why re-derive rather than trust a stored list: DR-084 ("Recount before
applying, not before deciding") is the standing lesson this whole primitive
answers. A cutover gate that compared a hand-authored ``confirmed_consumers``
list against a hand-authored known-consumer list discharges nothing — both
lists can under-list in lockstep and the gate cheerfully passes. See
docs/plans/2026-07-25-cutover-state-machine.md § The load-bearing design
decision.

gate_source is DATA, never a command (Review: the Director of Engineering-cutover-review F3):
``pattern`` is compiled once via ``re.compile`` and applied IN-PROCESS against
already-read file text / AST string constants — this module shells out to
nothing, ever. ``paths[]`` entries are containment-checked via
``coordinator_core.ops._path_guard.contained_path`` before any read, reusing
the same primitive the rest of the op family uses for caller-supplied paths
(see that module's docstring for the threat model this defends against).

Why AST-first for Python, regex reserved for JS/markdown (Review:
The Director of Engineering-cutover-review F2, engaging with ``cartography_edges.py``'s own standing
position): that module's header argues against porting a fence's
``grep -rl <module> ... | wc -l`` shape into an op when the AST-derived
import/call edge graph (``coordinator_core.cartography.edges.build_edges``) is
a strict superset for Python — grep cannot see intra-module call edges and
silently misses them without saying so, whereas the edge graph is explicit
about what it does and does not cover (``static_only`` / ``excludes``). This
module follows that precedent for Python source: every ``.py`` candidate file
is parsed with ``ast`` (never grepped) for BOTH (a) string-literal occurrences
of the vocabulary pattern, classified as writer- or reader-context by their
immediate AST parent, and (b) a supplementary pass over
``cartography.edges.build_edges``'s import/call edge graph, matching the
pattern against edge target names as a second, structural signal. Regex text
search is used ONLY for the languages the AST graph cannot parse at all — JS
and markdown — which is precisely the blind spot that produced DR-084's own
near-miss: ``consumed-marker.js``'s hardcoded ``TERMINAL_DEPLOYMENT`` array
was a plain JS string-literal enum reader with no Python AST to walk. A
derivation that skipped the JS/markdown regex pass and relied on the Python
AST graph alone would reproduce that exact near-miss inside the gate itself.

Usage-context classification, not pattern narrowing (precision fix, post-DR-084
exemplar red-run): the FIRST cut of this derivation counted a pattern match
ANYWHERE a string constant appeared — including a module/class/function
DOCSTRING, prose inside a markdown file's BODY TEXT, or a JS comment. Because
the vocabulary here is bare English words (``closed``, ``continued``,
``abandoned``), that counted "the handoff was closed by a peer" in a
docstring, "fails closed" in a comment, and "the migration continued" in
markdown prose as if each were an enum reader — inflating a ~12-file census
to 377. The fix is NOT to narrow ``pattern`` back to a record-specific token
(``derive-value-vocabulary.md``'s own "Why gate_source.pattern is the surface,
not the new token" explains why that reintroduces the original DR-084
near-miss: a new-token pattern is structurally blind to unmigrated
consumers). The fix is to tighten WHAT COUNTS as a usage once the surface
pattern matches:

  - Python: a match counts only when its immediate AST parent is a
    STRUCTURAL position — a comparison operand, a dict/collection member, a
    default argument, a keyword argument, an assignment target's value. A
    string constant that IS a docstring (an ``ast.Expr`` whose value is a
    bare ``ast.Constant`` string, positioned as the first statement of a
    ``Module``/``ClassDef``/``FunctionDef``/``AsyncFunctionDef`` body — the
    standard docstring convention) is excluded outright, regardless of what
    its immediate-parent shape would otherwise classify it as. Comments are
    never AST nodes, so they were never at risk here (see
    ``_docstring_constant_ids``, ``_ConstantClassifier``).
  - Every match, in every language, must be a WHOLE-TOKEN match — bounded by
    non-identifier characters on both sides, never a substring of a longer
    identifier or of surrounding prose (``_has_whole_token_match``). This is
    enforced by the derivation itself, not merely by convention in how a
    record author writes ``pattern`` (a future record's pattern need not
    remember to add ``\b`` — the derivation adds the boundary check
    unconditionally).
  - Markdown: prose in the document BODY never counts, structured position
    or not. Only a frontmatter field value or the contents of a fenced code
    block count (``_scan_markdown_file``) — the markdown analogue of "not a
    docstring, not a comment".
  - JS: the string-literal reader-array shape this kind exists to catch
    (``consumed-marker.js``'s ``TERMINAL_DEPLOYMENT``) is still caught via
    regex, but line (``//``) and block (``/* */``) comments are stripped
    first (``_scan_js_file`` / ``_strip_js_comments`` — a best-effort regex
    strip, not a JS parser; documented heuristic, not a false claim of
    completeness).

Do NOT "simplify" this back to a bare ``pattern.search(text)`` sweep — that
is precisely the defect this section exists to describe, and reintroducing
it silently re-inflates the derived set by roughly the same 30x this fix
closed.

Multi-modality is the KIND's property, not a per-record choice (Review:
The Director of Engineering-cutover-review F2): ``value-vocabulary``'s implementation below always
runs both the writer-context scan and the reader-context scan (Python AST +
edge graph + JS/markdown regex) unconditionally — there is no parameter that
lets a record author opt out of one mode. Binding the mode set to the kind
(not to record authoring) is the whole point: DR-084 already shipped a
writer-only migration once, silently, in prose; this module makes that
omission structurally unrepresentable for any record that selects
``value-vocabulary``.

Extensionless Python consumers (FIX-D — the named house trap, example-doctrine-repo
coordinator.local.md: "count by shebang, not by extension"): the candidate-file
collector cannot key off ``.py`` alone. The plan's own worked example,
``coordinator/bin/archive-stamp-cli``, is a pure-Python file with no
extension — its first line is ``#!/usr/bin/env python3``. A collector that
enumerates by suffix silently skips it, which is precisely this whole
module's reason for existing turned against itself: a derivation the gate
trusts that cannot see a real consumer. ``_has_python_shebang`` reads ONLY
the first line of an extensionless regular file (never the whole file) to
classify it; a match routes the file through the SAME AST-first path as a
``.py`` file (``_is_python_candidate``), and containment via
``contained_path`` applies identically — this widens WHICH files are
recognized, never WHERE the walk is allowed to look. If a shebang-classified
file cannot be parsed as Python (syntax error, non-UTF-8 body), it falls
back to the regex reader pass rather than vanishing from the derived set
silently — the same "a file the derivation can see but cannot parse must not
disappear" property the JS/markdown regex pass already had, extended to this
case, which has no reliable ``.py`` suffix to fall back on treating as
"contributes nothing" the way an actually-``.py``-suffixed parse failure
does.

Foreign-repo handling (Review: the Director of Engineering-cutover-review F5): this module receives
``repo_roots`` — the set of repos the CALLER can actually resolve a local
path for (example-doctrine-repo + claude-klabauter ``coordinator/bin/``, per D2). For every repo named in
``gate_source.repos[]`` this module returns an ANNOTATED copy recording
whether it was actually scanned (``scanned: bool``) alongside the record's own
declared ``foreign`` flag — but it does NOT decide PASS/REFUSE on an
unscanned/foreign repo. That fail-closed decision belongs to C4b, which reads
this module's ``repos`` return field.

Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § C4a

Negative-spec:
  - Does NOT register an op. No ``@register_op`` call anywhere in this
    module — C4b adds ``cutover.gate`` in a later wave.
  - Does NOT evaluate ``confirmed_consumers`` at all. This module only ever
    computes the derived set; the two-way agreement test is C4b's.
  - Does NOT shell out. No ``subprocess`` import, no shell invocation of any
    kind — ``pattern`` is applied purely in-process.
  - Does NOT resolve which repos are reachable. ``repo_roots`` is supplied
    by the caller; this module has no notion of a default repo map (mirrors
    ``coordinator_core.ops._path_guard``'s own negative-spec on
    ``allowed_roots``).
  - Does NOT attempt cross-file or cross-repo call resolution beyond what
    ``cartography.edges.build_edges`` already provides (intra-module call
    edges only) — see that module's own "THE HYBRID BLIND SPOT" for the
    inherited limitation.

C4b addition — the ``cutover.gate`` op handler (``@register_op("cutover.gate")``,
below): landed in this same module file (Review: the Director of Engineering-cutover-review F8/F14 —
module-file grouping decision made once, at C4a). The negative-spec above
("Does NOT shell out", "Does NOT register an op") describes ``derive()``
alone; it does NOT describe this module once the op handler is added.
``_cutover_gate`` DOES invoke read-only subprocess calls (a re-run pytest
node id, a `git cat-file -e` reachability check) for signal-2
re-verification, and DOES call other registered ops in-process (via
``coordinator_core.ipc.get_op_handler``) for `probe-op-key` re-verification.
It still writes nothing to disk, into rag, or to any shared mutable state —
see the DR-208 five-question affirmation below.

DR-208 five-question affirmation (COMPUTE_ONLY; citing ``_cutover_gate``):
  1. Writes, deletes, or reorders any state file, queue, or git object?    No.
     The handler reads the cutover record (``Path.read_text``), re-derives
     the consumer set (``derive()``, read-only), and re-verifies
     ``confirmed_consumers[].verified_by`` via read-only checks (a pytest
     re-run whose subprocess is never given write access to the record, a
     read-only `git cat-file -e`, an in-process call to another op's
     handler). No file is opened for write anywhere in this handler.
  2. Writes into rag's relational store?                                   No.
     Returns a structured verdict dict to the caller; no rag interaction.
  3. Opens any file for write (including sentinel creation)?               No.
     Every file this handler touches is opened read-only; no tempfile, no
     sentinel, no ``os.replace``. The record's ``derivation_history`` entry
     this handler computes is returned to the caller as
     ``derivation_history_entry`` — it is NOT written back to the record.
     Persisting it (alongside a phase bump) is ``cutover.advance``'s
     (C5, MUTATING) job, not this read-only gate's.
  4. Mutates shared mutable state outside its own module?                  No.
     No module-global or cross-module mutable state is written; the
     in-process `probe-op-key` call invokes another op's handler exactly as
     ``coordinator_core/ops/ceremony/tail_ops.py::run_coverage_gate`` does
     (same ``get_op_handler`` + await pattern) — that precedent is itself
     COMPUTE_ONLY-compatible (`coverage.gate`).
  5. Persistent state changes observable across process boundaries?       No.
     Nothing this handler does is written to disk or any external store;
     the pytest/git subprocess calls it makes are themselves read-only
     (test re-run, commit-reachability check) with no side effect on the
     cutover record or any other coordinator substrate.
  Git-shelling-is-read-only precedent: the `git cat-file -e` call mirrors
  `coverage.gate`'s affirmed-COMPUTE_ONLY git subprocess reads (DR-208's own
  table: "all subprocess calls are read-only git queries"). The pytest
  re-run subprocess is a DEPARTURE from that narrow precedent (it executes a
  test process, not a git query) but is itself read-only with respect to
  coordinator substrate — the plan's own Signal-2 design (§ Considered
  alternatives, Review: the Director of Engineering-cutover-review F7, PM-approved in full
  2026-07-25) requires exactly this re-run, and no substrate write results
  from it either way.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
"""

from __future__ import annotations

import ast
import asyncio
import datetime
import inspect
import json
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml

from coordinator_core.cartography.edges import build_edges
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.schema_drift_watch import resolve_doe_repo_path
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._pytest_child_env import pytest_child_env
from coordinator_core.ops.fleet._common import main_worktree_root

#: Repo-relative path (example-doctrine-repo-relative) of the vendored cutover schema —
#: mirrors D2 ("schema lives example-doctrine-repo-side"). Read via `git show <ref>:<this>`,
#: never vendored into claude-klabauter (unlike the frontmatter schemas
#: `schema_drift_watch.py` watches — this schema is read live, not copied).
_CUTOVER_SCHEMA_REPO_RELATIVE_PATH = "coordinator/schemas/cutover.schema.json"

#: Closed enum of gate_source.kind values this module knows how to derive.
#: Mirrors the schema-level closed enum (coordinator/schemas/cutover.schema.json,
#: C1) — an unrecognized kind is a hard error here, not a silent no-op.
SUPPORTED_KINDS: tuple[str, ...] = ("value-vocabulary",)

#: Non-Python file suffixes the regex-reader pass covers — the languages the
#: AST edge graph cannot parse at all (see module docstring).
_TEXT_SUFFIXES: frozenset[str] = frozenset({".js", ".mjs", ".md"})

#: AST node types whose immediate child is being WRITTEN/ASSIGNED, held as a
#: named default, or held as a collection/mapping MEMBER — a string constant
#: with one of these as its immediate parent is classified as writer/
#: structural context. Heuristic, not exhaustive (see _ConstantClassifier
#: docstring negative-spec). ``ast.arguments`` covers a function/lambda
#: parameter's DEFAULT value (the "default argument" structural position);
#: ``ast.List``/``ast.Tuple``/``ast.Set`` cover a collection-literal MEMBER
#: (e.g. ``frozenset({"closed", "continued"})``'s ``ast.Set`` display).
_WRITE_PARENT_TYPES: tuple[type, ...] = (
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.keyword,
    ast.Dict,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.arguments,
)

#: AST node types whose immediate child is being COMPARED — a string
#: constant with this as its immediate parent is classified as reader
#: context. Deliberately narrow: a bare positional call argument, a
#: ``return``/``raise`` value, or an f-string/concatenation fragment is NOT
#: a comparison operand and is excluded (see _ConstantClassifier docstring —
#: those shapes are message-prose-shaped, not enum-reader-shaped, and this
#: is the second half of the precision fix alongside docstring exclusion).
_READER_PARENT_TYPES: tuple[type, ...] = (ast.Compare,)

#: AST node types that own a docstring-eligible ``body`` — a bare ``ast.Expr``
#: string constant as the FIRST statement of one of these is the standard
#: Python docstring convention (see ``_docstring_constant_ids``).
_DOCSTRING_OWNER_TYPES: tuple[type, ...] = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

#: Character class treated as "part of an identifier/token" for whole-token
#: boundary checks (``_has_whole_token_match``) — letters, digits, underscore.
_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9_]")

#: A LINE (after optional ``key:`` stripping, see ``_is_prose``) with more
#: whitespace-separated words than this is PROSE (a human sentence — an
#: error message, a JSON-schema field description, a usage string), not a
#: vocabulary token or value, regardless of the structural position (dict
#: value, collection member, etc.) it sits in. ``1`` — a genuine vocabulary
#: value is a BARE TOKEN ("closed", "deployment_state=closed",
#: "abandoned") with no internal whitespace; two or more space-separated
#: words is sentence shape, not enum shape.
_MAX_STRUCTURAL_VALUE_WORDS = 1

#: Matches a YAML-shaped ``key: value`` line (frontmatter, or a Python test
#: fixture string embedding literal YAML/frontmatter text) — group 1 is the
#: VALUE half only, never the key. A key name is field-shaped, not a
#: sentence, however many words are convention in the field ("closed_reason
#: must be one of: cancelled" fails this because there is a SPACE before
#: the colon — a real YAML key never has one); this exists so
#: ``_is_prose`` can evaluate "is the VALUE a bare token" rather than
#: penalizing the harmless ``key:`` prefix for pushing the word count over.
_YAML_KEY_VALUE_RE = re.compile(r"^[\w.-]+:\s*(.*)$")


def _is_prose(text: str) -> bool:
    """Return True iff EVERY line of ``text`` reads as a human sentence, not a bare token/value.

    A vocabulary token or a short structural value ("closed",
    "deployment_state: closed", ``["closed", "continued"]``'s members) is a
    BARE TOKEN — one word, or a ``key: value`` line whose value half is one
    word (``_YAML_KEY_VALUE_RE`` strips the key so it never inflates the
    count). A JSON-schema field description or an error message ("required
    when deployment_state=closed") is prose that happens to share surface
    vocabulary with the enum this module derives against — the same
    failure mode a docstring or markdown-body mention represents, just one
    level deeper (inside a dict VALUE / collection member rather than a
    top-level module string). ``dict key or value`` and ``collection
    member`` are named structural positions (module docstring,
    ``_ConstantClassifier`` docstring) precisely so a genuine vocabulary
    container (``frozenset({"closed", "continued"})``) is caught — this
    heuristic keeps that catch while excluding the free-text sentence
    sharing the same AST shape.

    Evaluated LINE BY LINE (a Python test-fixture string commonly embeds
    multi-line YAML/frontmatter text, e.g. ``"title: orig\\nstatus: closed\\n"``)
    — the whole value is prose only if EVERY non-blank line is prose; a
    single bare-token or ``key: value`` line anywhere in a multi-line value
    is enough to call the value structural, matching how a markdown
    frontmatter block is evaluated (``_scan_markdown_file``, which applies
    this same per-line rule directly). A word-count threshold, not sentence
    parsing — deliberately crude, since the failure mode (hundreds of
    description-string false positives) is itself crude.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        kv_match = _YAML_KEY_VALUE_RE.match(line)
        candidate = kv_match.group(1) if kv_match is not None else line
        if len(candidate.split()) <= _MAX_STRUCTURAL_VALUE_WORDS:
            return False
    return True


def _has_whole_token_match(pattern: re.Pattern[str], text: str) -> bool:
    """Return True iff ``pattern`` matches ``text`` at a WHOLE-TOKEN boundary.

    A raw ``pattern.search(text)`` hit is accepted only if the character
    immediately before the match (if any) and immediately after (if any) are
    both non-identifier characters — i.e. the match is not a substring of a
    longer identifier ("closed_reason_v2") or of surrounding prose ("the
    handoff was closed by a peer" still matches "closed" at a legitimate
    boundary, which is exactly why this alone is not sufficient — see the
    docstring/comment/prose exclusions applied by each caller). Enforced
    unconditionally by the derivation, independent of whether the record
    author's own ``pattern`` string already includes ``\\b`` markers.
    """
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > 0 and _WORD_CHAR_RE.match(text[start - 1]):
            continue
        if end < len(text) and _WORD_CHAR_RE.match(text[end]):
            continue
        return True
    return False


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every ``ast.Constant`` node that is a docstring.

    A docstring is an ``ast.Expr`` whose value is a bare string
    ``ast.Constant``, positioned as the FIRST statement of a
    ``Module``/``ClassDef``/``FunctionDef``/``AsyncFunctionDef`` body — the
    standard Python docstring convention (``ast.get_docstring`` targets the
    same shape; this function returns node identities rather than string
    values so the classifier can exclude the exact node by identity, not by
    re-matching its text). These constants are prose, never an enum reader
    or writer, and are excluded from vocabulary derivation regardless of
    what their immediate-parent shape (``ast.Expr``) would otherwise imply.

    Negative-spec: does NOT catch a string assigned to ``__doc__`` via a
    plain ``ast.Assign`` — that shape is already classified WRITER by
    ``_ConstantClassifier`` and is a legitimate (if unusual) structural
    usage, not prose.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNER_TYPES):
            body = getattr(node, "body", None)
            if body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    ids.add(id(first.value))
    return ids


class UnsupportedGateSourceKind(ValueError):
    """Raised when gate_source.kind is not in SUPPORTED_KINDS."""


class _ConstantClassifier(ast.NodeVisitor):
    """Walk a parsed module, classifying pattern-matching string constants.

    A string constant matching ``pattern`` is classified WRITER if its
    immediate AST parent is a write/structural-member-shaped node
    (assignment, augmented/annotated assignment, a call/def keyword or
    default argument, a dict literal key/value, or a list/tuple/set literal
    member — ``_WRITE_PARENT_TYPES``), READER if its immediate parent is a
    comparison (``ast.Compare`` — ``_READER_PARENT_TYPES``), and otherwise
    EXCLUDED — not writer, not reader, not counted at all.

    The exclusion bucket is deliberate, not an oversight: a bare positional
    call argument (``print("... closed ...")``), a ``return``/``raise``
    value, or a string-concatenation/f-string fragment is MESSAGE-shaped —
    indistinguishable, by AST shape alone, from human-facing prose ("fail-
    closed", "the migration continued") that happens to share vocabulary
    with a domain enum. Counting every such position is exactly how the
    precision-fix's predecessor derivation reached 377 on a ~12-file census
    (module docstring "Usage-context classification, not pattern
    narrowing"). The five write-shaped positions and the one reader-shaped
    position above are the FULL closed set of what counts as a structural
    enum usage; nothing else does, however plausible a bare-call-argument
    reading might seem case-by-case.

    Negative-spec: this is a shallow, immediate-parent heuristic, not a
    dataflow analysis. A dict KEY matching the pattern is also classified
    writer (both keys and values of an ``ast.Dict`` share the same immediate
    parent) — an accepted false-positive in the writer direction, not a
    missed reader. The classifier never raises on well-formed ASTs; the
    caller is responsible for catching ``SyntaxError`` from ``ast.parse``.

    ``docstring_ids`` (node identities from ``_docstring_constant_ids``) are
    excluded outright before parent-shape classification even runs — a
    docstring is prose, never a reader or a writer, no matter what its
    immediate ``ast.Expr`` parent would otherwise imply. Every match is also
    required to be a WHOLE-TOKEN match (``_has_whole_token_match``), not a
    substring of a longer identifier or word.
    """

    def __init__(self, pattern: re.Pattern[str], docstring_ids: frozenset[int] = frozenset()) -> None:
        self._pattern = pattern
        self._docstring_ids = docstring_ids
        self._stack: list[ast.AST] = []
        self.is_writer = False
        self.is_reader = False

    def generic_visit(self, node: ast.AST) -> None:  # noqa: D102 — ast.NodeVisitor override
        self._stack.append(node)
        try:
            super().generic_visit(node)
        finally:
            self._stack.pop()

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802 — ast.NodeVisitor naming
        if (
            isinstance(node.value, str)
            and id(node) not in self._docstring_ids
            and not _is_prose(node.value)
            and _has_whole_token_match(self._pattern, node.value)
        ):
            parent = self._stack[-1] if self._stack else None
            if isinstance(parent, _WRITE_PARENT_TYPES):
                self.is_writer = True
            elif isinstance(parent, _READER_PARENT_TYPES):
                self.is_reader = True
            # else: a non-structural (message-shaped) position — positional
            # call argument, return/raise value, f-string/concat fragment —
            # excluded outright, not counted as writer or reader.
        self.generic_visit(node)


def _scan_python_file(file_path: Path, pattern: re.Pattern[str]) -> Optional[tuple[bool, bool]]:
    """Return (is_writer, is_reader) for a single Python source file, or None.

    Parses with ``ast`` (never grepped — see module docstring). Any read or
    parse failure (unreadable file, non-UTF-8 encoding, malformed syntax)
    yields ``None`` — a caller for a ``.py``-suffixed file treats that the
    same as ``(False, False)`` (file contributes nothing, mirroring this
    function's own historical shape); a caller for an extensionless
    shebang-classified file (no reliable ``.py`` suffix to trust) instead
    falls back to the regex reader pass rather than letting the file vanish
    silently from the derived set (see ``_derive_value_vocabulary``).
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    docstring_ids = frozenset(_docstring_constant_ids(tree))
    classifier = _ConstantClassifier(pattern, docstring_ids)
    classifier.visit(tree)
    return classifier.is_writer, classifier.is_reader


#: Shebang interpreter names this module treats as "this is Python" for an
#: extensionless file — matches "python"/"python3"/"python3.11" as either the
#: last path segment of the interpreter (``#!/usr/bin/python3``) or the
#: argument to ``env`` (``#!/usr/bin/env python3``), in both cases requiring
#: a word boundary on both ends so "python3-config"-shaped near-misses don't
#: false-match.
_PYTHON_SHEBANG_RE = re.compile(r"(?:^|[/\s])python3?(?:\.\d+)?(?:$|\s)")


def _has_python_shebang(file_path: Path) -> bool:
    """Return True iff ``file_path``'s first line is a shebang naming Python.

    Named house trap (example-doctrine-repo coordinator.local.md: "count by shebang, not by
    extension") — several extensionless files in this fleet (e.g.
    ``coordinator/bin/archive-stamp-cli``, the plan's own worked example) are
    pure Python with no ``.py`` suffix; a collector that keys off extension
    alone silently cannot see them. Reads ONLY the first line (bounded read,
    not the whole file) to classify — never loads a full file just to check
    its shebang. An unreadable file or a first line that fails to decode as
    UTF-8 (a binary file, most commonly) yields False rather than raising;
    a file this check cannot see contributes nothing, mirroring
    ``_scan_python_file``'s and ``_scan_text_file``'s own fail-quiet shape for
    read/parse failures.
    """
    try:
        with file_path.open("rb") as fh:
            first_line_bytes = fh.readline(256)
    except OSError:
        return False
    try:
        first_line = first_line_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not first_line.startswith("#!"):
        return False
    return bool(_PYTHON_SHEBANG_RE.search(first_line))


def _is_python_candidate(file_path: Path) -> bool:
    """Return True iff ``file_path`` should be treated as a Python source file.

    True for an actual ``.py`` suffix, OR for an extensionless file whose
    first line is a Python shebang (see ``_has_python_shebang``). Does NOT
    treat a file with some OTHER non-empty suffix (``.sh``, ``.txt``, ...) as
    Python even if it happens to carry a Python shebang — the shebang
    fallback exists to recover extensionless consumers specifically, not to
    reclassify differently-suffixed files.
    """
    if file_path.suffix == ".py":
        return True
    if file_path.suffix == "":
        return _has_python_shebang(file_path)
    return False


#: Matches a leading markdown frontmatter block: the file's first line is
#: exactly ``---``, and the block closes at the next line that is exactly
#: ``---``. Group 1 is the frontmatter body (YAML), never the delimiters
#: themselves.
_MD_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)

#: Matches a fenced code block (```...```` … ```` ```` ``` ``` ``` ```), any
#: info-string. Group 1 is the fence's CONTENTS, never the fence markers.
_MD_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

#: JS line comment (``// ...`` to end of line) and block comment
#: (``/* ... */``, possibly multi-line). Regex-based, best-effort — not a JS
#: parser/tokenizer, so a ``//`` or ``/*`` inside a string literal is
#: (rarely, in this fleet's actual JS) mis-stripped as a comment start. That
#: is an accepted false-negative in the exclusion direction (a real usage
#: inside such a string could be missed), never a false-positive that
#: inflates the derived set — the failure mode this module exists to close.
_JS_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

#: A single- or double-quoted JS string literal, escapes tolerated. Group 1
#: is the single-quoted content, group 2 the double-quoted content — exactly
#: one is non-``None`` per match. Not a backtick/template-literal reader
#: (documented gap, mirrors ``_strip_js_comments``'s own best-effort scope).
_JS_STRING_LITERAL_RE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")


def _strip_js_comments(text: str) -> str:
    """Best-effort removal of JS line and block comments (see regex docstrings above)."""
    text = _JS_BLOCK_COMMENT_RE.sub(" ", text)
    text = _JS_LINE_COMMENT_RE.sub(" ", text)
    return text


def _scan_js_file(file_path: Path, pattern: re.Pattern[str]) -> bool:
    """Return True iff ``pattern`` whole-token-matches a non-prose JS STRING LITERAL.

    Reserved for the DR-084 ``consumed-marker.js`` shape — a hardcoded
    string-literal enum reader the Python AST edge graph cannot parse at all
    (module docstring "Why AST-first for Python"). Comments are stripped
    first (``_strip_js_comments``) so a mention inside ``// ...`` or
    ``/* ... */`` — the JS analogue of a docstring — never counts. Matching
    is then scoped to the CONTENTS of quoted string literals only
    (``_JS_STRING_LITERAL_RE``) — the kind's own definition is "a hardcoded
    string-literal enum reader," so a bare code-comment-adjacent word
    outside any string was never in scope, and a match inside a literal is
    further rejected if that literal's content is PROSE (``_is_prose`` — an
    ``assert.equal(..., 'must be a closed shape (...)')`` message string
    sharing surface vocabulary with the enum, the JS analogue of a Python
    dict-value description string). A read failure yields False rather than
    raising.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    stripped = _strip_js_comments(text)
    for literal_match in _JS_STRING_LITERAL_RE.finditer(stripped):
        content = literal_match.group(1) if literal_match.group(1) is not None else literal_match.group(2)
        if content and not _is_prose(content) and _has_whole_token_match(pattern, content):
            return True
    return False


def _scan_markdown_file(file_path: Path, pattern: re.Pattern[str]) -> bool:
    """Return True iff ``pattern`` whole-token-matches a STRUCTURED, non-prose LINE in a markdown file.

    A bare vocabulary word in markdown BODY PROSE is never an enum reader —
    the markdown analogue of a Python docstring or a JS comment. Only two
    positions count: a leading YAML frontmatter block's field values, and
    the contents of a fenced code block (```` ``` ```` … ```` ``` ````).

    The two positions are NOT scanned identically (census fold-in constraint
    1, state/memos/2026-07-25-census-requirement-folded-into-cutover-
    primitive.md § "The requirement handed forward", item 1 — the
    ``resolve-python.sh`` 456-call-sites-in-``.md``-fences incident): a
    frontmatter field VALUE is prose-filtered LINE BY LINE (``_is_prose``) —
    a YAML ``notes:``/``summary:`` value can itself hold a free-text
    sentence mentioning the vocabulary in passing, the same failure mode a
    JSON-schema description string represents in Python, one syntax over.
    A FENCE line is never prose-filtered by word count — fence content is
    CODE by definition (delimited by the fence markers, not by sentence
    shape), and a genuine command-invocation call site routinely carries
    MULTIPLE whitespace-separated tokens (the command name plus its flags/
    arguments — e.g. ``archive-stamp-cli --close <path>``). Applying
    ``_is_prose``'s word-count heuristic to fence lines excluded exactly
    that shape as if it were a human sentence, reproducing the
    resolve-python.sh blind spot INSIDE the primitive built to catch it: a
    real call site silently invisible to the derivation rather than an
    extension-filtered file.

    One narrow, non-word-count guard survives on fence lines (Review:
    code-reviewer F2): a line whose first non-whitespace characters are a
    COMMENT MARKER (``#``, ``//``) is skipped outright, regardless of word
    count — a comment is unambiguously not a call site in any language this
    module's fences carry (bash/Python ``#``, JS ``//``), so this guard
    trades no precision against the resolve-python.sh regression the
    word-count removal fixed. It is a structural/shape check (first
    non-whitespace token), not the word-count heuristic reintroduced.

    Negative-spec / accepted tradeoff: a non-comment PROSE sentence inside
    a fence (e.g. a pasted log line, a free-text description embedded in an
    example JSON/YAML value) that happens to whole-token-match the pattern
    is NOT filtered — there is no reliable structural signal distinguishing
    that shape from a genuine multi-token call site without reintroducing
    the word-count heuristic this fix was written to remove. Likewise, code
    inside a fence that references the pattern without literally invoking
    it (e.g. a bare string-literal assignment) is treated as a match, same
    as the regex-reader pass already does for genuinely AST-unparseable
    files — a false positive in the "one extra file gets INDETERMINATE'd or
    counted as a reader" sense is a materially cheaper failure mode than the
    false negative (a real call site vanishing) this function exists to
    avoid. See
    ``test_markdown_fenced_prose_sentence_is_accepted_tradeoff_false_positive``
    and
    ``test_markdown_fenced_non_call_site_code_reference_is_accepted_tradeoff``
    in test_cutover_gate_derivation.py for the pinned current behavior.
    A read failure yields False rather than raising.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    fm_match = _MD_FRONTMATTER_RE.match(text)
    if fm_match is not None:
        for line in fm_match.group(1).splitlines():
            if not _is_prose(line) and _has_whole_token_match(pattern, line):
                return True
    for fence_match in _MD_FENCE_RE.finditer(text):
        for line in fence_match.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if _has_whole_token_match(pattern, line):
                return True
    return False


def _scan_plain_text_file(file_path: Path, pattern: re.Pattern[str]) -> bool:
    """Return True iff ``pattern`` whole-token-matches anywhere in a raw text file.

    Fallback-only path for an AST-unparseable candidate (module docstring
    FIX-D — "a file the derivation can see but cannot parse must not
    vanish"), with two distinct callers whose match consequence differs:

      (a) an extensionless shebang-classified file whose Python AST parse
          failed — a `True` return here joins ``reader_ids`` and
          contributes normally to ``derived_ids``, same as any other
          reader match.
      (b) a ``.py``-suffixed file whose AST parse failed — a `True` return
          here instead joins ``unknown_ids`` (see
          ``_derive_value_vocabulary``), which the gate handler
          (``_cutover_gate``) treats as a hard block (INDETERMINATE), never
          a silent pass-through.

    Neither comment-stripping nor markdown-structure-awareness applies
    here — a syntax-broken file's comments/prose cannot be reliably
    distinguished from code, so this is a best-effort whole-token sweep
    over the whole file, not the precision this module applies to
    well-formed Python/JS/markdown. A read failure yields False rather
    than raising.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _has_whole_token_match(pattern, text)


def _scan_text_file(file_path: Path, pattern: re.Pattern[str]) -> bool:
    """Dispatch a non-Python candidate file to its language-specific scanner.

    ``.md`` -> ``_scan_markdown_file`` (frontmatter/fence only, never body
    prose). ``.js``/``.mjs`` -> ``_scan_js_file`` (comments stripped first).
    Anything else (the AST-unparseable-Python fallback case) ->
    ``_scan_plain_text_file`` (best-effort whole-token sweep, see that
    function's docstring for why it cannot apply the same precision).
    """
    if file_path.suffix == ".md":
        return _scan_markdown_file(file_path, pattern)
    if file_path.suffix in (".js", ".mjs"):
        return _scan_js_file(file_path, pattern)
    return _scan_plain_text_file(file_path, pattern)


def _iter_candidate_files(root: Path, rel_path: str) -> Iterable[Path]:
    """Yield every ``.py``/JS/markdown/shebang-Python file under ``root / rel_path``.

    ``rel_path`` is containment-checked via ``contained_path`` before any
    filesystem walk — an entry that escapes ``root`` (traversal, a symlink
    resolving outside the root) is silently skipped, never read; this
    function widens WHICH files are recognized as candidates, never WHERE the
    walk is allowed to look. A single file path (not a directory) yields
    itself iff it is a supported extension OR an extensionless Python-shebang
    file (``_is_python_candidate``); a directory is walked recursively under
    the same rule.
    """
    resolved = contained_path(root / rel_path, [root])
    if resolved is None:
        return
    if resolved.is_file():
        if resolved.suffix in _TEXT_SUFFIXES or _is_python_candidate(resolved):
            yield resolved
        return
    if resolved.is_dir():
        # Review: code-reviewer — rglob follows symlinked subdirectories, so a
        # symlink anywhere under this directory that resolves outside `root`
        # would otherwise be walked and read with no second containment
        # check. Re-gate every child through contained_path, mirroring the
        # top-level check above, rather than trusting the top-level check
        # alone to cover the whole recursive walk.
        for child in sorted(resolved.rglob("*")):
            if not child.is_file():
                continue
            if child.suffix not in _TEXT_SUFFIXES and not _is_python_candidate(child):
                continue
            if contained_path(child, [root]) is None:
                continue
            yield child


def _scan_repos(
    declared_repos: Sequence[Mapping[str, object]],
    repo_roots: Mapping[str, Path],
) -> tuple[list[dict[str, object]], dict[str, Path]]:
    """Annotate every declared repo with whether it was actually scanned.

    A repo is scanned iff it is NOT declared ``foreign: true`` on the record
    AND the caller supplied a resolvable local path for it in
    ``repo_roots``. Returns (annotated repo list, {repo: root} map of the
    subset actually scanned) — the annotated list is the C1 ``repos[]``
    shape plus a ``scanned`` field; PASS/REFUSE on a foreign/unscanned repo
    is C4b's decision, not this function's (see module docstring).
    """
    report: list[dict[str, object]] = []
    scanned_roots: dict[str, Path] = {}
    for entry in declared_repos:
        repo_name = str(entry.get("repo", ""))
        foreign = bool(entry.get("foreign", False))
        root = repo_roots.get(repo_name)
        scanned = (not foreign) and root is not None
        if scanned and root is not None:
            scanned_roots[repo_name] = root
        report.append({"repo": repo_name, "foreign": foreign, "scanned": scanned})
    return report, scanned_roots


def _derive_value_vocabulary(
    gate_source: Mapping[str, object],
    repo_roots: Mapping[str, Path],
) -> dict[str, object]:
    """Derive the value-vocabulary consumer set for one gate_source spec.

    Runs BOTH derivation modes unconditionally, over every reachable
    (non-foreign, resolvable) repo named in ``gate_source.repos[]``:

      1. Writer enumeration — Python AST string-literal occurrences in a
         write-shaped context (assignment / kwarg / dict literal).
      2. Hardcoded-enum-reader enumeration — Python AST string-literal
         occurrences in any other context, PLUS a supplementary
         ``cartography.edges.build_edges`` import/call edge-graph pass over
         the same Python files, PLUS a plain regex sweep over JS/markdown
         files the AST graph cannot parse at all.

    This is the kind's fixed mode set (Review: the Director of Engineering-cutover-review F2) — no
    per-record flag can disable either mode.
    """
    pattern_src = gate_source.get("pattern")
    if not pattern_src:
        raise ValueError("value-vocabulary gate_source requires a non-empty 'pattern'")
    pattern = re.compile(str(pattern_src))

    raw_paths = [str(p) for p in (gate_source.get("paths") or [])]
    declared_repos_raw = gate_source.get("repos") or []
    declared_repos: Sequence[Mapping[str, object]] = list(declared_repos_raw)  # type: ignore[arg-type]

    repo_scan_report, scanned_roots = _scan_repos(declared_repos, repo_roots)

    writer_ids: set[str] = set()
    reader_ids: set[str] = set()
    unknown_ids: set[str] = set()

    for repo_name, root in scanned_roots.items():
        python_files: list[Path] = []
        for rel_path in raw_paths:
            for file_path in _iter_candidate_files(root, rel_path):
                rel_id = f"{repo_name}:{file_path.relative_to(root).as_posix()}"
                has_py_suffix = file_path.suffix == ".py"
                if has_py_suffix or _is_python_candidate(file_path):
                    scanned = _scan_python_file(file_path, pattern)
                    if scanned is None:
                        # AST-unparseable. An extensionless shebang-classified
                        # file has no `.py` suffix to trust either way, so it
                        # falls back to the same regex reader pass the
                        # non-Python languages use (module docstring FIX-D:
                        # "a file the derivation can see but cannot parse must
                        # not vanish").
                        #
                        # A `.py`-suffixed file that fails to parse (syntax
                        # error, non-UTF-8 body — never a plain "not found",
                        # which `_scan_plain_text_file` also cannot read and
                        # so cannot false-positive on) is NOT silently
                        # dropped either (census fold-in constraint 2,
                        # state/memos/2026-07-25-census-requirement-folded-
                        # into-cutover-primitive.md item 2): if a best-effort
                        # raw-text sweep still finds the pattern as a
                        # whole-token match, this file is a candidate
                        # consumer the derivation CANNOT confidently classify
                        # as writer or reader — it is UNKNOWN, a distinct
                        # bucket from both "contributes nothing" (dead) and
                        # a confirmed writer/reader. Bucketing an
                        # unclassifiable match into "dead" (silently
                        # contributing nothing, the pre-existing shape) would
                        # launder the gate's own uncertainty into apparent
                        # completeness — exactly the failure the memo names.
                        # The gate handler (`_cutover_gate`) treats any
                        # non-empty `unknown_ids` as a hard block
                        # (INDETERMINATE), never a silent pass-through.
                        if has_py_suffix:
                            if _scan_plain_text_file(file_path, pattern):
                                unknown_ids.add(rel_id)
                        elif _scan_text_file(file_path, pattern):
                            reader_ids.add(rel_id)
                    else:
                        is_writer, is_reader = scanned
                        if is_writer:
                            writer_ids.add(rel_id)
                        if is_reader:
                            reader_ids.add(rel_id)
                        python_files.append(file_path)
                else:
                    if _scan_text_file(file_path, pattern):
                        reader_ids.add(rel_id)

        # Supplementary structural pass (Review: the Director of Engineering-cutover-review F2 /
        # cartography_edges.py precedent): the AST import/call edge graph is
        # additive to the literal-constant walk above, not a replacement for
        # it — a value-vocabulary pattern rarely equals a module/callee name,
        # so this pass is expected to contribute nothing on most records and
        # is not the primary Python signal; it exists so a record whose
        # pattern DOES name an importable/callable symbol is still caught.
        if python_files:
            try:
                graph = build_edges(root, python_files)
            except Exception:  # noqa: BLE001 — best-effort supplementary signal
                graph = None
            if graph is not None:
                for per_file in graph.get("edges", []):
                    if per_file.get("error"):
                        continue
                    for edge in per_file.get("edges", []):
                        target = str(edge.get("to", ""))
                        if target and _has_whole_token_match(pattern, target):
                            reader_ids.add(f"{repo_name}:{per_file['path']}")
                            break

    derived_ids = sorted(writer_ids | reader_ids)
    unknown_id_list = sorted(unknown_ids)
    return {
        "kind": "value-vocabulary",
        "derived_ids": derived_ids,
        "derived_count": len(derived_ids),
        "unknown_ids": unknown_id_list,
        "unknown_count": len(unknown_id_list),
        "repos": repo_scan_report,
    }


_KIND_HANDLERS = {
    "value-vocabulary": _derive_value_vocabulary,
}


def derive(gate_source: Mapping[str, object], repo_roots: Mapping[str, Path]) -> dict[str, object]:
    """Derive the current consumer set for one cutover record's gate_source.

    Args:
        gate_source: the record's ``gate_source`` object — ``kind`` (closed
            enum), ``pattern`` (regex string), ``paths`` (list of repo-relative
            path strings), ``repos`` (list of ``{repo, foreign}`` objects).
        repo_roots: map of repo-name -> local filesystem root for every repo
            this process can actually resolve (example-doctrine-repo + claude-klabauter
            ``coordinator/bin/``, per D2) — a repo absent from this map is
            treated as unscanned regardless of its declared ``foreign`` flag.

    Returns:
        {
            "kind": str,
            "derived_ids": [str, ...],   # sorted, de-duplicated "repo:relpath" ids
            "derived_count": int,
            "unknown_ids": [str, ...],   # candidate files the derivation could
                # NOT confidently classify as writer/reader (census fold-in
                # constraint 2) — currently only an AST-unparseable
                # `.py`-suffixed file whose raw text still whole-token-matches
                # `pattern`. Disjoint from `derived_ids`: a file here is
                # neither a confirmed writer/reader nor "contributes nothing"
                # — the caller (`cutover.gate`) must treat a non-empty
                # `unknown_ids` as a hard block, never a silent pass-through.
            "unknown_count": int,
            "repos": [{"repo": str, "foreign": bool, "scanned": bool}, ...],
        }

    Raises:
        UnsupportedGateSourceKind: ``gate_source["kind"]`` is not in
            SUPPORTED_KINDS.
        ValueError: a required field (e.g. ``pattern`` for
            ``value-vocabulary``) is missing or empty.
    """
    kind = gate_source.get("kind")
    handler = _KIND_HANDLERS.get(str(kind))
    if handler is None:
        raise UnsupportedGateSourceKind(
            f"gate_source.kind={kind!r} is not a supported derivation kind "
            f"(supported: {sorted(_KIND_HANDLERS)})"
        )
    return handler(gate_source, repo_roots)


class CutoverSchemaResolutionError(RuntimeError):
    """Raised when ``coordinator/schemas/cutover.schema.json`` cannot be read
    live from the example-doctrine-repo sibling clone (clone unresolvable, ``git show``
    fails, or the resolved content is not valid JSON)."""


def resolve_cutover_schema(
    doe_repo_path: str | Path | None = None, ref: str = "HEAD"
) -> dict[str, object]:
    """Resolve the example-doctrine-repo-side ``cutover.schema.json`` live, at call time.

    A distinct concern from derivation (``derive``, above) and from op
    wiring (C4b) — this module lands both because the schema is the shape
    ``cutover.gate``/``cutover.advance`` validate a record against before
    calling ``derive``, so it is a sibling responsibility of the same
    module, not a step inside either (Review: the Director of Engineering-cutover-review F14).

    Mirrors the established seam
    ``coordinator_core.frontmatter.schema_validate.check_schema_drift``
    (D2: "the gate reads it engine-side via
    ``git -C <doe> show HEAD:coordinator/schemas/…``, the established
    seam") — resolves the example-doctrine-repo clone root via
    ``coordinator_core.frontmatter.schema_drift_watch.resolve_doe_repo_path``
    (registry-first, DR-071; never a hardcoded path or a ``Path(__file__)``
    walk — that shape is exactly what
    ``coordinator_core/tests/test_no_hardcoded_paths.py`` gates against),
    then reads the schema at ``ref`` via ``git show`` — never a local
    vendored copy, since unlike the frontmatter schemas
    ``schema_drift_watch`` watches, this schema is read live rather than
    co-vendored (D2).

    Args:
        doe_repo_path: example-doctrine-repo clone root override. ``None`` (the
            default) resolves it via ``resolve_doe_repo_path()``.
        ref: git ref to read the schema at. Defaults to ``HEAD`` — the
            gate always validates against the example-doctrine-repo tip, not a pin.

    Returns:
        The parsed JSON Schema document as a dict.

    Raises:
        CutoverSchemaResolutionError: the example-doctrine-repo clone could not be resolved,
            ``git show`` exited non-zero (missing ref, schema absent at
            that ref, not a git repo), or the resolved content is not
            valid JSON.

    Negative-spec: does NOT vendor or cache the resolved schema — every
    call re-reads live, per D2's "the gate reads it engine-side" framing.
    Does NOT walk ``Path(__file__).parents`` to guess the example-doctrine-repo clone's
    location under any circumstance; the only resolution path is
    ``resolve_doe_repo_path()`` or an explicit caller-supplied
    ``doe_repo_path``.
    """
    root = Path(doe_repo_path) if doe_repo_path is not None else resolve_doe_repo_path()
    if root is None:
        raise CutoverSchemaResolutionError(
            "example-doctrine-repo sibling clone could not be resolved (REPO_EXAMPLE_DOCTRINE_REPO / "
            "registry repos.example_doctrine_repo / pointer files all absent) — cannot read "
            f"{_CUTOVER_SCHEMA_REPO_RELATIVE_PATH} at ref {ref!r}."
        )

    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{_CUTOVER_SCHEMA_REPO_RELATIVE_PATH}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        raise CutoverSchemaResolutionError(
            f"Cannot read example-doctrine-repo {ref}:{_CUTOVER_SCHEMA_REPO_RELATIVE_PATH} from "
            f"{root}: {result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CutoverSchemaResolutionError(
            f"example-doctrine-repo {ref}:{_CUTOVER_SCHEMA_REPO_RELATIVE_PATH} is not valid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# C4b — the "cutover.gate" op handler
#
# Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § C4b
# ---------------------------------------------------------------------------

#: Repo-name aliases this handler recognizes when building the repo_roots map
#: `derive()` needs. A cutover record's `gate_source.repos[].repo` string is
#: authored per-record (C3/C11-C17); several plausible spellings for the same
#: two repos ("example-doctrine-repo" vs "example-doctrine-repo", "claude-klabauter" vs "claude-klabauter") are
#: all pointed at the same resolved root so a record author's casing choice
#: doesn't silently fail to match. Extra unused aliases are harmless — derive()
#: (C4a::_scan_repos) only consults the aliases a record's gate_source.repos[]
#: actually names.
_DOE_ROOT_ALIASES: tuple[str, ...] = ("example-doctrine-repo", "example-doctrine-repo", "example_doctrine_repo")
_CLAUDE_KLABAUTER_ROOT_ALIASES: tuple[str, ...] = ("claude-klabauter", "claude_klabauter", "claude-klabauter")


def _build_repo_roots(doe_root: Path) -> dict[str, Path]:
    """Build the {repo-name-alias: local root} map `derive()` scans against.

    `doe_root` is the caller-resolved example-doctrine-repo worktree (this handler's own
    `repo_root`/`main_worktree_root()` — cutover records live example-doctrine-repo-side, D2).
    The claude-klabauter root is THIS repo's own root, derived via an in-repo
    `Path(__file__)` climb — safe under `test_no_hardcoded_paths.py`'s Tooth 2
    ("in-repo climbing alone, with no sibling-repo token anywhere in the
    construction, is explicitly fine"): no sibling-repo name string appears in
    this expression, unlike `schema_drift_watch.py`'s retired example-doctrine-repo-guessing
    climb this repo's own hardcoded-paths gate was written against.
    """
    claude_klabauter_root = Path(__file__).resolve().parents[2]
    roots: dict[str, Path] = {}
    for alias in _DOE_ROOT_ALIASES:
        roots[alias] = doe_root
    for alias in _CLAUDE_KLABAUTER_ROOT_ALIASES:
        roots[alias] = claude_klabauter_root
    return roots


def _dedup_roots(repo_roots: Mapping[str, Path]) -> list[Path]:
    """Return the distinct resolved roots in `repo_roots`, alias duplicates collapsed."""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for root in repo_roots.values():
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(root)
    return ordered


def _read_record(record_path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Read and parse a cutover record's YAML frontmatter.

    Returns (frontmatter_dict, None) on success, or (None, error_message) on
    any read/parse failure — mirrors `coordinator/bin/cutover-cli`'s own
    `confirm-consumer` read path (`split_frontmatter` + `yaml.safe_load`), the
    established shape for reading a cutover record without a hand-rolled
    regex fence-scan.
    """
    try:
        text = record_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {record_path}: {exc}"
    split = split_frontmatter(text)
    if split is None:
        return None, f"{record_path} has no parseable frontmatter"
    try:
        fm = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError as exc:
        return None, f"{record_path} frontmatter is not valid YAML: {exc}"
    if not isinstance(fm, dict):
        return None, f"{record_path} frontmatter is not a mapping"
    return fm, None


def _gate_reply(verdict_line: str, notes: list[str], exit_code: int, **extra: Any) -> dict:
    """Build the coverage_gate-shaped verdict envelope (verdict_line/notes/exit_code)."""
    reply: dict[str, Any] = {"verdict_line": verdict_line, "notes": notes, "exit_code": exit_code}
    reply.update(extra)
    return reply


# ---------------------------------------------------------------------------
# Signal-2 re-verification (Review: the Director of Engineering-cutover-review F7, PM-approved in
# full 2026-07-25) — for every confirmed_consumers entry, RE-VERIFY
# verified_by at call time rather than merely reading the stored claim.
# ---------------------------------------------------------------------------


def _reverify_test_node_id(ref: str, repo_roots: Mapping[str, Path]) -> tuple[bool, str]:
    """Re-run a pytest node id and report whether it still passes.

    A node id alone doesn't say which repo it lives in, so this tries every
    distinct resolved repo root in turn, using the first root whose test file
    actually exists on disk. Any subprocess failure or timeout is a REFUSE
    (never a silent pass) — an unreachable/errored re-run is exactly the
    "cannot re-verify" case AC11 requires to REFUSE.

    `env=pytest_child_env()`: the re-run's verdict decides a gate, and the repo
    root can be claude-klabauter itself, whose suite asserts the op registry at import
    time — a dispatch process's own lazy-ops setting reaching this child would
    turn a passing node id into a collection error and the gate into a REFUSE.

    Deliberate isolation boundary — do not convert to an in-process pytest
    invocation. Mechanism: pytest process isolation (also needs
    `pytest_child_env()` for env isolation). See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    file_part = ref.split("::", 1)[0]
    for root in _dedup_roots(repo_roots):
        candidate = root / file_part
        if not candidate.is_file():
            continue
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", ref, "-q", "--no-header"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                env=pytest_child_env(),
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"test-node-id {ref!r}: re-run under {root} failed to invoke: {exc}"
        if proc.returncode == 0:
            return True, f"test-node-id {ref!r}: PASS under {root}"
        return False, f"test-node-id {ref!r}: FAIL under {root} (pytest exit {proc.returncode})"
    return False, f"test-node-id {ref!r}: test file {file_part!r} not found under any known repo root"


def _reverify_commit_sha(ref: str, repo_roots: Mapping[str, Path]) -> tuple[bool, str]:
    """Confirm `ref` is a reachable commit object in any distinct known repo root.

    Uses `git cat-file -e <sha>^{commit}` — read-only, mirrors coverage.gate's
    affirmed-COMPUTE_ONLY git subprocess-reads precedent. A SHA reachable in
    no known repo (rewritten history, wrong repo, typo) is a REFUSE.
    """
    for root in _dedup_roots(repo_roots):
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", f"{ref}^{{commit}}"],
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return True, f"commit-sha {ref!r}: reachable in {root}"
    return False, f"commit-sha {ref!r}: not reachable in any known repo root"


#: Filename shape a `sibling-commitment-ref` ref must match — mirrors the
#: real `state/cross-repo-commitments/*.yaml` naming convention on disk
#: (dated-slug-hash `.yaml`; see e.g.
#: `state/cross-repo-commitments/2026-07-21-claude-klabauter-to-land-c9-a11-scoped-to-
#: survives-7428f9933120.yaml`) and the schema's own allOf-enforced ref
#: pattern (`coordinator/schemas/cutover.schema.json`) — kept in lockstep
#: with `_CUTOVER_SIBLING_COMMITMENT_REF_RE` in
#: `coordinator_core/frontmatter/schema_validate.py`; a divergence between
#: the two is the exact "coupling declared in one and absent in the other"
#: defect this plan's own C8 already names.
_SIBLING_COMMITMENT_REF_RE = re.compile(
    r"^(state/cross-repo-commitments/)?\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*\.yaml$"
)

#: `status` values on a `state/cross-repo-commitments/*.yaml` record that
#: genuinely attest the sibling's confirmation has landed. `open` is a
#: standing, not-yet-fulfilled obligation (schema:
#: `coordinator/schemas/cross-repo-commitment.schema.json`) — it names an
#: intent, not evidence the consumer now handles the new form; `withdrawn`
#: is an explicit non-attestation. Only `fulfilled` counts.
_SIBLING_COMMITMENT_FULFILLED_STATUS = "fulfilled"


def _reverify_sibling_commitment_ref(ref: str, doe_root: Optional[Path]) -> tuple[bool, str]:
    """Resolve `ref` to a `state/cross-repo-commitments/*.yaml` record on OUR
    OWN disk and confirm it genuinely attests the sibling's confirmation.

    This is what makes signal-2 re-verifiable for a foreign-sourced
    `confirmed_consumers` entry at all (state/improvement-queue/2026-07-25-
    consume-a-fleet-token-sweep-capability-f-b0717d124c91.yaml P1;
    docs/plans/2026-07-25-cutover-state-machine.md § C8's FK relationship):
    the EVIDENCE the sibling repo confirmed may live entirely in a foreign
    repo the local engine cannot scan, but the COMMITMENT RECORD attesting
    to it is a example-doctrine-repo-local artifact (§ Cross-repo obligations,
    coordinator/docs/wiki/cutover-state-machine.md) — so this re-verification
    reads local disk only, same posture as the other three kinds.

    A missing record, an unparseable record, a record that isn't a mapping,
    or a record whose `status` is not `fulfilled` is a REFUSE — never a pass.
    Never re-derives or trusts the sibling's own claim beyond what the
    commitment record itself states; a `fulfilled` status is the sibling
    EM's own attestation, authored and closed independently of this call
    (the commitment record is the sibling-sourced signal; this function only
    reads it).
    """
    if doe_root is None:
        return False, f"sibling-commitment-ref {ref!r}: no example-doctrine-repo repo root available to resolve against"
    if not _SIBLING_COMMITMENT_REF_RE.match(ref):
        return False, f"sibling-commitment-ref {ref!r}: does not match the cross-repo-commitment filename shape"

    relative = ref
    prefix = "state/cross-repo-commitments/"
    if relative.startswith(prefix):
        relative = relative[len(prefix):]
    candidate = doe_root / "state" / "cross-repo-commitments" / relative
    resolved = contained_path(candidate, [doe_root])
    if resolved is None or not resolved.is_file():
        return False, f"sibling-commitment-ref {ref!r}: commitment record not found at {candidate}"

    try:
        text = resolved.read_text(encoding="utf-8")
        commitment = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        return False, f"sibling-commitment-ref {ref!r}: commitment record unparseable: {exc}"
    if not isinstance(commitment, Mapping):
        return False, f"sibling-commitment-ref {ref!r}: commitment record is not a mapping"

    status = commitment.get("status")
    if status != _SIBLING_COMMITMENT_FULFILLED_STATUS:
        return (
            False,
            f"sibling-commitment-ref {ref!r}: commitment status is {status!r}, not "
            f"{_SIBLING_COMMITMENT_FULFILLED_STATUS!r} — does not attest the sibling has "
            "confirmed the consumer handles the new form",
        )
    return True, f"sibling-commitment-ref {ref!r}: commitment {resolved.name} is fulfilled"


async def _reverify_probe_op_key(ref: str, repo_root: Optional[Path]) -> tuple[bool, str]:
    """Invoke a registered op by key in-process and check its result.

    Mirrors `coordinator_core/ops/ceremony/tail_ops.py::run_coverage_gate`'s
    own in-process op-invocation shape (`get_op_handler` + await), itself a
    COMPUTE_ONLY-compatible precedent (`coverage.gate`). An unregistered op
    key, an invocation that raises, or a result carrying a non-zero
    `exit_code` is a REFUSE — this handler never assumes success from silence.
    """
    handler = get_op_handler(ref)
    if handler is None:
        return False, f"probe-op-key {ref!r}: op not registered"
    try:
        result = handler({}, repo_root=repo_root)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 — any probe failure is a REFUSE, not a crash
        return False, f"probe-op-key {ref!r}: invocation raised {exc!r}"
    if isinstance(result, Mapping) and "exit_code" in result:
        ec = result.get("exit_code")
        if ec not in (0, None):
            return False, f"probe-op-key {ref!r}: returned exit_code={ec!r}"
    return True, f"probe-op-key {ref!r}: OK"


async def _reverify_confirmed_consumer(
    entry: Mapping[str, object],
    repo_roots: Mapping[str, Path],
    repo_root: Optional[Path],
) -> tuple[bool, str]:
    """Dispatch signal-2 re-verification for one confirmed_consumers[] entry by verified_by.kind.

    A malformed entry (no verified_by, unrecognized kind, empty ref) is a
    REFUSE, same as a re-verification that actually ran and failed — schema
    admissibility (C1) is a separate, earlier layer; this handler does not
    assume the schema has already validated the record it is reading.
    """
    consumer_id = str(entry.get("id", "<unknown>"))
    verified_by = entry.get("verified_by")
    if not isinstance(verified_by, Mapping):
        return False, f"{consumer_id}: verified_by is missing or malformed"
    kind = str(verified_by.get("kind", ""))
    ref = str(verified_by.get("ref", ""))
    if not ref:
        return False, f"{consumer_id}: verified_by.ref is empty"

    if kind == "test-node-id":
        ok, detail = await asyncio.to_thread(_reverify_test_node_id, ref, repo_roots)
    elif kind == "commit-sha":
        ok, detail = await asyncio.to_thread(_reverify_commit_sha, ref, repo_roots)
    elif kind == "probe-op-key":
        ok, detail = await _reverify_probe_op_key(ref, repo_root)
    elif kind == "sibling-commitment-ref":
        ok, detail = _reverify_sibling_commitment_ref(ref, repo_root)
    else:
        return False, f"{consumer_id}: unrecognized verified_by.kind {kind!r}"
    return ok, f"{consumer_id}: {detail}"


@register_op("cutover.gate")
async def _cutover_gate(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'cutover.gate' handler — the two-way AGREEMENT test (F1).

    Reads the record named by `params["record"]`, re-derives its consumer set
    via `derive()` AT CALL TIME (never trusts the stored `confirmed_consumers`
    list — § The load-bearing design decision), and evaluates, in order:

      0. `derive(gate_source).unknown_ids == []` — census fold-in constraint
         2 (state/memos/2026-07-25-census-requirement-folded-into-cutover-
         primitive.md item 2): a candidate the derivation could not
         confidently classify as writer or reader is `unknown`, a
         FIRST-CLASS blocking verdict — never silently bucketed into "dead"
         (contributes nothing) and never allowed to reach the subset checks
         below, which only ever reason about `derived_ids`. INDETERMINATE,
         same posture as an empty derived set (clause 1) and the
         foreign-repo/signal-2 fail-closed clauses (4, 5) below — the gate
         would rather block a phase advance than launder uncertainty into
         an apparent PASS.
      1. `derive(gate_source) != empty` — an empty derived set is
         INDETERMINATE, never PASS.
      2. `confirmed_consumers ⊆ derive(gate_source)` — a claimed consumer the
         derivation cannot re-find is a REFUSE (a defect in gate_source, not
         evidence of coverage).
      2b. `derive(gate_source) ⊆ confirmed_consumers` — the OTHER half of the
          two-way AGREEMENT test and the primitive's actual safety property
          (AC5(a)): a derived consumer with no matching `confirmed_consumers`
          entry is a REFUSE. Clause 2 alone (subset-of-derived) is the
          gate_source-narrowness check F1 layered ON TOP of this — it never
          replaces it; a record can satisfy clause 2 vacuously (few or no
          confirmed entries) while most of its real, currently-derived
          consumer set has never been confirmed by anyone.
      3. The derived cardinality has not shrunk since the record's own
         previous `derivation_history` entry, absent an explicit
         `derivation_narrowed_reason` on that prior entry.
      4. Foreign-repo fail-closed (F5): any `gate_source.repos[]` entry marked
         `foreign: true` with no sibling-sourced confirmation (no
         `confirmed_consumers[].id` prefixed `"<repo>:"`) is INDETERMINATE.
      5. Signal-2 (F7): every `confirmed_consumers[].verified_by` is
         RE-VERIFIED (pytest re-run / op re-invoke / git-reachability check
         per its `kind`) — any that cannot be re-verified is INDETERMINATE.

    Only when all of the above hold does the verdict PASS.

    Params:
        record (str, required) — path to the cutover record markdown file,
            absolute or relative to the resolved example-doctrine-repo worktree.

    Returns (coverage_gate-shaped verdict envelope):
        {
            "verdict_line": str,   # "surface=... phase=... derived_count=N VERDICT=..."
            "notes":        list,  # diagnostic notes, one per evaluated agreement leg
            "exit_code":    int,   # 0 PASS, 2 REFUSE/INDETERMINATE (HALT), 1 setup error
            "derivation_history_entry": dict,  # {phase, derived_count, derived_ids, at} —
                # COMPUTED, not written to the record; cutover.advance (C5, MUTATING)
                # is what persists it (see this module's C4b docstring negative-spec,
                # point 3 of the DR-208 affirmation above).
        }

    Negative-spec:
      - Does NOT write anything to the record file, or any other file —
        COMPUTE_ONLY (see the DR-208 five-question affirmation, module
        header, above).
      - Does NOT treat a subset-containment pass as sufficient — an empty
        derived set is checked FIRST and refuses before the subset check can
        be vacuously satisfied (F1).
      - Does NOT stop at confirmed-subset-of-derived (clause 2). A record
        with a narrow confirmed_consumers list and a wide derived set is a
        REFUSE at clause 2b, not a PASS — comparing only one direction is
        exactly the "compare-the-record-list-against-the-record-other-list"
        theatre the plan's Anti-scope warns against.
      - Does NOT trust `confirmed_consumers[].verified_by` as read off disk —
        every entry is re-verified at call time (F7); reading it without
        re-checking is exactly the prior (rejected) design this replaces.
    """
    if repo_root is None:
        return _gate_reply(
            "",
            ["cutover.gate: repo_root is required (common_dir scope) — no repo context supplied"],
            1,
        )
    worktree = main_worktree_root(repo_root)

    raw_record = str(params.get("record") or "").strip()
    if not raw_record:
        return _gate_reply("", ["cutover.gate: 'record' param is required"], 1)

    candidate = Path(raw_record)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    resolved = contained_path(candidate, [worktree])
    if resolved is None or not resolved.is_file():
        return _gate_reply(
            "",
            [f"cutover.gate: record path {raw_record!r} not found or escapes {worktree}"],
            1,
        )

    fm, err = _read_record(resolved)
    if err is not None or fm is None:
        return _gate_reply("", [f"cutover.gate: {err}"], 1)

    phase = fm.get("phase")
    gate_source = fm.get("gate_source")
    surface = str(fm.get("surface", ""))

    confirmed_consumers_raw = fm.get("confirmed_consumers")
    if confirmed_consumers_raw is None:
        confirmed_consumers_raw = []
    if not isinstance(confirmed_consumers_raw, list):
        return _gate_reply("", ["cutover.gate: confirmed_consumers is not a list"], 1)

    prior_history_raw = fm.get("derivation_history")
    if not isinstance(prior_history_raw, list):
        prior_history_raw = []

    if not isinstance(gate_source, Mapping):
        return _gate_reply("", ["cutover.gate: gate_source is missing or malformed"], 1)

    repo_roots = _build_repo_roots(worktree)

    try:
        derivation = derive(gate_source, repo_roots)
    except (UnsupportedGateSourceKind, ValueError) as exc:
        return _gate_reply(
            f"surface={surface!r} phase={phase!r} VERDICT=INDETERMINATE",
            [f"cutover.gate: derivation failed: {exc}"],
            2,
        )

    derived_ids = list(derivation.get("derived_ids") or [])
    derived_count = int(derivation.get("derived_count") or 0)
    unknown_ids = list(derivation.get("unknown_ids") or [])
    unknown_count = int(derivation.get("unknown_count") or 0)
    repo_scan_report = derivation.get("repos") or []

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    history_entry = {
        "phase": phase,
        "derived_count": derived_count,
        "derived_ids": derived_ids,
        "at": now_iso,
    }

    header = f"surface={surface!r} phase={phase!r} derived_count={derived_count}"
    notes: list[str] = []

    # (0) unknown candidates — census fold-in constraint 2: a candidate the
    # derivation could not confidently classify as writer or reader is a
    # FIRST-CLASS blocking verdict, never silently bucketed into "dead"
    # (contributes nothing to derived_ids) and never allowed to reach the
    # subset checks below. INDETERMINATE, same posture as an empty derived
    # set and the foreign-repo/signal-2 fail-closed clauses.
    if unknown_count > 0:
        notes.append(
            # Review: code-reviewer — F3: unknown_ids is already sorted by
            # `_derive_value_vocabulary` before it reaches `derivation`; re-sorting
            # here was a harmless but pointless no-op.
            f"cutover.gate: derivation could NOT classify {unknown_ids} as writer "
            "or reader (AST-unparseable, matched by raw text) — unknown is a first-class "
            "blocking verdict, never bucketed into 'dead'/contributes-nothing — "
            "INDETERMINATE, not PASS (census fold-in constraint 2)"
        )
        return _gate_reply(
            f"{header} VERDICT=INDETERMINATE", notes, 2, derivation_history_entry=history_entry
        )

    # (1) empty derived set — INDETERMINATE, never PASS (F1).
    if derived_count == 0:
        notes.append(
            "cutover.gate: derived consumer set is EMPTY — INDETERMINATE, not PASS "
            "(an empty derivation proves nothing; two-way AGREEMENT test, not a subset "
            "check that an empty set would vacuously satisfy — § The load-bearing design "
            "decision, Review: the Director of Engineering-cutover-review F1)"
        )
        return _gate_reply(
            f"{header} VERDICT=INDETERMINATE", notes, 2, derivation_history_entry=history_entry
        )

    # (2) confirmed_consumers ⊆ derive(gate_source) — a claimed consumer the
    # derivation cannot re-find is a defect in gate_source, not coverage.
    #
    # Records author confirmed_consumers[].id as a BARE repo-relative path
    # (human-meaningful, no repo prefix — see cutovers/closed-reason-terminal.md);
    # derive() emits "<repo>:<relpath>" ids (module docstring, derive() Returns).
    # Canonicalization is the GATE's job, not the record's: a bare relpath
    # matches a derived id when the relpath halves agree; a repo-qualified id
    # ("<repo>:<relpath>") must match BOTH halves, never falling back to a bare
    # match — qualification is honoured, never ignored. A bare relpath that
    # matches derived ids in more than one repo is AMBIGUOUS and REFUSEs
    # rather than silently picking one (guards match conditions, not
    # containers).
    derived_id_set = set(derived_ids)
    relpath_to_repos: dict[str, set[str]] = {}
    for did in derived_ids:
        repo_part, _, relpath_part = did.partition(":")
        relpath_to_repos.setdefault(relpath_part, set()).add(repo_part)

    confirmed_ids: list[str] = []
    unresolvable_confirmed: list[str] = []
    ambiguous_confirmed: list[str] = []
    canonical_confirmed_ids: set[str] = set()
    for entry in confirmed_consumers_raw:
        if not isinstance(entry, Mapping):
            unresolvable_confirmed.append(str(entry))
            continue
        cid = str(entry.get("id", ""))
        confirmed_ids.append(cid)
        if ":" in cid:
            if cid not in derived_id_set:
                unresolvable_confirmed.append(cid)
            else:
                canonical_confirmed_ids.add(cid)
        else:
            matching_repos = relpath_to_repos.get(cid, set())
            if not matching_repos:
                unresolvable_confirmed.append(cid)
            elif len(matching_repos) > 1:
                ambiguous_confirmed.append(cid)
            else:
                (only_repo,) = matching_repos
                canonical_confirmed_ids.add(f"{only_repo}:{cid}")

    if ambiguous_confirmed:
        details = ", ".join(
            f"{cid!r} matches repos {sorted(relpath_to_repos[cid])}" for cid in sorted(ambiguous_confirmed)
        )
        notes.append(
            "cutover.gate: confirmed_consumers id(s) matched derived ids in more than "
            f"one repo — bare relpath is ambiguous: {details}. Repo-qualify the id "
            "(\"<repo>:<relpath>\") to disambiguate; the gate never silently picks a repo."
        )
    if unresolvable_confirmed:
        notes.append(
            "cutover.gate: confirmed_consumers claims id(s) the derivation cannot "
            f"re-find: {sorted(set(unresolvable_confirmed))} — this proves gate_source "
            "is narrower than known truth, a defect in gate_source, not evidence of "
            "coverage (two-way AGREEMENT, not a subset check)"
        )
    if ambiguous_confirmed or unresolvable_confirmed:
        return _gate_reply(f"{header} VERDICT=REFUSE", notes, 2, derivation_history_entry=history_entry)

    # (2b) derive(gate_source) ⊆ confirmed_consumers — the OTHER half of the
    # two-way AGREEMENT test (AC5(a); the safety property this whole primitive
    # exists to enforce: DO NOT ADVANCE A PHASE UNTIL EVERY CONSUMER IS
    # CONFIRMED). Clause (2) above only ever checked confirmed-subset-of-derived
    # (the gate_source-narrowness check F1 added on top of the base property);
    # a derived id absent from confirmed_consumers means a real, currently-live
    # consumer has never been confirmed by anyone — REFUSE, never PASS. Uses
    # the SAME canonicalization as clause (2): a bare confirmed relpath matches
    # a derived id when the relpath halves agree (and the match is unambiguous,
    # already enforced above); a repo-qualified confirmed id must match both
    # halves. Comparing two ID LISTS is exactly the "compare-the-record-list-
    # against-the-record-other-list" theatre the plan's Anti-scope warns
    # against; what makes this NOT that is that one side (derived_ids) is
    # re-computed at call time from disk truth, never read off the record.
    unconfirmed_derived = sorted(derived_id_set - canonical_confirmed_ids)
    if unconfirmed_derived:
        notes.append(
            "cutover.gate: derivation found consumer(s) with NO matching "
            f"confirmed_consumers entry: {unconfirmed_derived} — REFUSE (a currently-"
            "derived consumer must be explicitly confirmed before this phase can "
            "advance; the derivation finding a consumer is not itself confirmation). "
            "To confirm: add a confirmed_consumers entry for each id above with a "
            "re-verifiable verified_by (test-node-id / commit-sha / probe-op-key / "
            "sibling-commitment-ref) — "
            "see AC5(a) and the C6 regression test, "
            "docs/plans/2026-07-25-cutover-state-machine.md."
        )
        return _gate_reply(f"{header} VERDICT=REFUSE", notes, 2, derivation_history_entry=history_entry)

    # (3) narrowing check against the record's own prior derivation_history.
    prior_count: Optional[int] = None
    prior_narrowed_reason: object = None
    for prior_entry in reversed(prior_history_raw):
        if isinstance(prior_entry, Mapping) and "derived_count" in prior_entry:
            try:
                prior_count = int(prior_entry["derived_count"])
            except (TypeError, ValueError):
                prior_count = None
            else:
                prior_narrowed_reason = prior_entry.get("derivation_narrowed_reason")
            break
    if prior_count is not None and derived_count < prior_count and not prior_narrowed_reason:
        notes.append(
            f"cutover.gate: derived_count SHRANK ({prior_count} -> {derived_count}) since "
            "the previous call with no derivation_narrowed_reason recorded — REFUSE "
            "(narrowing must be visible and explained, never silent)"
        )
        return _gate_reply(f"{header} VERDICT=REFUSE", notes, 2, derivation_history_entry=history_entry)

    # (4) foreign-repo fail-closed (F5): a foreign, unscanned repo with no
    # sibling-sourced confirmation is INDETERMINATE, never PASS.
    foreign_unconfirmed: list[str] = []
    for repo_entry in repo_scan_report:
        if not isinstance(repo_entry, Mapping) or not repo_entry.get("foreign"):
            continue
        repo_name = str(repo_entry.get("repo", ""))
        has_sibling_confirmation = any(
            isinstance(e, Mapping) and str(e.get("id", "")).startswith(f"{repo_name}:")
            for e in confirmed_consumers_raw
        )
        if not has_sibling_confirmation:
            foreign_unconfirmed.append(repo_name)

    if foreign_unconfirmed:
        notes.append(
            f"cutover.gate: foreign repo(s) {foreign_unconfirmed} are unscanned and carry "
            "no sibling-sourced confirmation in confirmed_consumers — INDETERMINATE, never "
            "PASS (fail-closed on 'cannot see', Review: the Director of Engineering-cutover-review F5)"
        )
        return _gate_reply(
            f"{header} VERDICT=INDETERMINATE", notes, 2, derivation_history_entry=history_entry
        )

    # (5) signal-2 re-verification (F7): re-check every verified_by at call
    # time rather than trusting the stored claim.
    unverifiable: list[str] = []
    for entry in confirmed_consumers_raw:
        if not isinstance(entry, Mapping):
            continue
        ok, detail = await _reverify_confirmed_consumer(entry, repo_roots, worktree)
        notes.append(f"cutover.gate: signal-2 {detail}")
        if not ok:
            unverifiable.append(str(entry.get("id", "<unknown>")))

    if unverifiable:
        notes.append(
            f"cutover.gate: verified_by re-verification FAILED for {unverifiable} — "
            "INDETERMINATE, never a pass (the gate cannot re-verify this consumer's own claim)"
        )
        return _gate_reply(
            f"{header} VERDICT=INDETERMINATE", notes, 2, derivation_history_entry=history_entry
        )

    notes.append(
        f"cutover.gate: two-way agreement holds — confirmed {sorted(confirmed_ids)} all "
        f"re-found in derived {sorted(derived_id_set)}, AND every derived id is matched by "
        "a confirmed_consumers entry (no unconfirmed derived consumer); all verified_by "
        "entries re-verified"
    )
    return _gate_reply(f"{header} VERDICT=PASS", notes, 0, derivation_history_entry=history_entry)
