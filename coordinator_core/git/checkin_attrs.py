"""coordinator_core.git.checkin_attrs -- resolve a path's CHECKIN-side text
disposition from `.gitattributes`, in process, so a commit path can produce
the blob git would produce instead of refusing.

WHY THIS IS SMALL AND WHY THAT IS CORRECT. Git's checkin conversion has one
rule that the `eol=` spelling obscures:

    `eol=crlf` and `eol=lf` both mean `text`. They differ ONLY on CHECKOUT.
    On CHECKIN, every `text` path is normalized CRLF -> LF, without
    exception.

So the three dispositions that matter here collapse to three answers:

    TEXT      -- `text`, `text=auto` (with CR content), `eol=lf`, `eol=crlf`
                 => normalize CRLF -> LF, then hash
    BINARY    -- `-text`
                 => hash the RAW bytes, no conversion at all
    UNSET     -- no attribute matches
                 => caller's default (`core.autocrlf`), unchanged

`content_hash._text_attribute_pinned` already FINDS these patterns; it
deliberately returns a refusal diagnostic rather than a value, because its
`_autocrlf_checkin_normalize` corpus was only ever run against paths with no
forced attribute. This module answers the narrower question that function
declined -- WHICH disposition -- so the caller can act instead of refuse.

NEGATIVE SPEC:
- **No `[attr]` macro expansion.** A macro carrying a text directive is
  UNRESOLVED, and resolving it is a second pass. Returns `UNRESOLVED` so the
  caller keeps refusing, exactly as before.
- **No `filter=` / clean-driver handling.** That is `_clean_filter_may_apply`'s
  job and remains a refusal -- an LFS pointer is not a normalization, it is
  different content entirely.
- **Last match wins**, per git: later lines in the same file override earlier
  ones, and a deeper `.gitattributes` overrides a shallower one.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.git.content_hash import _attributes_pattern_matches, _read_text_scoped
from coordinator_core.git.git_dir import resolve_git_dir

TEXT = "text"
BINARY = "binary"
UNSET = "unset"
UNRESOLVED = "unresolved"

_LOCAL_ATTRIBUTES_FILENAME = ".gitattributes"


def checkin_disposition(root: Path, normalized: str) -> str:
    """`TEXT` / `BINARY` / `UNSET` / `UNRESOLVED` for `normalized`.

    Candidate order mirrors `_text_attribute_pinned`'s exactly -- same files,
    same shallow-to-deep walk -- so the two never disagree about WHICH line
    matched, only about what to do with it.
    """
    parent = Path(normalized).parent
    parts = [] if parent == Path(".") else list(parent.parts)

    candidates = [(resolve_git_dir(root) / "info" / "attributes", normalized)]
    for depth in range(len(parts) + 1):
        candidates.append(
            (
                root.joinpath(*parts[:depth]) / _LOCAL_ATTRIBUTES_FILENAME,
                "/".join(normalized.split("/")[depth:]),
            )
        )

    verdict = UNSET
    for candidate, rel_to_attrs_dir in candidates:
        text = _read_text_scoped(candidate)
        if text is None:
            continue
        for line in text.splitlines():
            code = line.split("#", 1)[0].strip()
            if not code:
                continue
            tokens = code.split()
            attr_tokens = tokens[1:] if len(tokens) > 1 else []
            directives = [
                tok
                for tok in attr_tokens
                if tok in ("text", "-text")
                or tok.startswith("text=")
                or tok.startswith("eol=")
            ]
            if not directives:
                continue
            if code.startswith("[attr]"):
                return UNRESOLVED
            if not _attributes_pattern_matches(tokens[0], rel_to_attrs_dir):
                continue
            for tok in directives:
                if tok == "-text":
                    verdict = BINARY
                elif tok == "text" or tok.startswith("eol="):
                    verdict = TEXT
                elif tok == "text=auto":
                    verdict = TEXT
                else:
                    return UNRESOLVED
    return verdict


TEXT_SET = "set"
TEXT_AUTO = "auto"
TEXT_UNSET = "unset"
TEXT_UNSPECIFIED = "unspecified"


class TextPin(NamedTuple):
    """`text` is one of TEXT_SET / TEXT_AUTO / TEXT_UNSET / TEXT_UNSPECIFIED; `eol` is
    None, "lf" or "crlf". The two are separate attributes in git: each is last-match-wins
    on its own, so `* text=auto` then `*.x eol=lf` leaves text=auto with eol=lf."""

    text: str
    eol: Optional[str]


# Attribute spellings that change the checkin conversion but that this resolver does not
# model; any matching line carrying one makes the whole path unresolvable.
_UNMODELLED_TOKENS = ("binary", "crlf", "-crlf", "!text", "!eol", "!crlf")


def resolve_text_pin(root: Path, normalized: str) -> Optional[TextPin]:
    """The `text` state and `eol` value that apply to `normalized`, per attribute, or None
    when a macro, an unmodelled spelling (`binary`, legacy `crlf`, `!text`) or an unknown
    value governs the path. Same candidate files and walk order as `checkin_disposition`."""
    parent = Path(normalized).parent
    parts = [] if parent == Path(".") else list(parent.parts)

    candidates = [(resolve_git_dir(root) / "info" / "attributes", normalized)]
    for depth in range(len(parts) + 1):
        candidates.append(
            (
                root.joinpath(*parts[:depth]) / _LOCAL_ATTRIBUTES_FILENAME,
                "/".join(normalized.split("/")[depth:]),
            )
        )

    text_state = TEXT_UNSPECIFIED
    eol: Optional[str] = None
    for candidate, rel_to_attrs_dir in candidates:
        content = _read_text_scoped(candidate)
        if content is None:
            continue
        for line in content.splitlines():
            code = line.split("#", 1)[0].strip()
            if not code:
                continue
            tokens = code.split()
            attr_tokens = tokens[1:]
            relevant = [
                tok
                for tok in attr_tokens
                if tok in ("text", "-text")
                or tok.startswith("text=")
                or tok.startswith("eol=")
                or tok in _UNMODELLED_TOKENS
            ]
            if not relevant:
                continue
            if code.startswith("[attr]"):
                return None
            if not _attributes_pattern_matches(tokens[0], rel_to_attrs_dir):
                continue
            for tok in relevant:
                if tok == "text":
                    text_state = TEXT_SET
                elif tok == "-text":
                    text_state = TEXT_UNSET
                elif tok == "text=auto":
                    text_state = TEXT_AUTO
                elif tok in ("eol=lf", "eol=crlf"):
                    eol = tok.split("=", 1)[1]
                else:
                    return None
    return TextPin(text_state, eol)
