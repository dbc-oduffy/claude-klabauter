"""Deterministic lookup of work that already exists for an ask.

``lookup_extant`` scans frontmatter heads (bounded byte reads, ``os.scandir``) of
``docs/plans``, ``state/sizings``, open ``state/handoffs``, ``state/warp/*`` run ledgers and
``docs/wiki`` and returns exact hits (slug or ask-hash match) and near hits (token overlap).

Negative spec: no subprocess, model call or RAG query (DR-344). "Semantic match" is the token
overlap score here; a model only judges the near band, in a leg that already runs.
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

#: Bytes read per candidate; the head carries the keys this module reads.
HEAD_BYTES = 2048

_STOPWORDS = frozenset(
    "this that with from have been will into only when then than they them their there which "
    "what where does done each every also such must should would could about after before "
    "never always without within stop plan plans work".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")


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


def _read_head(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(HEAD_BYTES).decode("utf-8", errors="replace")
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


def _scan(directory: str, suffix: str) -> Iterator[os.DirEntry]:
    try:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.endswith(suffix) and entry.is_file():
            yield entry


def _slug_match(stem: str, slug: str) -> bool:
    return bool(slug) and (stem == slug or stem.endswith("-" + slug))


def _rel(repo_root: str, path: str) -> str:
    return os.path.relpath(path, repo_root).replace(os.sep, "/")


def _classify(
    kind: str,
    repo_root: str,
    path: str,
    stem: str,
    name: str,
    text: str,
    own_hash: Optional[str],
    slug: str,
    want_hash: str,
    want_tokens: frozenset,
    plan_status: Optional[str] = None,
) -> Optional[ExtantHit]:
    rel = _rel(repo_root, path)
    if _slug_match(stem, slug) or (slug and name == slug):
        return ExtantHit(kind, rel, "exact", "slug", 1.0, plan_status)
    if want_hash and own_hash == want_hash:
        return ExtantHit(kind, rel, "exact", "ask_hash", 1.0, plan_status)
    score = _score(want_tokens, _tokens(stem.replace("-", " ") + " " + text))
    if score >= NEAR_MIN:
        return ExtantHit(kind, rel, "near", "text", round(score, 4), plan_status)
    return None


def _candidates(repo_root: str, slug: str, want_hash: str, want_tokens: frozenset) -> Iterator[ExtantHit]:
    def cls(*args, **kw):
        return _classify(*args, slug=slug, want_hash=want_hash, want_tokens=want_tokens, **kw)

    for e in _scan(os.path.join(repo_root, "docs", "plans"), ".md"):
        keys = _head_keys(_read_head(e.path), strip_fence=True)
        text = keys.get("title", "") + " " + keys.get("deliverable_id", "")
        hit = cls("plan", repo_root, e.path, e.name[:-3], keys.get("name", ""), text, None,
                  plan_status=keys.get("status") or None)
        if hit:
            yield hit

    for e in _scan(os.path.join(repo_root, "state", "sizings"), ".yaml"):
        keys = _head_keys(_read_head(e.path), strip_fence=False)
        intent = keys.get("intent", "")
        hit = cls("sizing", repo_root, e.path, e.name[:-5], keys.get("name", ""),
                  keys.get("name", "") + " " + intent, compute_ask_hash(intent) if intent else None)
        if hit:
            yield hit

    for e in _scan(os.path.join(repo_root, "state", "handoffs"), ".md"):
        keys = _head_keys(_read_head(e.path), strip_fence=True)
        if keys.get("status") not in OPEN_HANDOFF_STATUSES:
            continue
        text = keys.get("title", "") + " " + keys.get("summary", "")
        hit = cls("handoff", repo_root, e.path, e.name[:-3], keys.get("title", ""), text, None)
        if hit:
            yield hit

    warp = os.path.join(repo_root, "state", "warp")
    try:
        with os.scandir(warp) as it:
            runs = sorted((d for d in it if d.is_dir()), key=lambda d: d.name)
    except OSError:
        runs = []
    for run in runs:
        ask_path = os.path.join(run.path, "relayed-ask.md")
        ask_text = _read_head(ask_path)
        if not ask_text:
            continue
        sizing_rel = ""
        run_json = _read_head(os.path.join(run.path, "run.json"))
        m = re.search(r'"sizing_rel"\s*:\s*"([^"]*)"', run_json)
        if m:
            sizing_rel = m.group(1)
        stem = os.path.basename(sizing_rel)[: -len(".yaml")] if sizing_rel.endswith(".yaml") else ""
        hit = cls("ledger", repo_root, ask_path, stem, "", ask_text, compute_ask_hash(ask_text))
        if hit:
            yield hit

    for e in _scan(os.path.join(repo_root, "docs", "wiki"), ".md"):
        head = _read_head(e.path)
        keys = _head_keys(head, strip_fence=True)
        heading = re.search(r"^# (.+)$", head, re.M)
        text = keys.get("title", "") + " " + (heading.group(1) if heading else "")
        hit = cls("wiki", repo_root, e.path, e.name[:-3], "", text, None)
        if hit:
            yield hit


def lookup_extant(repo_root: str, slug: str, ask_hash: str, intent: str) -> ExtantLookup:
    """Exact (slug or ask-hash) and near (token-overlap >= ``NEAR_MIN``) extant work for an ask.

    Pure file reads; never raises on a missing or unreadable corpus directory.
    """
    want_tokens = _tokens(intent)
    exact, near, related = [], [], []
    for hit in _candidates(str(repo_root), slug, ask_hash, want_tokens):
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
