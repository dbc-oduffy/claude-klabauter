"""
coordinator_core.frontmatter.primitives

Unified Python port of the YAML frontmatter text-manipulation primitives from
the DoE-claude coordinator JS tools:
  - handoff-transition.js       (4-arg anchored insertFmField, block-scalar guard)
  - stamp-shipped-in.js         (numeric/scientific quoting additions)
  - normalize-handoff-frontmatter.js (2-arg append-only insertFmField, null→'null')

Text-based throughout (no pyyaml for write) — preserves byte-identical output with the
JS originals so round-trip diffs are zero outside the mutated fields.

Spec backlinks:
  coordinator/bin/handoff-transition.js
  coordinator/bin/stamp-shipped-in.js
  coordinator/bin/normalize-handoff-frontmatter.js

Public surface (imported by C2/C3/C4/C5 executors):
  split_frontmatter(text)                              → FrontmatterSplit | None
  read_fm_field(fm, key)                               → str | None
  unquote_yaml_scalar(raw)                             → str | None
  read_fm_field_unquoted(fm, key)                      → str | None
  serialize_yaml_scalar(v, *, numeric_quoting=False)   → str
  replace_fm_field(fm, key, v)                         → str
  insert_fm_field(fm, key, v, after_key=None)          → str
  insert_fm_field_raw(fm, key, raw_value, after_key=None) → str
  remove_fm_field(fm, key)                             → str
  read_fm_nested_field(fm, key)                        → str | None
  write_fm_nested_field(fm, key, block_text)           → str
  remove_fm_nested_field(fm, key)                      → str
  rebuild(split, fm_text)                              → str
  frontmatter_body_text(file_text)                     → str
  git_blob_sha1(text)                                  → str | None
  canonical_body_sha(file_text)                        → str | None

read_fm_nested_field/write_fm_nested_field/remove_fm_nested_field (AC11, eng-director
F1, break-class) extend the toolkit to a YAML sequence-of-mappings value — the
`gate_evidence:`/`carried_items:` shape, an indented `- kind: ...\n  repo: ...` block
under its key line. Spec backlink:
`docs/plans/2026-07-26-structured-sibling-evidence-gates.md` § C0. The single-line
helpers above (`replace_fm_field`/`remove_fm_field`) carry a matching guard (the Staff Engineer
F4): a key that reads back empty via `read_fm_field` with a more-indented next line is
this same nested-block shape, and calling the single-line helper on it would silently
orphan the indented continuation lines — the guard makes that a mechanical `ValueError`
instead of a rule a caller has to remember.

frontmatter_body_text/git_blob_sha1/canonical_body_sha are the shared
plan-body-hash recipe extracted from two independent hand-maintained copies
(`coordinator_core.pickup_assemble._frontmatter_body_text`/
`_git_hash_object_stdin` and `coordinator_core.review_assemble.exec_auth_stamp.
_canonical_body_sha`) — both computed the same canonical `git hash-object
--stdin`-over-plan-body recipe independently; a one-sided drift between them
would silently break execution-authorization-staleness detection (Review:
code-reviewer — Finding 3, `state/subagent-share/e180604f-9221-4f7e-8fe2-0f9b4bb279a6/
2026-07-25-codereview-slicephase1-engine-layer-coordinator-core.md`).
`git_blob_sha1` is the literal git blob-hash algorithm
(`sha1("blob " + len(content) + "\\0" + content)`), computed in-process —
verified byte-identical to a real `git hash-object --stdin` subprocess call
across multiple samples; no subprocess spawn needed.
"""
from __future__ import annotations

import hashlib
import re
from typing import NamedTuple, Optional


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

class FrontmatterSplit(NamedTuple):
    """Parsed frontmatter components.

    Negative-spec: body_with_leading_newline retains the newline that follows
    the closing ``---`` so that rebuild() produces byte-identical output.
    """

    preamble: str
    """Leading blank lines and HTML comment blocks before the opening ``---``.
    Preserved verbatim on rebuild."""

    fm_text: str
    """Raw text between the ``---`` delimiters (may or may not end in ``\\n``)."""

    body_with_leading_newline: str
    """Everything after the closing ``---``, including its trailing newline."""


# ---------------------------------------------------------------------------
# Internal compiled patterns
# ---------------------------------------------------------------------------

# Preamble: one or more of:
#   - blank line (only spaces/tabs before the newline)
#   - complete HTML comment block on its own line(s)
# JS original: /^(?:[ \t]*\r?\n|[ \t]*<!--[\s\S]*?-->[ \t]*\r?\n?)+/
_PREAMBLE_RE = re.compile(
    r'^(?:[ \t]*\r?\n|[ \t]*<!--[\s\S]*?-->[ \t]*\r?\n?)+'
)

# Closing --- (horizontal whitespace only after, NOT \s which eats blank body lines)
_CLOSE_RE = re.compile(r'^---[ \t]*$', re.MULTILINE)

# YAML structural characters that require quoting
_STRUCTURAL_RE = re.compile(r'[#:{}\[\],&*!|>"\'%@`]')

# All-digit integer — SHA-as-int / plain integer defense (stamp-shipped-in F0)
_ALL_NUMERIC_RE = re.compile(r'^[0-9]+$')

# YAML 1.1 scientific-notation float (e.g. '1958e194') — YAML 1.1 auto-coerce defense
_SCIENTIFIC_RE = re.compile(r'^[0-9]+[eE][0-9]+$')


# ---------------------------------------------------------------------------
# split_frontmatter
# ---------------------------------------------------------------------------

def split_frontmatter(text: str) -> FrontmatterSplit | None:
    """Parse YAML frontmatter from file content.

    Normalises CRLF to LF on entry (DR-148 cross-platform portability). Tolerates
    a leading preamble (blank lines and/or HTML comment blocks) before the opening
    ``---``; the preamble is captured verbatim and reassembled on rebuild.

    Returns ``None`` when no valid frontmatter block is found (missing ``---``
    delimiters, no closing ``---``, or unparseable preamble).
    """
    # CRLF normalize — JS: text.replace(/\r\n/g, '\n')
    text = text.replace('\r\n', '\n')

    preamble = ''
    # Review: code-reviewer — F3: JS uses /^---\s*\n/ so `---yaml` is rejected; tighten to regex
    if not re.match(r'^---[ \t]*\n', text):
        m = _PREAMBLE_RE.match(text)
        if not m:
            return None
        after = text[m.end():]
        if not re.match(r'^---[ \t]*\n', after):
            return None
        preamble = m.group(0)
        text = after

    # text now begins with ---
    after_first = text[3:]
    first_newline = after_first.find('\n')
    if first_newline == -1:
        return None
    rest = after_first[first_newline + 1:]

    close_match = _CLOSE_RE.search(rest)
    if not close_match:
        return None

    fm_text = rest[: close_match.start()]
    body_start = close_match.start() + len(close_match.group(0))
    body_with_leading_newline = rest[body_start:]

    return FrontmatterSplit(
        preamble=preamble,
        fm_text=fm_text,
        body_with_leading_newline=body_with_leading_newline,
    )


# ---------------------------------------------------------------------------
# read_fm_field
# ---------------------------------------------------------------------------

def read_fm_field(fm: str, key: str) -> str | None:
    """Return the trimmed value of ``key:`` in frontmatter text, or ``None``.

    The boundary lookahead ``(?=[ \\t]|\\r?$)`` prevents ``status`` from
    matching ``status_message:`` (code-reviewer A1 fix from
    handoff-transition.js). The ``\\r?`` half admits a CRLF-authored
    present-but-empty ``key:\\r\\n`` — see the CRLF note below.

    Negative-spec (break-class fix, 2026-07-28 — do not "simplify" the
    horizontal-whitespace classes back to ``\\s``): the padding around the
    captured value is ``[ \\t]``, never ``\\s``, because ``\\s`` matches a
    NEWLINE. With ``\\s*`` the pattern walked past the line break of a
    present-but-empty key and returned the FOLLOWING LINE's content — so
    ``read_fm_field("blocking_notes:\\nstatus: open\\n", "blocking_notes")``
    read back ``"status: open"``, and a ``replace_fm_field`` driven by that
    reading overwrote the ``status:`` line, silently destroying an unrelated
    field. A present-but-empty key now reads as ``""`` (falsy) and an absent
    key still reads as ``None``; that None-vs-empty distinction is meaningful
    for the first time. The trailing ``\\r?`` keeps a CRLF-authored document's
    carriage return out of the captured value (Windows is first-class).

    CRLF (2026-07-28 — the residual left open by the fix above, now closed).
    The boundary lookahead used to be ``(?=[ \\t]|$)``, which rejects the
    ``\\r`` of a present-but-empty ``key:\\r\\n``: the character after the
    colon is neither ``[ \\t]`` nor a MULTILINE ``$``, so on a CRLF-authored
    document such a key read as ABSENT rather than empty. Widening it to
    ``\\r?$`` admits exactly that one position and nothing else — the
    ``status``/``status_message:`` guarantee above is unaffected, because in a
    well-formed LF- or CRLF-authored document a ``\\r`` appears only
    immediately before the ``\\n`` that ends its line. (A malformed file CAN
    carry a lone mid-line ``\\r``; do not reason from this as a universal. It
    does not weaken the guarantee here, because the widened alternative is
    ``\\r?$`` — anchored — so a mid-line ``\\r`` still fails the lookahead.)

    Negative-spec: this lookahead is shared VERBATIM by five key-resolution
    patterns — ``read_fm_field``, ``replace_fm_field``, ``remove_fm_field``,
    ``_fm_key_line_pattern`` and ``insert_fm_field``'s anchor — and any
    future change to it belongs in all five at once. Narrowing it back here
    alone would be worse than the original gap, not better: a read would go
    on succeeding where the matching write silently no-ops, turning a
    consistent blind spot into a read/write disagreement.
    """
    pattern = re.compile(
        r'^' + re.escape(key) + r':(?=[ \t]|\r?$)[ \t]*(.*?)[ \t]*\r?$',
        re.MULTILINE,
    )
    m = pattern.search(fm)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# unquote_yaml_scalar / read_fm_field_unquoted
# ---------------------------------------------------------------------------

def unquote_yaml_scalar(raw: str | None) -> str | None:
    """Invert ``serialize_yaml_scalar``'s quoting for a single-line scalar.

    ``read_fm_field`` is a raw text extractor and deliberately returns the
    on-disk bytes after ``key:``, quotes included. ``serialize_yaml_scalar``
    quotes on structural characters and — under ``numeric_quoting=True`` — on
    all-digit and YAML-1.1 scientific-notation values (the SHA-as-number
    defence). The pair is therefore write/read asymmetric: a value written as
    ``shipped_in: '44379324'`` reads back as ``"'44379324'"``. This function
    closes that asymmetry for callers that COMPARE or PARSE the value.

    Handles:
    - a matched pair of surrounding single-quotes, unescaping YAML's doubled
      ``''`` inner-quote form (the exact inverse of ``serialize_yaml_scalar``);
    - a matched pair of surrounding double-quotes (plain strip) — not emitted by
      ``serialize_yaml_scalar``, but present in hand-authored frontmatter and in
      artifacts written by the DoE node oracle's ``schema.js`` ``unquoteScalar``,
      whose behaviour this mirrors.

    Negative-spec: NOT a general YAML unquoter. It does not process
    double-quoted backslash escapes (``\\n``, ``\\"`` — see
    ``ops/fleet/memo_compose._unquote`` for that distinct outbox scheme), and it
    does not handle block scalars or multi-line values. A value that merely
    happens to begin and end with a quote character is only altered when the
    quotes form a matched pair, so ``.strip("'")``-style corruption of a
    legitimately-quoted value containing quotes cannot occur.
    """
    if raw is None:
        return None
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def _split_trailing_comment(raw: str) -> tuple[str, str]:
    """Split a raw scalar value returned by ``read_fm_field`` into
    ``(value, comment_suffix)`` such that ``value + comment_suffix == raw``
    byte-for-byte, honouring quoting so a ``#`` *inside* a quoted scalar is
    never mistaken for a comment start.

    The single parser behind two consumers: ``_strip_trailing_comment``
    (read side, wants just ``value``) and ``replace_fm_field_raw`` (write
    side, break-class fix 2026-08-01, wants ``comment_suffix`` too so a
    rewritten line can re-emit the original comment instead of silently
    deleting it — see that function's docstring for the corruption this
    closes). ``comment_suffix`` includes any whitespace padding between the
    trimmed value and the ``#`` itself, so simple concatenation round-trips
    a comment-bearing line exactly, and is ``''`` when ``raw`` carries no
    trailing comment (round-trip is then just ``value == raw``).

    YAML's own rule: a ``#`` starts a comment only when preceded by
    whitespace (or at the very start of the scalar) AND it is not inside a
    quoted scalar — a ``#`` glued to a preceding non-space character is data
    (``abc#def``), not a comment. For a quoted raw value (``read_fm_field``
    returns the quotes verbatim), only the tail AFTER the closing quote is
    ever comment-eligible — a ``#`` inside the quotes (e.g. ``'has # a
    hash'``) is part of the string and must survive unstripped; this mirrors
    ``unquote_yaml_scalar``'s own matched-pair discipline rather than a naive
    ``.split('#')``.
    """
    if raw.startswith("'"):
        i, n = 1, len(raw)
        while i < n:
            if raw[i] == "'":
                if i + 1 < n and raw[i + 1] == "'":
                    i += 2
                    continue
                i += 1
                break
            i += 1
        quoted_end = i
    elif raw.startswith('"'):
        i, n = 1, len(raw)
        while i < n and raw[i] != '"':
            i += 2 if raw[i] == '\\' else 1
        quoted_end = i + 1 if i < n else i
    else:
        quoted_end = 0

    tail = raw[quoted_end:]
    # Review: code-reviewer — Finding 1 (P1): a glued `#` (data, not comment-
    # eligible) used to make this function give up entirely via a bare
    # `return raw, ''`, so a LATER, genuinely space-preceded `#` starting a
    # real comment was never found (`abc#def  # real comment` dropped the
    # comment on rewrite). Keep scanning from `hash_pos + 1` instead of
    # stopping at the first ineligible `#`.
    search_from = 0
    while True:
        hash_pos = tail.find('#', search_from)
        if hash_pos == -1:
            return raw, ''
        if hash_pos == 0 or tail[hash_pos - 1] in (' ', '\t'):
            value_with_pad = raw[:quoted_end] + tail[:hash_pos]
            value = value_with_pad.rstrip()
            pad = value_with_pad[len(value):]
            return value, pad + tail[hash_pos:]
        search_from = hash_pos + 1


def _strip_trailing_comment(raw: str) -> str:
    """Strips a trailing YAML ``# comment`` from a raw scalar value returned by
    ``read_fm_field``, honouring quoting so a ``#`` *inside* a quoted scalar is
    never mistaken for a comment start. Thin delegate over
    ``_split_trailing_comment`` — see that function for the quote-aware
    parsing rule; this keeps ONE parser behind both the read-only caller here
    and the comment-preserving write path in ``replace_fm_field_raw``.

    Break-class bug this closes (2026-07-27, `baton_assemble` FK-corruption
    report): ``read_fm_field``/``read_fm_field_unquoted`` returned the entire
    rest of the line verbatim, so ``initiative: null  # FK to
    state/initiatives/<id>.yaml; null when no named initiative`` read back as
    the **string** ``"null  # FK to ...; null when no named initiative"``
    instead of the YAML scalar ``null`` — silently turning "no initiative"
    into "an initiative literally named after its own doc-comment". Any
    frontmatter value in the corpus carrying a trailing ``#`` comment on its
    own ``key: value  # comment`` line hit the identical corruption via this
    one shared reader (`coordinator_core.baton_assemble.resolve_lineage`'s
    ``deliverable_id``/``initiative``/``predecessor`` reads, and every other
    C2-C5 executor caller of ``read_fm_field_unquoted``) — this is a shared-
    primitive fix, not a baton_assemble-local one.

    YAML's own rule: a ``#`` starts a comment only when preceded by
    whitespace (or at the very start of the scalar) AND it is not inside a
    quoted scalar — a ``#`` glued to a preceding non-space character is data
    (``abc#def``), not a comment. For a quoted raw value (``read_fm_field``
    returns the quotes verbatim), only the tail AFTER the closing quote is
    ever comment-eligible — a ``#`` inside the quotes (e.g. ``'has # a
    hash'``) is part of the string and must survive unstripped; this mirrors
    ``unquote_yaml_scalar``'s own matched-pair discipline rather than a naive
    ``.split('#')``.
    """
    return _split_trailing_comment(raw)[0]


def read_fm_field_unquoted(fm: str, key: str) -> str | None:
    """Read ``key:`` from frontmatter text, strip a trailing ``# comment``
    (see ``_strip_trailing_comment``), then strip one layer of YAML quoting.

    The comparison-safe sibling of ``read_fm_field``: use this wherever the
    value is compared against an unquoted in-memory value (an idempotency or
    already-at-target gate), parsed, or set-membership-tested. Use the raw
    ``read_fm_field`` when the value is only presence-tested, echoed, logged, or
    rewritten verbatim.

    Returns ``None`` when the key is absent — identical key-resolution semantics
    to ``read_fm_field``, including the ``(?=[ \\t]|\\r?$)`` boundary lookahead that
    stops ``status`` matching ``status_message:``.
    """
    raw = read_fm_field(fm, key)
    if raw is None:
        return None
    return unquote_yaml_scalar(_strip_trailing_comment(raw))


# ---------------------------------------------------------------------------
# serialize_yaml_scalar
# ---------------------------------------------------------------------------

def serialize_yaml_scalar(v: object, *, numeric_quoting: bool = False) -> str:
    """Serialise a scalar value for inline YAML frontmatter text.

    Quoting rules:
    - ``None`` → bare ``null`` literal (D9 present-as-null, from normalize-handoff).
    - Values containing YAML structural characters (``#:{}``, brackets, etc.) are
      single-quoted; internal single quotes are escaped by doubling (``'`` → ``''``).
    - Leading ``-``, ``?``, or space always triggers quoting.
    - ``numeric_quoting=True``: additionally quotes all-digit values (SHA-as-int
      defence) and YAML-1.1 scientific-notation floats (stamp-shipped-in the Staff Engineer F0).

    Negative-spec: does not handle multi-line values.
    """
    if v is None:
        return 'null'

    s = str(v)

    needs_quoting = (
        bool(_STRUCTURAL_RE.search(s))
        or s.startswith('-')
        or s.startswith('?')
        or s.startswith(' ')
    )

    if not needs_quoting and numeric_quoting:
        needs_quoting = bool(_ALL_NUMERIC_RE.match(s)) or bool(_SCIENTIFIC_RE.match(s))

    if not needs_quoting:
        return s

    return "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Nested-block key line lookup — shared by the block-scalar-adjacent guard and
# by read_fm_nested_field/write_fm_nested_field/remove_fm_nested_field below.
# ---------------------------------------------------------------------------

def _fm_key_line_pattern(key: str) -> re.Pattern[str]:
    """The single boundary-lookahead ``^key:(?=[ \\t]|\\r?$).*$`` pattern every
    frontmatter primitive anchors on — factored out so the nested-block helpers
    and the guards share exactly one key-resolution rule with
    ``read_fm_field``/``replace_fm_field``/``remove_fm_field``."""
    return re.compile(r'^' + re.escape(key) + r':(?=[ \t]|\r?$).*$', re.MULTILINE)


def _locate_nested_block(fm: str, key: str) -> Optional[tuple[int, int, int]]:
    """Locate ``key:``'s line and its trailing indented continuation block.

    Returns ``(key_line_start, block_start, block_end)`` character offsets, or
    ``None`` when the key is absent. ``block_start == block_end`` when the key
    has no continuation lines (an ordinary single-line field). A continuation
    line is any line that is blank or begins with a space/tab — the block ends
    at the first column-0 line (a new top-level key) or end of text, mirroring
    how YAML block-sequence indentation scopes a mapping value.
    """
    m = _fm_key_line_pattern(key).search(fm)
    if m is None:
        return None
    key_start = m.start()
    pos = m.end()
    if pos < len(fm) and fm[pos] == '\n':
        pos += 1
    block_start = pos
    idx = pos
    while idx < len(fm):
        nl = fm.find('\n', idx)
        line_end = nl if nl != -1 else len(fm)
        line = fm[idx:line_end]
        if line == '' or line[:1] in (' ', '\t'):
            idx = line_end + 1 if nl != -1 else len(fm)
        else:
            break
    return (key_start, block_start, idx)


def _is_nested_block_key(fm: str, key: str) -> bool:
    """True when ``key:``'s own line carries no inline value AND its very next
    physical line is a block-nested continuation — either the ``gate_evidence:``
    -style indented sequence-of-mappings shape, or a legal YAML block sequence
    written at the SAME indentation as its parent key (``tags:\\n- a\\n- b``) —
    that the single-line helpers below must refuse rather than silently orphan
    (the Staff Engineer F4; Review: code-reviewer — Finding 4, unindented-sequence gap).

    Deliberately does NOT use ``read_fm_field`` to test emptiness. That was
    originally because ``read_fm_field``'s ``\\s*`` crossed the newline after
    ``key:`` and captured the first continuation line's own text as its
    "value" (e.g. ``"- kind: test-node-id"``) instead of ``""`` — a quirk
    fixed at the root on 2026-07-28. The independence is kept anyway: this
    check needs the key's OWN line text, which it reads from the match
    directly, and reading it here rather than through a value-extracting
    sibling keeps the two concerns from drifting back together.
    """
    m = _fm_key_line_pattern(key).search(fm)
    if m is None:
        return False
    same_line_after_colon = m.group(0)[len(key) + 1:]
    if same_line_after_colon.strip() != '':
        return False
    rest = fm[m.end():]
    if rest.startswith('\n'):
        rest = rest[1:]
    if not rest:
        return False
    nl = rest.find('\n')
    first_line = rest[:nl] if nl != -1 else rest
    if not first_line:
        return False
    if first_line[0] in (' ', '\t'):
        return True
    # An unindented block sequence item (`- ...`) at column 0 is also a
    # nested block value, not a sibling top-level key — no legal frontmatter
    # key starts with `-`, so this cannot collide with a following sibling.
    return first_line[0] == '-'


def _raise_nested_block_guard(fn_name: str, key: str) -> None:
    """Shared raise for the nested-block guard in ``replace_fm_field``/
    ``remove_fm_field`` — same defensive posture as the block-scalar guard
    (the Staff Engineer F4): a mechanical refusal, not a comment a future caller has to
    read."""
    raise ValueError(
        f'{fn_name}: field "{key}" holds a nested YAML block '
        f'(sequence-of-mappings, e.g. gate_evidence:) — mutating only the key '
        f'line would silently orphan its indented continuation lines. Use '
        f'{"remove_fm_nested_field" if fn_name == "remove_fm_field" else "write_fm_nested_field"}'
        f'(fm, "{key}", ...) instead.'
    )


# ---------------------------------------------------------------------------
# replace_fm_field
# ---------------------------------------------------------------------------

def replace_fm_field(fm: str, key: str, v: object, *, numeric_quoting: bool = False) -> str:
    """Replace the value of an existing ``key:`` line in frontmatter text.

    Block-scalar guard (the Staff Engineer F1): raises ``ValueError`` when the current value
    starts with ``>`` (folded scalar) or ``|`` (literal scalar) — truncating a
    multi-line block value into a single line would silently corrupt the document.

    Nested-block guard (the Staff Engineer F4, AC11): raises the same ``ValueError`` when the
    current value reads back empty and its next line is indented — a YAML
    sequence-of-mappings (``gate_evidence:``-shaped) value that a single-line
    replace would truncate to one bare ``key:`` line, orphaning the indented
    entries. Use ``write_fm_nested_field`` for that shape instead.

    Uses the boundary lookahead from handoff-transition.js so ``status`` cannot
    accidentally match ``status_message:``.

    Negative-spec (break-class fix, 2026-07-28): the captured prefix pads with
    ``[ \\t]*``, never ``\\s*``. Because ``\\s`` matches a newline, the prefix
    of a present-but-empty ``key:`` used to swallow the line break and the
    trailing ``.*$`` then matched the FOLLOWING line — so replacing an empty
    field overwrote its innocent neighbour. See ``read_fm_field``'s own
    negative-spec for the read-side half of the same defect. A
    present-but-empty ``key:`` is filled as ``key: value`` (the separator is
    supplied, never glued into ``key:value``), and the rewritten line
    re-emits its own trailing ``\\r``, so rewriting one line of a CRLF
    document cannot leave it with mixed line endings.

    ``numeric_quoting`` is forwarded to ``serialize_yaml_scalar`` — set True when
    writing commit SHAs that may be all-digit.

    Negative-spec: does NOT insert a new line when the key is absent — call
    ``insert_fm_field`` for that.

    Negative-spec (break-class fix, 2026-08-01 — trailing-inline-comment
    preservation): delegates to ``replace_fm_field_raw``, which now preserves
    a pre-existing trailing ``# comment`` on the ``key:`` line being
    rewritten instead of deleting it. See that function's docstring for the
    corruption this closes and the exact reconstruction rule.
    """
    current = read_fm_field(fm, key)
    if current is not None and (current.startswith('>') or current.startswith('|')):
        truncated = current[:40] + '...' if len(current) > 40 else current
        raise ValueError(
            f'replace_fm_field: field "{key}" uses a block-scalar YAML value '
            f'("{truncated}") — cannot safely replace single-line. '
            f'Fix the frontmatter manually.'
        )
    if _is_nested_block_key(fm, key):
        _raise_nested_block_guard('replace_fm_field', key)

    return replace_fm_field_raw(
        fm, key, serialize_yaml_scalar(v, numeric_quoting=numeric_quoting)
    )


def replace_fm_field_raw(fm: str, key: str, raw_value: str) -> str:
    """Replace an existing ``key:`` line's value with ALREADY-SERIALIZED text.

    The value-rewriting half of ``replace_fm_field``, factored out for callers
    that hold a pre-serialized value ``serialize_yaml_scalar`` cannot produce —
    today that is the inline-array form ``[a, b]`` written by
    ``ops/handoff_author_fork._stamp_fork_provenance`` and
    ``ops/handoff_transition``'s array writers. This substitution regex must
    exist EXACTLY ONCE in the tree: three hand-copies of it outside this module
    each independently reproduced the ``\\s*``-crosses-the-newline corruption
    described in ``read_fm_field``'s negative-spec (2026-07-28 code review), so
    a caller needing the array shape calls this rather than re-forking the
    pattern.

    Negative-spec: applies NO guards. ``replace_fm_field``'s block-scalar and
    nested-block guards run in that wrapper, before the call reaches here,
    because a raw-value caller generally wants to raise its own
    domain-specific message for those shapes — see ``_stamp_fork_provenance``,
    which reapplies both explicitly. Do not "helpfully" add the guards here
    without moving them out of the wrapper; running them twice would double
    the read cost and split the error text across two owners.

    Negative-spec: like ``replace_fm_field``, does NOT insert a line when the
    key is absent, and the padding around the captured value is ``[ \\t]``,
    never ``\\s``.

    Negative-spec (break-class fix, 2026-08-01 — trailing-inline-comment
    preservation, example-cockpit-repo repro): this function used to substitute
    the ENTIRE rest of the ``key:`` line, so a value carrying a trailing YAML
    inline comment (``status: approved  # PM authorized execution ...``)
    silently lost the comment on rewrite (``status: implemented``) — the
    write-side half of the same defect ``_split_trailing_comment``/
    ``read_fm_field_unquoted`` closed on the read side (2026-07-27). The
    comment is now split off the OLD line's own captured text (via this
    substitution regex's match, never a second ``read_fm_field`` call — this
    function must stay independently correct for its inline-array callers,
    which never go through ``read_fm_field``'s value-typed path) and
    re-appended, byte-identical padding included, after the new value:
    ``key: implemented  # PM authorized execution ...``. Quote-aware via
    ``_split_trailing_comment`` (the same parser ``_strip_trailing_comment``
    delegates to), so a ``#`` inside a quoted old value, or glued to a
    preceding non-space character, is correctly treated as data and never
    misread as a comment start.

    The captured-prefix group no longer bundles the horizontal whitespace
    after the colon — it is now exactly ``key:``, with the run of
    ``[ \\t]*`` immediately after captured as part of the "rest" group so it
    can be told apart from the OLD value's own trailing padding (which
    belongs to the comment side of the split, not the separator side). A
    present-but-empty ``key:`` (no value, comment or not) still fills as
    ``key: value`` — the canonical single-space separator is synthesized
    fresh rather than replayed from the old line, exactly as before this
    fix. A comment-only line (``key:  # nothing yet``, no value) now fills
    as ``key: value  # nothing yet`` — the comment is preserved rather than
    replayed-then-dropped; see ``_append_blocking_note``'s docstring for the
    one caller whose own documented contract this changes.
    """
    pattern = re.compile(
        r'^(' + re.escape(key) + r':(?=[ \t]|\r?$))(.*?)(\r?)$',
        re.MULTILINE,
    )

    def _sub(m: re.Match[str]) -> str:
        prefix = m.group(1)
        rest = m.group(2)
        cr = m.group(3)

        stripped = rest.lstrip(' \t')
        leading_ws = rest[: len(rest) - len(stripped)]
        old_value, comment_suffix = _split_trailing_comment(stripped)

        if comment_suffix == '':
            # No trailing comment on the old line at all — byte-identical to
            # this function's pre-fix behaviour: replay whatever horizontal
            # whitespace already followed the colon verbatim (a
            # present-but-empty `key:` with no comment synthesizes the
            # canonical single space, same as before).
            sep = leading_ws if leading_ws else ' '
            new_rest = sep + raw_value
        elif old_value == '':
            # Comment-only line (`key:  # nothing yet`, no real value) — the
            # old separator space was doing double duty as pre-comment
            # padding; synthesize the canonical single-space separator for
            # the new value and re-home that padding in front of the
            # preserved comment.
            new_rest = ' ' + raw_value + leading_ws + comment_suffix
        else:
            new_rest = leading_ws + raw_value + comment_suffix

        # `cr` re-emits the line's own trailing `\r`, so rewriting one line
        # of a CRLF document cannot leave it with mixed line endings.
        return prefix + new_rest + cr

    # A lambda/function avoids backslash interpretation in the replacement string
    return pattern.sub(_sub, fm)


# ---------------------------------------------------------------------------
# insert_fm_field
# ---------------------------------------------------------------------------

def insert_fm_field(
    fm: str,
    key: str,
    v: object,
    after_key: str | None = None,
    *,
    numeric_quoting: bool = False,
) -> str:
    """Insert ``key: value`` into frontmatter text.

    Two variants unified via the optional ``after_key`` parameter:

    **Anchored** (``after_key`` given): insert the new line immediately after the
    line matching ``after_key:``.  If ``after_key`` is not found in the text,
    falls back to append-at-end.  Matches the 4-arg ``insertFmField`` in
    handoff-transition.js and stamp-shipped-in.js.

    **Append-only** (``after_key=None``): trim trailing whitespace and append
    ``key: value\\n``.  Matches the 2-arg ``insertFmField`` in
    normalize-handoff-frontmatter.js.

    ``numeric_quoting`` is forwarded to ``serialize_yaml_scalar`` — set True when
    writing commit SHAs that may be all-digit (stamp-shipped-in the Staff Engineer F0).

    Negative-spec (2026-07-28, CRLF): the inserted line adopts the line ending
    of the document it lands in — the anchor line's own terminator on the
    anchored path, the last existing line's on the append path — so inserting
    into a CRLF-authored document cannot leave it with MIXED line endings. The
    anchored half became newly reachable on CRLF with that release's
    ``(?=[ \\t]|\\r?$)`` widening: before it, a present-but-empty
    ``after_key:\\r\\n`` did not match at all and silently fell through to
    append-at-end.
    """
    serialized = serialize_yaml_scalar(v, numeric_quoting=numeric_quoting)
    new_line = f'{key}: {serialized}'

    if after_key is not None:
        # Anchored variant — boundary lookahead consistent with read_fm_field
        after_pattern = re.compile(
            r'^' + re.escape(after_key) + r':(?=[ \t]|\r?$).*$',
            re.MULTILINE,
        )
        m = after_pattern.search(fm)
        if m:
            insert_at = m.end()
            # `.*$` stops before the `\n` but AFTER any `\r`, so the anchor line
            # keeps its own terminator; the NEW line borrows the trailing `\n`
            # already in the text and therefore needs the matching `\r` re-emitted.
            cr = '\r' if m.group(0).endswith('\r') else ''
            return fm[:insert_at] + '\n' + new_line + cr + fm[insert_at:]
        # after_key absent — fall through to append (same as JS behaviour)

    # Append-only (or anchored fallback).
    # The line ending is detected on the ORIGINAL `fm`, never on the rstrip()ed
    # text: rstrip() eats the trailing `\r\n`, so a document whose only CRLF was
    # its terminator (`'title: T\r\n'`, and every single-line CRLF frontmatter)
    # was misdetected as LF — and because `trimmed + eol` re-supplies the
    # stripped ending, the existing last line was silently DOWNGRADED to LF too,
    # contradicting this function's own mixed-endings contract above.
    eol = '\r\n' if '\r\n' in fm else '\n'
    trimmed = fm.rstrip()
    return trimmed + eol + new_line + eol


def insert_fm_field_raw(fm: str, key: str, raw_value: str, after_key: str | None = None) -> str:
    """Insert ``key: value`` into frontmatter text using ALREADY-SERIALIZED text.

    The insert-side counterpart to ``replace_fm_field_raw`` — same rationale:
    a caller holding a pre-serialized value (e.g. this file's own
    ``_yaml_quote``-forced-double-quote convention) cannot route it through
    ``insert_fm_field``, because that function's ``serialize_yaml_scalar``
    call would see the caller's own quote characters as structural and
    re-quote them, producing a doubled/malformed value. Mirrors
    ``insert_fm_field``'s anchored/append-only line-ending discipline
    exactly — only the value's construction differs.

    Review: coordinator:code-reviewer — the insert branch of
    ``coordinator-doc-new::_mutate_sizing_reverse_edge`` used to call
    ``insert_fm_field`` with a raw unquoted path, so a first-time scaffold
    left ``plan:`` bare while a re-run (replace branch, which already used a
    raw-value primitive) left it double-quoted — the function's own
    docstring claimed both were always double-quoted. This primitive closes
    that gap in the primitives module rather than hand-rolling a fourth
    line-surgery site in the CLI.
    """
    if after_key is not None:
        after_pattern = re.compile(
            r'^' + re.escape(after_key) + r':(?=[ \t]|\r?$).*$',
            re.MULTILINE,
        )
        m = after_pattern.search(fm)
        if m:
            insert_at = m.end()
            cr = '\r' if m.group(0).endswith('\r') else ''
            return fm[:insert_at] + '\n' + f'{key}: {raw_value}' + cr + fm[insert_at:]
        # after_key absent — fall through to append (same as insert_fm_field)

    eol = '\r\n' if '\r\n' in fm else '\n'
    trimmed = fm.rstrip()
    return trimmed + eol + f'{key}: {raw_value}' + eol


# ---------------------------------------------------------------------------
# remove_fm_field
# ---------------------------------------------------------------------------

def remove_fm_field(fm: str, key: str) -> str:
    """Remove the ``key: …`` line from frontmatter text, including its trailing newline.

    Port of ``removeFmField`` from DoE-claude ``bin/memo-transition.js:126-132``.

    The boundary lookahead ``(?=[ \\t]|\\r?$)`` prevents ``picked_up_by`` from
    matching ``picked_up_by_x:`` (same discipline as ``read_fm_field``). The
    ``\\n?`` makes removal safe when the key is the last line of the frontmatter
    block (no trailing newline present). Uses ``re.escape(key)`` for sibling
    consistency with ``read_fm_field``, ``replace_fm_field``, and
    ``insert_fm_field``. Returns the frontmatter text unchanged when the key is
    absent (no-op).

    Block-scalar guard (mirrors ``replace_fm_field``): raises ``ValueError`` when
    the current value starts with ``>`` or ``|`` — the pattern ``.*$\\n?`` removes
    only the key line, silently orphaning indented continuation lines.

    Nested-block guard (the Staff Engineer F4, AC11): raises the same ``ValueError`` when the
    current value reads back empty and its next line is indented — the
    ``gate_evidence:``-shaped sequence-of-mappings case the block-scalar guard
    above does not cover (it reads back as ``""``, not a ``>``/``|`` prefix). Use
    ``remove_fm_nested_field`` for that shape instead.

    Spec backlink: coordinator/bin/memo-transition.js:126-132 (removeFmField).
    """
    # Review: code-reviewer — F1: block-scalar guard mirrors replace_fm_field.
    # Removing only the key line of a block-scalar orphans indented continuation
    # lines, silently corrupting the frontmatter.
    current = read_fm_field(fm, key)
    if current is not None and (current.startswith('>') or current.startswith('|')):
        truncated = current[:40] + '...' if len(current) > 40 else current
        raise ValueError(
            f'remove_fm_field: field "{key}" uses a block-scalar YAML value '
            f'("{truncated}") — cannot safely remove single-line. '
            f'Fix the frontmatter manually.'
        )
    if _is_nested_block_key(fm, key):
        _raise_nested_block_guard('remove_fm_field', key)
    pattern = re.compile(
        r'^' + re.escape(key) + r':(?=[ \t]|\r?$).*$\n?',
        re.MULTILINE,
    )
    return pattern.sub('', fm)


# ---------------------------------------------------------------------------
# _append_blocking_note / _retire_gate_dependency
# ---------------------------------------------------------------------------

def _append_blocking_note(fm: str, note: str, anchor_key: str) -> str:
    """Append ``note`` onto ``blocking_notes``, never overwriting existing prose.

    The ONE append-to-``blocking_notes`` mechanism behind every gate-field
    retirement (C8/AC9). ``_retire_gate_dependency`` below and
    ``handoff_transition._retire_gate_evidence`` both route through here —
    neither carries its own private copy of the read/combine/insert dance,
    because two divergent copies of "how a retirement lands in
    blocking_notes" is exactly the drift this factoring exists to prevent.

    ``blocking_notes`` is a plain advisory `string` schema property, never
    read by the resolver — landing here is legal at every deployment_state,
    including ready_to_fire, where both ``gate_dependency`` and
    ``gate_evidence`` are FORBIDDEN by cross-field rule. That legality is the
    whole reason it is the retirement destination.

    Append semantics: an existing non-empty note is preserved and the new
    note joined onto it with `` | `` (a node may already carry advisory prose
    unrelated to this gate). A ``blocking_notes`` key that is present but
    EMPTY is filled IN PLACE rather than inserted alongside — inserting
    would mint a SECOND ``blocking_notes:`` line and hand the reader a
    duplicate-key document; there is no prose to preserve in that case, so
    filling loses nothing.

    **Present-but-empty has THREE on-disk shapes, and all three take the
    in-place path** (code-reviewer P2 on 2bf49370). Only the first is
    reachable from this module's own writers — ``serialize_yaml_scalar('')``
    emits neither quotes nor a comment — so the other two require
    hand-authored or externally-written frontmatter:

    1. bare — ``blocking_notes:`` with nothing but optional whitespace/``\\r``;
    2. quoted-empty — ``blocking_notes: ''`` / ``blocking_notes: ""``, which
       ``unquote_yaml_scalar`` strips to ``''``;
    3. comment-only — ``blocking_notes:  # nothing yet``, which
       ``_strip_trailing_comment`` reduces to ``''``.

    All three are handled by the single ``existing_notes == ''`` branch below.
    An ``is None`` test, not a truthiness test, is what separates "absent"
    from "present but empty" — that distinction became meaningful only once
    ``read_fm_field`` stopped returning the following line's text for an
    empty key. There is no prose being lost, and the note must land as a
    legible scalar.

    Updated 2026-08-01 (``replace_fm_field_raw`` comment-preservation fix):
    shape 2's original quoting (``''``/``""``) still does NOT survive the
    fill — the fresh value is always re-serialized bare or single-quoted per
    ``serialize_yaml_scalar``'s own rules, never the old quote style. Shape
    3's trailing comment, however, now DOES survive: ``blocking_notes:  #
    nothing yet`` fills to ``blocking_notes: <note>  # nothing yet`` rather
    than silently dropping the comment, because the fill routes through
    ``replace_fm_field``/``replace_fm_field_raw`` like any other rewrite and
    that function no longer discards a trailing inline comment on the line
    it replaces.

    Negative-spec (2026-07-28 — do NOT reintroduce a private line-anchored
    ``^blocking_notes:[ \\t]*(\\r?)$`` regex branch ahead of the reads).
    ``_EMPTY_BLOCKING_NOTES_RE`` existed here from 2bf49370 until the
    boundary lookahead was widened to ``(?=[ \\t]|\\r?$)``, on two
    justifications that the widening retired outright:

    - **CRLF blind spot** — a bare ``blocking_notes:\\r\\n`` used to read
      back as ABSENT (the old lookahead rejected the ``\\r``), so without a
      line-anchored branch it fell through to ``insert_fm_field`` and minted
      a duplicate key. It now reads as ``''`` and takes the in-place branch.
    - **Duplicate-key prevention** — already discharged by the ``is None``
      test below, which is what actually discriminates absent from empty;
      the regex branch was never the thing preventing the duplicate.

    Removing it also closed a live corruption path it had been *causing*:
    running ahead of the reads, it bypassed ``replace_fm_field``'s
    nested-block guard, so a ``blocking_notes:`` with an indented
    continuation was blindly rewritten to ``blocking_notes: <note>`` with
    its ``  - …`` entries orphaned beneath. That shape now raises
    ``ValueError`` like every other nested-block misuse in this module.

    ``anchor_key`` is the field being retired, used only as the
    insert-after anchor when ``blocking_notes`` is absent entirely;
    ``insert_fm_field`` falls back to append-at-end when the anchor is
    missing, so a caller never has to check.

    No-op on an empty/blank ``note`` — safe to call unconditionally from a
    retire-site whose source field turned out to be absent.
    """
    if not note:
        return fm

    existing_notes = read_fm_field_unquoted(fm, "blocking_notes")
    if existing_notes is None:
        return insert_fm_field(fm, "blocking_notes", note, anchor_key)
    if existing_notes:
        return replace_fm_field(fm, "blocking_notes", f"{existing_notes} | {note}")
    # Present but empty in any of its three shapes — fill the existing line
    # rather than inserting a duplicate key alongside it.
    return replace_fm_field(fm, "blocking_notes", note)


def _retire_gate_dependency(fm: str) -> str:
    """Retire ``gate_dependency`` into ``blocking_notes``, then strip it.

    The single primitive behind every gate_dependency-full-strip call site
    (C8, AC11): the schema's ready_to_fire→gate_dependency-forbidden if/then
    rule makes destruction the only legal way to reach ready_to_fire, but
    destruction with no history retention silently loses the prose. This
    function makes the destination legal AND non-destructive: it APPENDS the
    current ``gate_dependency`` value onto ``blocking_notes`` (never
    overwrites an existing note — a node may already carry advisory prose
    unrelated to this gate) and only then removes ``gate_dependency`` via
    ``remove_fm_field``.

    ``blocking_notes`` is a plain advisory `string` schema property, never
    read by the resolver — landing here is legal at every deployment_state,
    including ready_to_fire.

    No-op (returns ``fm`` unchanged, but still routes through
    ``remove_fm_field`` for its no-op-when-absent behaviour) when
    ``gate_dependency`` is absent — safe to call unconditionally at a
    destroy-site that previously called ``remove_fm_field(fm,
    "gate_dependency")`` directly.

    Uses ``read_fm_field_unquoted`` to extract the retired value (so a
    quoted on-disk scalar doesn't carry its quotes into the appended prose)
    and delegates the write to ``_append_blocking_note`` (so the combined
    string is re-quoted correctly by ``serialize_yaml_scalar`` on write,
    regardless of either half's original quoting, and so the append rule
    itself lives in exactly ONE place — see that function).
    """
    current_gate_dep = read_fm_field_unquoted(fm, "gate_dependency")
    if current_gate_dep is None:
        return remove_fm_field(fm, "gate_dependency")

    fm = _append_blocking_note(fm, current_gate_dep, "gate_dependency")
    return remove_fm_field(fm, "gate_dependency")


# ---------------------------------------------------------------------------
# read_fm_nested_field / write_fm_nested_field / remove_fm_nested_field
#
# The nested-block counterpart of read_fm_field/replace_fm_field/
# remove_fm_field, for a key whose value is an indented YAML
# sequence-of-mappings (gate_evidence:'s shape — a "- kind: ...\n  repo: ..."
# block under the key line) rather than a single-line scalar. AC11 /
# eng-director F1 (break-class): the only prior nested field, carried_items,
# is read-only (full-YAML-load, never mutated by these primitives), so there
# was no existing writer to extend.
# ---------------------------------------------------------------------------

def read_fm_nested_field(fm: str, key: str) -> str | None:
    """Return ``key:``'s full indented continuation block, or ``None`` if
    ``key:`` is absent entirely.

    Returns ``''`` (not ``None``) when the key is present with no continuation
    lines — an ordinary empty/single-line field — matching ``read_fm_field``'s
    absent-vs-empty distinction. The returned text is the raw on-disk block
    (entries only, key line excluded), suitable for round-tripping through
    ``write_fm_nested_field``.
    """
    loc = _locate_nested_block(fm, key)
    if loc is None:
        return None
    _, block_start, block_end = loc
    return fm[block_start:block_end]


def write_fm_nested_field(fm: str, key: str, block_text: str) -> str:
    """Insert or replace ``key:``'s nested block value with ``block_text``.

    ``block_text`` is the raw indented block (e.g.
    ``'  - kind: test-node-id\\n    ref: abc\\n'``), key line excluded — a
    trailing newline is added if missing. When ``key:`` already exists, its
    key line and entire prior continuation block are replaced; when absent,
    ``key:\\n`` plus the block is appended, matching ``insert_fm_field``'s
    append-only convention.

    Negative-spec: does not validate ``block_text``'s YAML shape — callers
    supply already-serialized entries (this module has no YAML dumper).
    """
    if block_text and not block_text.endswith('\n'):
        block_text += '\n'
    loc = _locate_nested_block(fm, key)
    if loc is None:
        trimmed = fm.rstrip()
        sep = '\n' if trimmed else ''
        return trimmed + sep + f'{key}:\n' + block_text
    key_start, _, block_end = loc
    return fm[:key_start] + f'{key}:\n' + block_text + fm[block_end:]


def remove_fm_nested_field(fm: str, key: str) -> str:
    """Remove ``key:``'s line and its entire indented continuation block.

    The nested-block counterpart of ``remove_fm_field`` — unlike that
    function, this is safe to call on a ``gate_evidence:``-shaped value; it
    removes the key line AND every continuation line beneath it, so nothing
    is orphaned. No-op (returns ``fm`` unchanged) when the key is absent.
    """
    loc = _locate_nested_block(fm, key)
    if loc is None:
        return fm
    key_start, _, block_end = loc
    return fm[:key_start] + fm[block_end:]


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------

def rebuild(split: FrontmatterSplit, fm_text: str) -> str:
    """Reassemble a document from a ``FrontmatterSplit`` and updated frontmatter.

    Ensures exactly one newline before the closing ``---``. Preserves preamble
    and body verbatim — producing byte-identical output outside the mutated lines.
    """
    fm_normalized = fm_text if fm_text.endswith('\n') else fm_text + '\n'
    return (
        (split.preamble or '')
        + '---\n'
        + fm_normalized
        + '---'
        + split.body_with_leading_newline
    )


# ---------------------------------------------------------------------------
# canonical plan-body hash — shared by pickup_assemble and review_assemble
# ---------------------------------------------------------------------------

_BODY_DELIMITER_RE = re.compile(r'^---[ \t]*$')


def frontmatter_body_text(file_text: str) -> str:
    """Everything below the SECOND ``---`` frontmatter delimiter line — pure-
    Python port of the canonical recipe's awk half (``awk '/^---[[:space:]]*$/
    {fm++; next} fm>=2{print}'``). Every line that is ``---`` alone (optional
    trailing horizontal whitespace) increments the counter and is itself
    never emitted; printed lines are re-terminated with ``\\n`` regardless of
    the source line's own terminator, mirroring awk's ``print`` (content +
    ``ORS``), so a file lacking a trailing newline still hashes identically
    to the awk pipeline's output.
    """
    fm_count = 0
    out_lines: list[str] = []
    for line in file_text.splitlines():
        if _BODY_DELIMITER_RE.match(line):
            fm_count += 1
            continue
        if fm_count >= 2:
            out_lines.append(line + '\n')
    return ''.join(out_lines)


def git_blob_sha1(text: str) -> Optional[str]:
    """The literal git blob-hash algorithm — ``sha1("blob " + len(content) +
    "\\0" + content)`` — computed in-process rather than shelled out to a
    real ``git hash-object --stdin``. Not a heuristic approximation; this is
    the object-header format git's own hash-object documents. UTF-8
    encoding, no platform newline translation, matching a real
    ``git hash-object --stdin``'s binary-stdin guarantee on Windows. Returns
    ``None`` only on an encoding failure — degrades the caller's gate to
    absent, never a fabricated hash.
    """
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        return None
    header = f'blob {len(data)}\0'.encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


def canonical_body_sha(file_text: str) -> Optional[str]:
    """The shared plan-body-hash recipe: ``git_blob_sha1(frontmatter_body_text
    (file_text))``. Byte-identical to a real ``git hash-object --stdin`` over
    the plan body (everything below the second ``---`` delimiter);
    frontmatter fields never enter the hash — only a material change to the
    plan BODY invalidates a previously-computed stamp. Both
    `coordinator_core.pickup_assemble.compute_execution_stamp_match` (the
    read-side checker) and `coordinator_core.review_assemble.exec_auth_stamp.
    stamp_execution_authorization` (the write-side stamper) route through
    this one recipe.
    """
    return git_blob_sha1(frontmatter_body_text(file_text))
