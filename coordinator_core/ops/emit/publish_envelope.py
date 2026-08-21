"""
coordinator_core.ops.emit.publish_envelope — byte-splice the publish envelope onto the
emission body, without parsing it.

Purpose: build the document cockpit's sink stores by splicing three fields —
``repo_slug``, ``published_at``, ``producer`` — onto the TOP LEVEL of an already-assembled
frozen-emission-v2 body, as sibling fields on the same object (not a nested ``body`` key).
Per the far-side spec (``example-cockpit-repo/docs/wiki/per-repo-emission-sink.md`` § Doc body),
the envelope is "added alongside the emission body ... sibling fields on the same [object]".

DESIGN IS FIXED BY THE PERFORMANCE PLAN. Measured: parse+re-serialise the emission body is
~281ms process time against ~31ms for a byte-level splice — a 9x cost this module exists to
avoid (DR-344 brightline). The body's bytes are never rewritten, which also makes AC4 true
by construction: every section's content survives unchanged because it is never touched.
A re-serialising (``json.loads`` + ``json.dumps``) implementation is a rejected design here,
not an alternative — do not reach for one to "simplify" a future edit.

Negative-spec: this module does NOT validate the emission body's shape beyond the minimal
splice-safety guards below. A malformed on-disk artifact (corrupt JSON deeper than the
guarded prefix/suffix, a truncated section) is not caught here — it surfaces downstream, at
Cockpit's endpoint, or lands as a corrupt document in cockpit's sink carrying this repo's
name. That is an accepted trade for the 9x, not an oversight. Do NOT add a validating parse
to close this gap — that reintroduces the ~281ms this design exists to avoid.

``schema_version`` is the one field that overlaps by name with the spliced envelope and is
deliberately NOT spliced: the body already carries it (the far-side spec says the published
document carries "the same field name and semantics as the local emission's own
schema_version"), and a duplicate top-level JSON key is last-wins in Python/JS and rejected
outright by stricter parsers. Instead this module VERIFIES the body's own ``schema_version``
against the real comparand — the vendored cockpit-contract version
(``validate.read_schema_version()``), NOT a value re-derived from the body itself (that would
be a vacuous self-comparison masquerading as a safety guard). Disagreement fails loud.

Bounded head-scan, not a full parse: ``schema_version`` and ``emitted_at`` are read via a
bounded byte-window regex scan, safe today only because ``envelope.py``'s dict literal opens
with ``schema_version`` then ``emitted_at`` and the writer uses ``json.dumps(..., indent=2)``
with no ``sort_keys`` — CPython dict insertion order pins that ordering. Nothing enforces
that staying true, so the scan is bound-limited (fails loud if the key is not found inside
the bound, never silently falls back to a full parse) and depth-checked (the match must sit
at top-level nesting depth 1, not inside a nested object/array) rather than trusted blindly
by position. See ``tests/test_envelope_schema_version_key_order.py`` for the ordering pin.

``producer`` is composed at runtime from the caller-supplied ``repo`` (never a string
literal): this repo's own name, written as a quoted string literal in this file's source,
would be silently rewritten to its mirror's repo token by the percolate depersonalize
transform when this repo is published
to its mirror — the mirror is what actually runs in production — producing a ``producer``
value that never matches this repo's real identity. Composing it from runtime data (the
caller's ``repo`` argument, itself sourced from the emission body's own ``repo`` field per
C1's ``publish_identity`` convention) gives the transform no string literal to touch.

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C2 dispatch brief.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.publish_identity import repo_slug as _repo_slug
from coordinator_core.warm.engine_root import current_engine_clone
from coordinator_core.warm.skew import read_engine_stamp_sha

_HEAD_SCAN_BOUND = 4096

_WHITESPACE = b" \t\r\n"
#: Same set as `_WHITESPACE`, as ints, for index-based scanning of a multi-ten-MB
#: buffer -- `raw[i]` yields an int, and membership against a frozenset avoids the
#: full-size copy every `bytes.strip()` call would allocate.
_WHITESPACE_BYTES = frozenset(_WHITESPACE)

# JSON structural bytes tracked by the bounded nesting-depth scan.
_QUOTE = 0x22
_BACKSLASH = 0x5C
_OPEN_BRACE = 0x7B
_CLOSE_BRACE = 0x7D
_OPEN_BRACKET = 0x5B
_CLOSE_BRACKET = 0x5D


class PublishEnvelopeError(ValueError):
    """Raised for any splice-safety guard failure or a schema_version disagreement.

    Fails loud by design (matches ``publish_transport.PublishTransportError``'s posture) —
    this module never silently degrades to a partial or best-effort splice.
    """


def _bounded_head_scan_key(raw: bytes, key: str, bound: int) -> str:
    """Return the string value of ``key`` found within the first ``bound`` bytes of ``raw``.

    Fails loud (does not fall back to a full parse) if the key is not found inside the
    bound, or is found but does not sit at top-level (depth 1) nesting.
    """
    window = raw[:bound]
    pattern = re.compile(
        b'"' + re.escape(key.encode("utf-8")) + b'"\\s*:\\s*"(?:[^"\\\\]|\\\\.)*"'
    )
    match = pattern.search(window)
    if match is None:
        raise PublishEnvelopeError(
            f"{key!r} not found within the first {bound} bytes of the emission artifact "
            "(bounded head-scan) -- refusing to fall back to a full parse"
        )
    depth = _nesting_depth_at(raw, match.start())
    if depth != 1:
        raise PublishEnvelopeError(
            f"{key!r} found at nesting depth {depth}, expected top-level depth 1"
        )
    _, _, value_literal = match.group(0).partition(b":")
    return json.loads(value_literal.strip())


def _nesting_depth_at(raw: bytes, pos: int) -> int:
    """Return the brace/bracket nesting depth immediately before byte offset ``pos``.

    A minimal bytewise scan (not a JSON parser): tracks string-literal state (honouring
    backslash escapes) so braces/brackets inside string values never perturb the count.
    """
    depth = 0
    in_string = False
    escape = False
    for byte in raw[:pos]:
        if in_string:
            if escape:
                escape = False
            elif byte == _BACKSLASH:
                escape = True
            elif byte == _QUOTE:
                in_string = False
            continue
        if byte == _QUOTE:
            in_string = True
        elif byte in (_OPEN_BRACE, _OPEN_BRACKET):
            depth += 1
        elif byte in (_CLOSE_BRACE, _CLOSE_BRACKET):
            depth -= 1
    return depth


def _resolve_producer_stamp(engine_root: Optional[Path]) -> str:
    """Render the ``<stamp>`` component of ``producer`` per the dispatch brief's tri-state.

    Reader returns a sha string -> that sha (covers the literal ``unpinned`` stamp-file
    contents case too, since the reader strips only the ``sha:`` prefix). Reader returns
    ``None`` (stamp file absent -- the live-tree case) -> the literal string ``"live"``.
    """
    root = engine_root if engine_root is not None else current_engine_clone()
    stamp_sha = read_engine_stamp_sha(root)
    return stamp_sha if stamp_sha is not None else "live"


def splice_publish_envelope(
    raw: bytes,
    *,
    owner: str,
    repo: str,
    engine_root: Optional[Path] = None,
) -> bytes:
    """Byte-splice ``repo_slug``/``published_at``/``producer`` onto ``raw`` without parsing it.

    ``raw`` is the exact on-disk bytes of an already-written frozen-emission-v2 artifact.
    Returns the spliced document bytes; never mutates ``raw`` or re-serialises any of it.

    Raises ``PublishEnvelopeError`` for: a BOM-prefixed artifact, a first non-whitespace
    byte other than ``{``, a last non-whitespace byte other than ``}``, a missing/misplaced
    ``schema_version`` or ``emitted_at`` key inside the bounded head-scan, or a
    ``schema_version`` disagreeing with the vendored cockpit-contract pin.
    """
    # Bound the document by INDEX, never by slicing. `raw` is tens of MB; `raw.strip()`
    # and friends each materialise a second full-size copy, and the memory floor this
    # module is held to (plan § Trigger model: "must avoid doubling the buffer") is a
    # per-invocation transient on a box sized for 50-70 concurrent sessions.
    start = 0
    end = len(raw)
    while start < end and raw[start] in _WHITESPACE_BYTES:
        start += 1
    while end > start and raw[end - 1] in _WHITESPACE_BYTES:
        end -= 1

    if start == end:
        raise PublishEnvelopeError("emission artifact is empty")
    if raw[start : start + 3] == b"\xef\xbb\xbf":
        raise PublishEnvelopeError(
            "emission artifact carries a UTF-8 BOM -- refusing to splice a BOM-prefixed "
            "document (rejected by strict parsers on the far side)"
        )
    if raw[start : start + 1] != b"{":
        raise PublishEnvelopeError(
            f"emission artifact's first non-whitespace byte is {raw[start:start + 1]!r}, not b'{{'"
        )
    if raw[end - 1 : end] != b"}":
        raise PublishEnvelopeError(
            f"emission artifact's last non-whitespace byte is {raw[end - 1:end]!r}, not b'}}'"
        )

    schema_version = _bounded_head_scan_key(raw, "schema_version", _HEAD_SCAN_BOUND)
    emitted_at = _bounded_head_scan_key(raw, "emitted_at", _HEAD_SCAN_BOUND)

    expected_schema_version = validate.read_schema_version()
    if schema_version != expected_schema_version:
        raise PublishEnvelopeError(
            f"emission body schema_version {schema_version!r} disagrees with the vendored "
            f"cockpit-contract version {expected_schema_version!r}"
        )

    slug = _repo_slug(owner, repo)
    stamp = _resolve_producer_stamp(engine_root)
    producer = f"{repo}@{stamp}"

    brace_idx = start
    # Is the object non-empty? Answer it by scanning, not by materialising `inner`:
    # `stripped[1:-1].strip()` allocated a THIRD full-size copy purely to be tested
    # for truthiness.
    probe = start + 1
    last = end - 1
    while probe < last and raw[probe] in _WHITESPACE_BYTES:
        probe += 1
    has_inner = probe < last

    envelope_fields = (
        f'"repo_slug": {json.dumps(slug)}, '
        f'"published_at": {json.dumps(emitted_at)}, '
        f'"producer": {json.dumps(producer)}'
    ).encode("utf-8")
    separator = b", " if has_inner else b""

    # Assemble through memoryview slices: `bytes.join` sizes the result once and copies
    # each piece straight in, where `raw[:i] + fields + raw[i:]` would materialise the
    # tail slice AND a left-to-right intermediate before the result. Views, not copies,
    # so the only full-size allocation here is the returned document itself.
    view = memoryview(raw)
    return b"".join(
        (view[: brace_idx + 1], envelope_fields, separator, view[brace_idx + 1 :])
    )
