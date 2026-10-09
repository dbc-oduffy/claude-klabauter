"""Research segments for the dispatch.emit research route: a shape result to ordered (pipeline, inputs).

`segments_for` turns a research.shape result into the ordered `(pipeline, PipelineInputs)` segments
the chain composer runs; `write_ask` writes the ask file the scouts read as `{{brief}}`;
`bind_context` names the caller's non-corpus context files in the brief every member reads.
Scout questions are slugged to [a-z0-9-] (at most 48 chars) because the scouts template names
`digest-{{item}}.md`.

Negative-spec: loads and validates no manifest, composes no script, resolves no content root; the
only writes are ask.md and brief.md under the caller's repo-relative scratch dir.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Sequence

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused, PipelineInputs

__all__ = ["ASK_FILE", "BRIEF_FILE", "CORPUS_DEFAULT_LISTS", "corpus_default_lists", "MAX_SLUG_LEN", "bind_context", "scout_slugs", "segments_for", "write_ask"]

ASK_FILE = "ask.md"
BRIEF_FILE = "brief.md"
_CONTEXT_HEADING = "## Context files"
MAX_SLUG_LEN = 48
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BARE_SLUG = "question"
_ROSTER_LIST = "roster"
_QUESTIONS_LIST = "questions"
_NOTEBOOKS_LIST = "notebooks"
_NOTEBOOKLM_PIPELINE = "notebooklm"

# The corpus scope templates fix these shapes: the repo scope defines exactly four chunks A-D
# (spanning every target, by the repos' architecture, not one per target) and assigns them to
# two Haiku scouts; the web scope defaults to four topics a-d. A caller's --list overrides.
CORPUS_DEFAULT_LISTS: dict[str, dict[str, tuple[str, ...]]] = {
    "repo": {"chunks": ("A", "B", "C", "D"), "haiku_scouts": ("1", "2")},
    "web": {"topics": ("a", "b", "c", "d")},
}


def corpus_default_lists(pipeline: str, questions: Sequence[str] = ()) -> dict[str, tuple[str, ...]]:
    """`CORPUS_DEFAULT_LISTS[pipeline]`, with web topics seeded one letter per research question."""
    lists = dict(CORPUS_DEFAULT_LISTS.get(pipeline, {}))
    if pipeline == "web" and questions:
        lists["topics"] = tuple("abcdefghijklmnopqrstuvwxyz"[: len(questions)])
    return lists


def _slug(text: str) -> str:
    return _NON_SLUG_RE.sub("-", text.lower()).strip("-")[:MAX_SLUG_LEN].strip("-")


def topic_slug(ask: str = "", sizing_rel: str = "") -> str:
    """The run's `research.close` topic: the sizing's stem without its date, else the ask's slug."""
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(sizing_rel).stem) if sizing_rel else ""
    return _slug(stem or ask) or _BARE_SLUG


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


def bind_context(root: Path, scratch_rel: str, brief_rel: str, context: Sequence[str]) -> str:
    """Name `context` files in the brief; return the brief path members read.

    The ask file gains a `## Context files` section in place. Any other brief (a sizing) is never
    edited: `<scratch>/brief.md` points at it and carries the section, and that path is returned.
    Paths under `root` are written repo-relative, others absolute. Refuses a file that is missing.
    """
    if not context:
        return brief_rel
    names, missing = [], []
    for raw in context:
        path = Path(raw) if Path(raw).is_absolute() else root / raw
        if not path.is_file():
            missing.append(f"context file {raw} does not exist")
            continue
        try:
            names.append(path.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            names.append(path.resolve().as_posix())
    if missing:
        raise PipelineEmitRefused(missing)
    section = (
        f"{_CONTEXT_HEADING}\n\nRead each before you start; they are sources outside every corpus.\n\n"
        + "".join(f"- `{name}`\n" for name in names)
    )
    ask_rel = f"{scratch_rel.rstrip('/')}/{ASK_FILE}"
    if brief_rel == ask_rel:
        target = root / ask_rel
        text = target.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + section
        target.write_text(text, encoding="utf-8", newline="\n")
        return brief_rel
    rel = f"{scratch_rel.rstrip('/')}/{BRIEF_FILE}"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"The brief is `{brief_rel}`; read it first.\n\n{section}", encoding="utf-8", newline="\n")
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
    targets: Sequence[Mapping[str, str]] = (),
) -> list[tuple[str, PipelineInputs]]:
    """One `(pipeline, PipelineInputs)` per manifest in `shape["pipelines"]`, in order.

    Every segment binds `brief_rel` and `scratch_rel` and the flags `shape["flags"]` names for its
    pipeline. The scouts segment carries the slugs of `questions` (with none, the one entry
    `question`, the bare ask); the unblock segment carries the deep roster, three role members plus
    one specialist per `sources` entry that has one; the notebooklm segment carries the refs of
    the `targets` whose source is notebooklm as its notebooks. `CORPUS_DEFAULT_LISTS` is bound by
    the caller, which sees which lists each loaded manifest declares.
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
        elif pipeline == _NOTEBOOKLM_PIPELINE:
            refs = tuple(t["ref"] for t in targets if t.get("source") == _NOTEBOOKLM_PIPELINE)
            if refs:
                lists[_NOTEBOOKS_LIST] = refs
        segments.append(
            (pipeline, PipelineInputs(brief=brief_rel, subjects=(), scratch_dir=scratch_rel, flags=flags, lists=lists))
        )
    return segments
