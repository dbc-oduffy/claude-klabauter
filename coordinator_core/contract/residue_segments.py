"""
coordinator_core.contract.residue_segments — the shared, caller-agnostic
segment-loading mechanism factored out of
`coordinator_core.review_assemble.residue`.

Purpose: list a directory, parse each file's YAML frontmatter as that
segment's registration, filter on ONE caller-named frontmatter field against
a caller-supplied set of active values, sort by declared `order:`, and
fail loud on anything malformed. This is entirely generic — no consumer's
vocabulary (a filter field name, a filter value, a segment directory) is
fixed here. `coordinator_core.review_assemble.residue` is the first caller,
parameterising this loader with `segment_dir="skills/review/residue"` and
its own resolution ladder for the filter field and active-value set; a
second consumer (e.g. `/handoff`'s `case:`-keyed segment corpus)
parameterises it identically, with its own directory, field name, and
legal-value enum.

Segment contract (frozen, caller-agnostic): each file directly under the
segment directory carries YAML frontmatter with exactly four keys —

    ---
    segment_id: <stable-kebab-case-id, unique within the segment directory>
    <filter_key>: <one of legal_values>
    class: protected | droppable
    order: <integer>
    ---

The directory listing IS the manifest — there is no separate registry file
a segment must additionally appear in. A segment's own frontmatter is its
registration; this module discovers segments purely by listing the
directory.

`source_path` is emitted RELATIVE to `content_root` (e.g.
`skills/review/residue/plan-triage-and-exit.md`), never absolute — an
absolute operator filesystem path leaks the operator's home directory and
is not portable across machines (breaking fixture-based testing of the
emission).

Negative-spec:
  - Does NOT resolve the filter field's active values, or the segment
    directory's location, on its own — both are caller-supplied. A caller's
    own resolution ladder (e.g. an explicit-flag/artifact-shape/diff-probe
    precedence chain) stays in the caller; this module has no opinion about
    where an active value came from.
  - Does NOT offer a tolerant/`strict=False` posture. Every fail-loud below
    is unconditional — a malformed segment is always an error, never a
    silent skip. A consumer needing a tolerant posture (e.g. a prompt-
    expansion hook that must never block on a malformed segment) implements
    that tolerance at its own seam, not by asking this module to degrade.
  - Does NOT know any consumer's own catch-all/wildcard filter value. That
    is exactly one more member of the caller's `active_values` set — this
    module filters `segment[filter_key] in active_values` and nothing more.

Spec backlink: docs/plans/2026-08-13-shared-residue-segment-loader.md, chunk C1
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence, Set

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)

#: The two legal values of a segment's `class:` frontmatter field. Preserved
#: verbatim in emitted segments so a budget-limited consumer can degrade a
#: `droppable` segment before ever touching a `protected` one. Universal
#: across every consumer — unlike the filter field, `class` is not
#: parameterised.
SEGMENT_CLASSES: tuple[str, ...] = ("protected", "droppable")


class SegmentLoadError(RuntimeError):
    """Raised for any fail-loud condition this module owns directly — an
    unreadable/missing segment directory, an empty segment directory, a
    segment with malformed or missing frontmatter, an out-of-enum `class`,
    an out-of-enum filter value, or a non-integer `order`. Never caught-and-
    continued past; a caller sees this as a process failure, not a
    recoverable runtime condition."""


def _list_segment_files(segment_dir: Path) -> list[Path]:
    """The directory listing IS the manifest — every non-hidden regular
    file directly under *segment_dir*, sorted by filename for a stable
    parse order (final ordering is still `order:`-driven, this only makes
    parse-time errors reproducible)."""
    if not segment_dir.is_dir():
        # Negative-spec: "residue directory not found" is pinned verbatim by
        # test_absent_residue_directory_raises_fail_loud_not_empty_envelope
        # in coordinator_core/review_assemble/test_residue.py — changing
        # this substring breaks that test with no local signal.
        raise SegmentLoadError(
            f"residue-segments: residue directory not found: {segment_dir}\n"
            "  Run: coordinator:install OR verify the content root resolves to a "
            "checkout that carries this segment directory."
        )
    return sorted(
        p for p in segment_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def _parse_segment(
    path: Path,
    content_root: Path,
    *,
    filter_key: str,
    legal_values: Sequence[str],
) -> dict[str, Any]:
    """Parse one segment file's frontmatter + body.

    Raises `SegmentLoadError` on missing/unparseable frontmatter, a missing
    required key, or an out-of-enum `<filter_key>`/`class` value — a
    segment's frontmatter is its registration, so a malformed one is a
    fail-loud authoring bug, never silently skipped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SegmentLoadError(f"{path}: could not read ({exc})") from exc

    split = split_frontmatter(text)
    if split is None:
        raise SegmentLoadError(f"{path}: no parseable frontmatter")

    fm = split.fm_text
    segment_id = read_fm_field_unquoted(fm, "segment_id")
    filter_value = read_fm_field_unquoted(fm, filter_key)
    segment_class = read_fm_field_unquoted(fm, "class")
    order_raw = read_fm_field_unquoted(fm, "order")

    missing = [
        key
        for key, value in (
            ("segment_id", segment_id),
            (filter_key, filter_value),
            ("class", segment_class),
            ("order", order_raw),
        )
        if value is None
    ]
    if missing:
        raise SegmentLoadError(
            f"{path}: missing required frontmatter field(s): {missing}"
        )

    if filter_value not in legal_values:
        raise SegmentLoadError(
            f"{path}: {filter_key}={filter_value!r} not one of {tuple(legal_values)}"
        )
    if segment_class not in SEGMENT_CLASSES:
        raise SegmentLoadError(
            f"{path}: class={segment_class!r} not one of {SEGMENT_CLASSES}"
        )
    try:
        order = int(order_raw)
    except ValueError as exc:
        raise SegmentLoadError(
            f"{path}: order={order_raw!r} is not an integer"
        ) from exc

    return {
        "segment_id": segment_id,
        filter_key: filter_value,
        "class": segment_class,
        "order": order,
        "content": split.body_with_leading_newline.lstrip("\n"),
        "source_path": path.relative_to(content_root).as_posix(),
    }


def _load_segments(
    segment_dir: Path,
    content_root: Path,
    *,
    filter_key: str,
    legal_values: Sequence[str],
) -> list[dict[str, Any]]:
    """Parse every segment file under *segment_dir*. Raises
    `SegmentLoadError` if the directory holds zero segment files — an
    empty segment set is never silently treated as "nothing to say"."""
    files = _list_segment_files(segment_dir)
    if not files:
        raise SegmentLoadError(
            f"residue-segments: {segment_dir} contains no segment files"
        )
    return [
        _parse_segment(path, content_root, filter_key=filter_key, legal_values=legal_values)
        for path in files
    ]


def _select_segments(
    segments: list[dict[str, Any]],
    *,
    filter_key: str,
    active_values: Set[str],
) -> list[dict[str, Any]]:
    """Filter *segments* to those whose `<filter_key>` value is in
    *active_values*, sorted ascending by declared `order`. A caller's own
    catch-all value is just one more member of *active_values* — this
    module has no opinion about it."""
    applicable = [segment for segment in segments if segment[filter_key] in active_values]
    return sorted(applicable, key=lambda segment: segment["order"])


def load_segments(
    content_root: Path,
    segment_dir: str,
    *,
    filter_key: str,
    legal_values: Sequence[str],
) -> list[dict[str, Any]]:
    """Load and parse every segment file under
    ``content_root / segment_dir``.

    `filter_key` names the frontmatter field a caller filters segments on
    (e.g. `"case"`); `legal_values` is that field's closed enum — any other
    value is fail-loud, not silently accepted.

    Raises `SegmentLoadError` on a missing/unreadable segment directory, an
    empty segment directory, unparseable frontmatter, a missing required
    field, an out-of-enum `class`, an out-of-enum filter value, or a
    non-integer `order`.
    """
    resolved_dir = content_root / segment_dir
    return _load_segments(
        resolved_dir, content_root, filter_key=filter_key, legal_values=legal_values
    )


def select_segments(
    segments: list[dict[str, Any]],
    *,
    filter_key: str,
    active_values: Set[str],
) -> list[dict[str, Any]]:
    """Filter `segments` (as returned by `load_segments`) to those whose
    `filter_key` value is in `active_values`, sorted ascending by declared
    `order`.

    Negative-spec: not fail-loud on zero matches. An empty or non-matching
    `active_values` yields `[]` silently — this function has no opinion on
    whether zero applicable segments is an error. A consumer that treats
    that as a failure condition must check for it and raise itself, as
    `coordinator_core.review_assemble.residue`'s caller does after calling
    this function.
    """
    return _select_segments(segments, filter_key=filter_key, active_values=active_values)
