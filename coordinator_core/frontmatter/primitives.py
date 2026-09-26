from __future__ import annotations

import hashlib
import re
from typing import NamedTuple, Optional


class FrontmatterSplit(NamedTuple):

    preamble: str
    """Leading blank lines and HTML comment blocks before the opening ``---``.
    Preserved verbatim on rebuild."""

    fm_text: str
    """Raw text between the ``---`` delimiters (may or may not end in ``\\n``)."""

    body_with_leading_newline: str
    """Everything after the closing ``---``, including its trailing newline."""


_PREAMBLE_RE = re.compile(
    r'^(?:[ \t]*\r?\n|[ \t]*<!--[\s\S]*?-->[ \t]*\r?\n?)+'
)

_CLOSE_RE = re.compile(r'^---[ \t]*$', re.MULTILINE)

_STRUCTURAL_RE = re.compile(r'[#:{}\[\],&*!|>"\'%@`]')

_ALL_NUMERIC_RE = re.compile(r'^[0-9]+$')

_SCIENTIFIC_RE = re.compile(r'^[0-9]+[eE][0-9]+$')


def split_frontmatter(text: str) -> FrontmatterSplit | None:
    text = text.replace('\r\n', '\n')

    preamble = ''
    if not re.match(r'^---[ \t]*\n', text):
        m = _PREAMBLE_RE.match(text)
        if not m:
            return None
        after = text[m.end():]
        if not re.match(r'^---[ \t]*\n', after):
            return None
        preamble = m.group(0)
        text = after

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


def unquote_yaml_scalar(raw: str | None) -> str | None:
    if raw is None:
        return None
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def _split_trailing_comment(raw: str) -> tuple[str, str]:
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
    return _split_trailing_comment(raw)[0]


def read_fm_field_unquoted(fm: str, key: str) -> str | None:
    raw = read_fm_field(fm, key)
    if raw is None:
        return None
    return unquote_yaml_scalar(_strip_trailing_comment(raw))


def serialize_yaml_scalar(v: object, *, numeric_quoting: bool = False) -> str:
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


def _fm_key_line_pattern(key: str) -> re.Pattern[str]:
    return re.compile(r'^' + re.escape(key) + r':(?=[ \t]|\r?$).*$', re.MULTILINE)


def _locate_nested_block(fm: str, key: str) -> Optional[tuple[int, int, int]]:
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
    return first_line[0] == '-'


def _raise_nested_block_guard(fn_name: str, key: str) -> None:
    raise ValueError(
        f'{fn_name}: field "{key}" holds a nested YAML block '
        f'(sequence-of-mappings, e.g. gate_evidence:) — mutating only the key '
        f'line would silently orphan its indented continuation lines. Use '
        f'{"remove_fm_nested_field" if fn_name == "remove_fm_field" else "write_fm_nested_field"}'
        f'(fm, "{key}", ...) instead.'
    )


#: comment. Anchored whole so a value that merely CONTAINS a pipe cannot match.
_BLOCK_SCALAR_HEADER_RE = re.compile(
    r'^(?P<style>[|>])'
    r'(?:(?P<indent>[1-9])(?P<chomp_a>[+-])?|(?P<chomp_b>[+-])(?P<indent_b>[1-9])?)?'
    r'[ \t]*(?:#.*)?$'
)


class BlockScalar(NamedTuple):
    """A frontmatter field whose value is a ``|`` or ``>`` block scalar.

    ``style`` is ``'|'`` (literal) or ``'>'`` (folded). ``pad`` is the body's
    literal indentation PREFIX — the actual whitespace characters, not a
    count, so that re-emitting a line into a tab-indented block writes tabs
    rather than silently mixing in spaces. ``indent`` is ``len(pad)``, kept
    for callers that only need the column. ``lines`` holds the body
    DE-INDENTED, in order, blank lines preserved as ``''``. ``newline`` is
    the terminator the BLOCK's own lines use, which is not necessarily the
    document's — a mixed-ending document must not have a foreign terminator
    appended into an otherwise-consistent block. ``end_offset`` is the
    character offset in the source frontmatter one past the last body line's
    newline — the splice point ``append_fm_block_scalar_line`` writes at.
    """

    style: str
    indent: int
    lines: list[str]
    end_offset: int
    pad: str = ''
    newline: str = '\n'


def read_fm_block_scalar(fm: str, key: str) -> Optional[BlockScalar]:
    """Read ``key:``'s value as a block scalar, or ``None`` when the field is
    absent or holds an ordinary single-line value.

    Exists because ``read_fm_field`` deliberately reads only the ``key:``
    LINE: on a block-scalar field it returns the bare header (``'|'``,
    ``'>-'``) and never the body. A caller that compared that reading
    against real note text was comparing against a two-character sigil —
    which is how a convergence test over a block-scalar
    ``execution_authorized_note`` silently answered "not converged" forever
    (cross-repo memo, example-retrieval-repo-em, 2026-08-20).

    Negative-spec: this does NOT resolve folded (``>``) line-joining or
    chomping into a final YAML string — it returns the body as authored.
    Its consumers are convergence tests and the appender below, both of
    which want the authored lines; a caller needing the resolved scalar
    should parse the document with a YAML loader instead.

    Trailing blank lines follow the chomping indicator, for ``lines`` and
    ``end_offset`` alike: under ``+`` (keep) YAML counts them as part of the
    value, so the block ends after them; under ``-`` or none they are filler
    between this field and the next, and the block ends before them. Do not
    re-unify these into one unconditional trim — dropping them under ``+``
    reads back less than the authored body, and keeping them under ``-``
    lands an appended line outside the block.

    Negative-spec: recognises only a WELL-FORMED header. A malformed value
    that merely starts with ``|``/``>`` (``|abc``, ``|0``) reads as ``None``,
    i.e. "not a block scalar", while ``replace_fm_field``'s looser
    single-character guard still refuses it. Callers must not infer from a
    ``None`` here that ``replace_fm_field`` will accept the field — see the
    ValueError-to-domain-error conversion in
    ``exec_auth_stamp.stamp_execution_authorization``.
    """
    m = _fm_key_line_pattern(key).search(fm)
    if m is None:
        return None

    header = m.group(0).split(':', 1)[1].strip()
    hm = _BLOCK_SCALAR_HEADER_RE.match(header)
    if hm is None:
        return None

    explicit = hm.group('indent') or hm.group('indent_b')
    chomp = hm.group('chomp_a') or hm.group('chomp_b')

    # part of the key line (`.` matches `\r`; MULTILINE `$` matches before
    rest = fm[m.end():]
    if rest.startswith('\n'):
        rest = rest[1:]
    body_start = len(fm) - len(rest)

    pad: Optional[str] = ' ' * int(explicit) if explicit is not None else None
    kept: list[str] = []
    for line in rest.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        if not body.strip():
            kept.append(line)
            continue
        if not body.startswith((' ', '\t')):
            break
        if pad is None:
            pad = body[:len(body) - len(body.lstrip())]
        if not body.startswith(pad):
            break
        kept.append(line)
    if pad is None:
        pad = '  '

    if chomp != '+':
        while kept and not kept[-1].strip():
            kept.pop()

    consumed = sum(len(line) for line in kept)
    raw = [line.rstrip('\r\n') for line in kept]

    newline = '\r\n' if any(ln.endswith('\r\n') for ln in kept) else '\n'

    return BlockScalar(
        style=hm.group('style'),
        indent=len(pad),
        lines=[ln[len(pad):] if ln.strip() else '' for ln in raw],
        end_offset=body_start + consumed,
        pad=pad,
        newline=newline,
    )


def append_fm_block_scalar_line(fm: str, key: str, line: str) -> str:
    if '\n' in line or '\r' in line:
        raise ValueError(
            f'append_fm_block_scalar_line: field "{key}" — *line* must be a '
            f'single line; append multi-line text one line at a time.'
        )

    block = read_fm_block_scalar(fm, key)
    if block is None:
        raise ValueError(
            f'append_fm_block_scalar_line: field "{key}" is absent or does not '
            f'hold a block-scalar ("|"/">") value — nothing to append into.'
        )

    if block.lines and block.lines[-1] == line:
        return fm

    newline = block.newline
    pad = block.pad

    addition = '' if fm[:block.end_offset].endswith(('\n', '\r')) else newline
    if block.style == '>' and block.lines and block.lines[-1].strip():
        addition += newline
    addition += pad + line + newline

    return fm[:block.end_offset] + addition + fm[block.end_offset:]


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
            f'("{truncated}") — a single-line replace would truncate it. '
            f'To add a line, use append_fm_block_scalar_line(fm, "{key}", ...).'
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
            sep = leading_ws if leading_ws else ' '
            new_rest = sep + raw_value
        elif old_value == '':
            new_rest = ' ' + raw_value + leading_ws + comment_suffix
        else:
            new_rest = leading_ws + raw_value + comment_suffix

        return prefix + new_rest + cr

    return pattern.sub(_sub, fm)


def insert_fm_field(
    fm: str,
    key: str,
    v: object,
    after_key: str | None = None,
    *,
    numeric_quoting: bool = False,
) -> str:
    serialized = serialize_yaml_scalar(v, numeric_quoting=numeric_quoting)
    new_line = f'{key}: {serialized}'

    if after_key is not None:
        after_pattern = re.compile(
            r'^' + re.escape(after_key) + r':(?=[ \t]|\r?$).*$',
            re.MULTILINE,
        )
        m = after_pattern.search(fm)
        if m:
            insert_at = m.end()
            cr = '\r' if m.group(0).endswith('\r') else ''
            return fm[:insert_at] + '\n' + new_line + cr + fm[insert_at:]

    # The line ending is detected on the ORIGINAL `fm`, never on the rstrip()ed
    # stripped ending, the existing last line was silently DOWNGRADED to LF too,
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

    The insert branch of
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

    eol = '\r\n' if '\r\n' in fm else '\n'
    trimmed = fm.rstrip()
    return trimmed + eol + f'{key}: {raw_value}' + eol


def remove_fm_field(fm: str, key: str) -> str:
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
    return replace_fm_field(fm, "blocking_notes", note)


def _retire_gate_dependency(fm: str) -> str:
    current_gate_dep = read_fm_field_unquoted(fm, "gate_dependency")
    if current_gate_dep is None:
        return remove_fm_field(fm, "gate_dependency")

    fm = _append_blocking_note(fm, current_gate_dep, "gate_dependency")
    return remove_fm_field(fm, "gate_dependency")


def read_fm_nested_field(fm: str, key: str) -> str | None:
    loc = _locate_nested_block(fm, key)
    if loc is None:
        return None
    _, block_start, block_end = loc
    return fm[block_start:block_end]


def write_fm_nested_field(fm: str, key: str, block_text: str) -> str:
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
    loc = _locate_nested_block(fm, key)
    if loc is None:
        return fm
    key_start, _, block_end = loc
    return fm[:key_start] + fm[block_end:]


def rebuild(split: FrontmatterSplit, fm_text: str) -> str:
    fm_normalized = fm_text if fm_text.endswith('\n') else fm_text + '\n'
    return (
        (split.preamble or '')
        + '---\n'
        + fm_normalized
        + '---'
        + split.body_with_leading_newline
    )


_BODY_DELIMITER_RE = re.compile(r'^---[ \t]*$')


def frontmatter_body_text(file_text: str) -> str:
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
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        return None
    header = f'blob {len(data)}\0'.encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


def canonical_body_sha(file_text: str) -> Optional[str]:
    return git_blob_sha1(frontmatter_body_text(file_text))
