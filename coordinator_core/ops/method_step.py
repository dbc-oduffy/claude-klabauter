"""Method-step rows: the marker on a register row anchor, the plan's `method-trace` fence, and
the refusal for a step row claimed without a mechanism.

Pure functions over text and dicts; the one file read is the in-tree literal probe. No spawn.

Import discipline: module scope imports only the stdlib and `requirement_register`, whose YAML
loader is reached lazily.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.ops import requirement_register as rr

METHOD_STEP_SEP = "#method-step:"

TRACE_FENCE = re.compile(r"(?ms)^[ \t]*```ya?ml[ \t]+method-trace[ \t]*\n(.*?)^[ \t]*```")

FENCE_SHAPE = (
    "add ```yaml method-trace with row, mechanism (one line), "
    "at: {path, contains}, and deviation: {delivers_by, reason, ratified_by} if the mechanism differs"
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_MIN_WORD = 3
_STOPWORDS = frozenset(
    "a an and are as at be but by can for from has have how if in into is it its not of on or "
    "so than that the their then there these they this to was were when which while will with "
    "you your via per each every any all".split()
)


def mark_anchor(origin: str, slug: str, label: str) -> str:
    """`<origin>#method-step:<slug>/<label>`; the origin text is kept verbatim."""
    return f"{origin}{METHOD_STEP_SEP}{slug}/{label}"


def split_anchor(anchor: Any) -> tuple[str, Optional[str]]:
    """`(origin_anchor, step)`; `step` is `<slug>/<label>`, or None for an unmarked anchor."""
    text = "" if anchor is None else str(anchor)
    origin, sep, step = text.partition(METHOD_STEP_SEP)
    return (origin, step) if sep and step else (text, None)


def is_method_step(row: Mapping[str, Any]) -> bool:
    """True when the row's anchor carries the method-step marker."""
    return split_anchor(row.get(rr.ROW_ANCHOR))[1] is not None


def parse_traces(plan_text: str) -> dict[str, dict[str, Any]]:
    """`{row_id: trace}` from every `method-trace` fence in `plan_text`. A fence that does not
    parse, is not a mapping or names no row is skipped; a later fence for a row wins."""
    traces: dict[str, dict[str, Any]] = {}
    for body in TRACE_FENCE.findall((plan_text or "").replace("\r\n", "\n")):
        try:
            data = rr.load_register_yaml(body)
        except Exception:  # noqa: BLE001 - a malformed fence is "no trace"
            continue
        if isinstance(data, dict) and str(data.get("row") or "").strip():
            traces[str(data["row"]).strip()] = data
    return traces


def _words(text: Any) -> set[str]:
    return {
        w for w in _WORD_RE.findall(str(text or "").lower())
        if len(w) >= _MIN_WORD and w not in _STOPWORDS
    }


def _all_words(text: Any) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


def _probe_refusal(at: Any, repo_root: Path) -> Optional[str]:
    if not isinstance(at, Mapping) or not str(at.get("path") or "") or not str(at.get("contains") or ""):
        return "trace has no at: {path, contains} probe; " + FENCE_SHAPE
    root = Path(repo_root).resolve()
    target = (root / str(at["path"])).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return f"trace at.path {at['path']} escapes the repo; point it at an in-tree file"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"trace at.path {at['path']} is unreadable; point it at the file that delivers the step"
    if str(at["contains"]) not in text:
        return (
            f"trace at.contains {str(at['contains'])!r} is absent from {at['path']}; "
            "the mechanism is not in the tree"
        )
    return None


def trace_refusal(row: Mapping[str, Any], plan_text: str, repo_root: Path) -> Optional[str]:
    """Why a plan claiming method-step `row` is refused, or None when it passes.

    None for a row that is not a method step. Checks in order: a trace exists; its mechanism is
    more than the method and step name; it covers at least half of the step's own words, or
    carries a `deviation` with `ratified_by`; its `at.contains` literal is in `at.path`.
    """
    if not is_method_step(row):
        return None
    rid = str(row.get(rr.ROW_ID))
    step = split_anchor(row.get(rr.ROW_ANCHOR))[1] or ""
    slug, _, label = step.partition("/")
    trace = parse_traces(plan_text).get(rid)
    if trace is None:
        return f"method-step row {rid} has no mechanism trace; {FENCE_SHAPE}"

    mechanism = str(trace.get("mechanism") or "").strip()
    mech_words = _words(mechanism)
    name_words = _all_words(slug) | _all_words(label) | {"step", "method"}
    if not mech_words or mech_words <= name_words:
        return (
            f"method-step row {rid} trace mechanism names the step, not how it is delivered; "
            "state the mechanism in one line"
        )

    deviation = trace.get("deviation")
    if deviation is not None:
        if not isinstance(deviation, Mapping) or not str(deviation.get("ratified_by") or "").strip():
            return f"method-step row {rid} deviation lacks ratified_by; name the PM or APM ruling"
    else:
        step_words = _words(row.get(rr.ROW_TEXT)) - _all_words(slug)
        if step_words and 2 * len(mech_words & step_words) < len(step_words):
            return (
                f"method-step row {rid} mechanism covers under half of the step's words; "
                "deliver the step as stated or add deviation: {delivers_by, reason, ratified_by}"
            )

    probe = _probe_refusal(trace.get("at"), Path(repo_root))
    return f"method-step row {rid}: {probe}" if probe else None
