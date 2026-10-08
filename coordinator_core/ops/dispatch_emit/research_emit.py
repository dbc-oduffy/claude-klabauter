"""Research segments for the dispatch.emit research route: a shape result to ordered (pipeline, inputs).

`segments_for` turns a research.shape result into the ordered `(pipeline, PipelineInputs)` segments
the chain composer runs; `write_ask` writes the ask file the scouts read as `{{brief}}`.
Scout questions are slugged to [a-z0-9-] (at most 48 chars) because the scouts template names
`digest-{{item}}.md`.

Negative-spec: loads and validates no manifest, composes no script, resolves no content root; the
only write is ask.md under the caller's repo-relative scratch dir.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused, PipelineInputs

__all__ = ["ASK_FILE", "MAX_SLUG_LEN", "scout_slugs", "segments_for", "write_ask"]

ASK_FILE = "ask.md"
MAX_SLUG_LEN = 48
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BARE_SLUG = "question"
_ROSTER_LIST = "roster"
_QUESTIONS_LIST = "questions"


def _slug(text: str) -> str:
    return _NON_SLUG_RE.sub("-", text.lower()).strip("-")[:MAX_SLUG_LEN].strip("-")


def scout_slugs(questions: Sequence[str]) -> list[str]:
    """The [a-z0-9-] slug of each question, in order.

    Refuses more than MAX_SCOUT_QUESTIONS, a question with no slug-able character, and two
    questions that slug alike.
    """
    reasons: list[str] = []
    if len(questions) > rc.MAX_SCOUT_QUESTIONS:
        reasons.append(f"{len(questions)} scout questions given; at most {rc.MAX_SCOUT_QUESTIONS} are allowed")
    slugs: list[str] = []
    for index, question in enumerate(questions):
        slug = _slug(question) if isinstance(question, str) else ""
        if not slug:
            reasons.append(f"scout question {index + 1} has no letter or digit to slug")
        elif slug in slugs:
            reasons.append(f"scout question {index + 1} slugs to {slug!r}, which an earlier question already took")
        slugs.append(slug)
    if reasons:
        raise PipelineEmitRefused(reasons)
    return slugs


def write_ask(root: Path, scratch_rel: str, ask: str, questions: Sequence[str] = ()) -> str:
    """Write `<root>/<scratch_rel>/ask.md` and return its repo-relative POSIX path.

    No questions: the file is the ask verbatim. Otherwise the ask is followed by each full question
    under a `## <slug>` heading, the section a scout finds by its `{{item}}`. Refuses an empty ask
    or questions `scout_slugs` refuses.
    """
    if not isinstance(ask, str) or not ask.strip():
        raise PipelineEmitRefused(["the ask is empty"])
    slugs = scout_slugs(questions)
    text = ask
    if slugs:
        text = ask.rstrip("\n") + "".join(f"\n\n## {slug}\n\n{q.strip()}" for slug, q in zip(slugs, questions)) + "\n"
    rel = f"{scratch_rel.rstrip('/')}/{ASK_FILE}"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return rel


def _deep_roster(sources: Sequence[str]) -> list[dict[str, str]]:
    roster = [{"slug": role, "agent_type": rc.UNBLOCK_ROLE_AGENT_TYPE} for role in rc.UNBLOCK_ROLES]
    for source in sources or ("web",):
        agent_type = rc.SOURCE_SPECIALIST.get(source)
        if agent_type and not any(m["slug"] == source for m in roster):
            roster.append({"slug": source, "agent_type": agent_type})
    return roster


def segments_for(
    shape: rc.ShapeResult,
    *,
    brief_rel: str,
    scratch_rel: str,
    questions: Sequence[str] = (),
    sources: Sequence[str] = (),
) -> list[tuple[str, PipelineInputs]]:
    """One `(pipeline, PipelineInputs)` per manifest in `shape["pipelines"]`, in order.

    Every segment binds `brief_rel` and `scratch_rel` and the flags `shape["flags"]` names for its
    pipeline. The scouts segment carries the slugs of `questions` (with none, the one entry
    `question`, the bare ask); the unblock segment carries the deep roster, three role members plus
    one specialist per `sources` entry that has one.
    """
    shaped_flags = shape.get("flags") or {}
    segments: list[tuple[str, PipelineInputs]] = []
    for pipeline in shape["pipelines"]:
        flags: dict[str, str | bool] = dict(shaped_flags.get(pipeline) or {})
        lists: dict[str, tuple] = {}
        if pipeline == rc.SCOUTS_PIPELINE:
            lists[_QUESTIONS_LIST] = tuple(scout_slugs(questions) if questions else [_BARE_SLUG])
        elif pipeline == rc.DEEP_PIPELINE:
            lists[_ROSTER_LIST] = tuple(_deep_roster(sources))
        segments.append(
            (pipeline, PipelineInputs(brief=brief_rel, subjects=(), scratch_dir=scratch_rel, flags=flags, lists=lists))
        )
    return segments
