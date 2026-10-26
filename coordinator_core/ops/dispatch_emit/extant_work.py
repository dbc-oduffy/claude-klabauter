"""Deterministic lookup of work that already exists for an ask, scoped to the sizing's own surface.

``lookup_extant`` opens only the paths the ask's slug, the sizing's stem and the sizing's own edges
name, one read each: ``docs/plans/<slug>.md`` and ``<stem>.md``, ``state/sizings/<slug>.yaml`` and
``<stem>.yaml``, the sizing's ``plan:`` and ``baton:`` edges, ``state/warp/<run>/`` ledgers for runs
the caller or the sizing names, and ``docs/wiki`` paths the sizing text names. It returns exact hits
(slug or ask-hash match) and near hits (token overlap over the records those reads reached).

Negative spec: no directory listing or tree walk of any corpus (``os.scandir``, ``os.listdir``,
glob); no subprocess, model call or RAG query (DR-344). "Semantic match" is the token overlap
score here; a model only judges the near band, in a leg that already runs.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterator, Optional

#: Plan statuses that count as an accepted plan. plan.schema.json has no ``accepted`` status.
ACCEPTED_PLAN_STATUSES = frozenset({"approved", "executing", "landed", "implemented"})

#: Handoff statuses the handoff schema's ``status`` enum treats as not closed (pinned by test).
OPEN_HANDOFF_STATUSES = frozenset({"open", "claimed"})

#: A non-exact candidate is a near hit when its token-overlap score is at least NEAR_MIN.
NEAR_MIN = 0.25
#: Cap on hits kept per bucket, best score first.
MAX_NEAR = 8
MAX_RELATED = 12
#: Cap on wiki pages read from the sizing's own text.
MAX_WIKI_READS = 8

#: Bytes read per candidate; the head carries the keys this module reads.
HEAD_BYTES = 2048
#: Bytes of the sizing record read for its edges and wiki citations.
SIZING_BYTES = 65536

_STOPWORDS = frozenset(
    "this that with from have been will into only when then than they them their there which "
    "what where does done each every also such must should would could about after before "
    "never always without within stop plan plans work".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")
_WIKI_RE = re.compile(r"docs/wiki/[A-Za-z0-9_./-]+?\.md")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ExtantHit:
    """One piece of extant work. ``kind``: plan|sizing|handoff|ledger|wiki; ``match``: exact|near;
    ``key``: slug|ask_hash|text; ``plan_status`` is set for plan hits only."""

    kind: str
    path: str
    match: str
    key: str
    score: float
    plan_status: Optional[str] = None


@dataclass(frozen=True)
class ExtantLookup:
    """``exact``: slug/ask-hash hits; ``near``: token-overlap hits inside the band (non-wiki);
    ``related``: every non-exact hit plus every wiki hit (a wiki is never an
    exact route), best first."""

    exact: tuple = ()
    near: tuple = ()
    related: tuple = ()


def compute_ask_hash(words: str) -> str:
    """sha256 of the UTF-8 ask words: the ``ask_sha256`` derivation ``terminal_commit`` writes."""
    return hashlib.sha256(words.encode("utf-8")).hexdigest()


def _tokens(text: str) -> frozenset:
    return frozenset(
        t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 4 and t not in _STOPWORDS
    )


def _score(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _read(path: str, limit: int) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _head_keys(text: str, *, strip_fence: bool) -> dict:
    """Top-level ``key: value`` pairs of a frontmatter/YAML head; indented continuation lines
    (folded scalars, wrapped values) join onto their key."""
    lines = text.splitlines()
    if strip_fence:
        if not lines or lines[0].strip() != "---":
            return {}
        body = []
        for line in lines[1:]:
            if line.strip() == "---":
                break
            body.append(line)
        lines = body
    out: dict = {}
    key: Optional[str] = None
    for line in lines:
        m = _KEY_RE.match(line)
        if m and not line.startswith((" ", "\t")):
            key = m.group(1)
            out[key] = m.group(2)
        elif key is not None and line.startswith((" ", "\t")):
            out[key] += " " + line.strip()
    return {k: _unquote(v.strip().lstrip(">|-").strip()) for k, v in out.items()}


def _slug_match(stem: str, slug: str) -> bool:
    return bool(slug) and (stem == slug or stem.endswith("-" + slug))


def _safe_rel(rel: str) -> str:
    """``rel`` normalised to a repo-relative posix path, or "" when it escapes the repo."""
    rel = (rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
        return ""
    return rel


def _classify(
    kind: str,
    rel: str,
    stem: str,
    text: str,
    own_hash: Optional[str],
    slug: str,
    want_hash: str,
    want_tokens: frozenset,
    plan_status: Optional[str] = None,
    named_by_edge: bool = False,
) -> Optional[ExtantHit]:
    if named_by_edge or _slug_match(stem, slug):
        return ExtantHit(kind, rel, "exact", "slug", 1.0, plan_status)
    if want_hash and own_hash == want_hash:
        return ExtantHit(kind, rel, "exact", "ask_hash", 1.0, plan_status)
    score = _score(want_tokens, _tokens(stem.replace("-", " ") + " " + text))
    if score >= NEAR_MIN:
        return ExtantHit(kind, rel, "near", "text", round(score, 4), plan_status)
    return None


def _stem_of(sizing_rel: str) -> str:
    base = sizing_rel.rsplit("/", 1)[-1]
    return base[: -len(".yaml")] if base.endswith(".yaml") else ""


def _candidates(
    repo_root: str,
    slug: str,
    want_hash: str,
    want_tokens: frozenset,
    sizing_rel: str,
    run_ids: tuple,
) -> Iterator[ExtantHit]:
    def cls(*args, **kw):
        return _classify(*args, slug=slug, want_hash=want_hash, want_tokens=want_tokens, **kw)

    def full(rel: str) -> str:
        return os.path.join(repo_root, *rel.split("/"))

    stem = _stem_of(sizing_rel)
    sizing_keys: dict = {}
    sizing_text = ""
    if sizing_rel:
        sizing_text = _read(full(sizing_rel), SIZING_BYTES)
        sizing_keys = _head_keys(sizing_text, strip_fence=False)

    seen: set = set()

    def once(rel: str) -> bool:
        if not rel or rel in seen:
            return False
        seen.add(rel)
        return True

    # Plans: the slug- and stem-keyed files, then the sizing's own `plan:` edge.
    edge_plan = _safe_rel(sizing_keys.get("plan", ""))
    plan_rels = [f"docs/plans/{n}.md" for n in (stem, slug) if n and _SAFE_NAME_RE.match(n)]
    for rel in plan_rels + ([edge_plan] if edge_plan else []):
        if not once(rel):
            continue
        keys = _head_keys(_read(full(rel), HEAD_BYTES), strip_fence=True)
        if not keys:
            continue
        pstem = rel.rsplit("/", 1)[-1][:-3]
        text = keys.get("title", "") + " " + keys.get("deliverable_id", "")
        hit = cls("plan", rel, pstem, text, None, plan_status=keys.get("status") or None,
                  named_by_edge=rel == edge_plan)
        if hit:
            yield hit

    # Sizings: the slug-/stem-keyed records. The sizing being routed is never its own hit.
    own = _safe_rel(sizing_rel)
    for n in (stem, slug):
        rel = f"state/sizings/{n}.yaml" if n and _SAFE_NAME_RE.match(n) else ""
        if rel == own or not once(rel):
            continue
        keys = _head_keys(_read(full(rel), HEAD_BYTES), strip_fence=False)
        if not keys:
            continue
        intent = keys.get("intent", "")
        hit = cls("sizing", rel, n, keys.get("name", "") + " " + intent,
                  compute_ask_hash(intent) if intent else None)
        if hit:
            yield hit

    # Handoff: the sizing's `baton:` edge, only while open.
    baton = _safe_rel(sizing_keys.get("baton", ""))
    if once(baton):
        keys = _head_keys(_read(full(baton), HEAD_BYTES), strip_fence=True)
        if keys.get("status") in OPEN_HANDOFF_STATUSES:
            text = keys.get("title", "") + " " + keys.get("summary", "")
            hit = cls("handoff", baton, baton.rsplit("/", 1)[-1][:-3], text, None, named_by_edge=True)
            if hit:
                yield hit

    # Ledgers: runs the caller or the sizing record names.
    named_runs = list(run_ids)
    for k in ("run_id", "warp_run"):
        if sizing_keys.get(k):
            named_runs.append(sizing_keys[k])
    for run in named_runs:
        if not _SAFE_NAME_RE.match(str(run)):
            continue
        rel = f"state/warp/{run}/relayed-ask.md"
        if not once(rel):
            continue
        ask_text = _read(full(rel), SIZING_BYTES)
        if not ask_text:
            continue
        hit = cls("ledger", rel, "", ask_text, compute_ask_hash(ask_text))
        if hit:
            yield hit

    # Wiki: only pages the sizing text itself cites.
    for rel in sorted(set(_WIKI_RE.findall(sizing_text)))[:MAX_WIKI_READS]:
        if not once(rel):
            continue
        head = _read(full(rel), HEAD_BYTES)
        if not head:
            continue
        keys = _head_keys(head, strip_fence=True)
        heading = re.search(r"^# (.+)$", head, re.M)
        text = keys.get("title", "") + " " + (heading.group(1) if heading else "")
        hit = cls("wiki", rel, rel.rsplit("/", 1)[-1][:-3], text, None)
        if hit:
            yield hit


def lookup_extant(
    repo_root: str,
    slug: str,
    ask_hash: str,
    intent: str,
    *,
    sizing_rel: str = "",
    run_ids: tuple = (),
) -> ExtantLookup:
    """Exact (slug or ask-hash) and near (token-overlap >= ``NEAR_MIN``) extant work for an ask.

    ``sizing_rel`` (repo-relative) unlocks the sizing's stem and its ``plan:``/``baton:`` edges;
    without it only the slug-keyed paths are read. Keyed file reads only; never raises on a
    missing or unreadable path.
    """
    want_tokens = _tokens(intent)
    exact, near, related = [], [], []
    for hit in _candidates(str(repo_root), slug, ask_hash, want_tokens,
                           _safe_rel(sizing_rel), tuple(run_ids)):
        if hit.kind == "wiki":
            related.append(hit)
        elif hit.match == "exact":
            exact.append(hit)
        else:
            related.append(hit)
            near.append(hit)
    best = lambda h: (-h.score, h.path)  # noqa: E731
    return ExtantLookup(
        exact=tuple(sorted(exact, key=lambda h: (h.kind, h.path))),
        near=tuple(sorted(near, key=best)[:MAX_NEAR]),
        related=tuple(sorted(related, key=best)[:MAX_RELATED]),
    )
