"""
coordinator_core.distill.guide_index — deterministic per-batch relevant-guide lists.

Purpose: a distill scan agent decides ALREADY_CAPTURED against the wiki. Handed the whole
slug table it re-reads guides to decide; handed the few guides that share terms with its own
batch it checks those and nothing else. This module indexes the guides under wiki_dirs by
heading and body terms (lexical, no model, no network) and returns, per batch, the top
RELEVANT_GUIDES_K guides by IDF-weighted term overlap with the batch's files, each with a
short heading outline, stopping early once summed guide bytes would pass GUIDE_BYTE_BUDGET.

Invariant: a pure function of file contents and the slug table; ties break on slug, so two
runs over the same tree emit byte-identical lists. A missing or unreadable file degrades to
fewer terms, never an error: the list is advisory input to a scan agent and the full slug
table stays declared beside it.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from coordinator_core.distill.manifest_schema import GUIDE_BYTE_BUDGET

__all__ = [
    "RAG_INDEX_KEY",
    "RELEVANT_GUIDES_K",
    "RagIndexUnavailable",
    "build_index",
    "rag_graph_db",
    "rag_known_paths",
    "rag_relevant_guides",
    "ranked_guides_for_batches",
    "relevant_guides_for_batches",
]

#: How many guides a scan agent is told to check for ALREADY_CAPTURED, per batch.
RELEVANT_GUIDES_K = 8

#: Read at most this many bytes of one guide when extracting terms. Batch files get a smaller
#: head (title, frontmatter and opening prose name the topic): ~900 files cost ~0.2s of
#: process time at 6 KB against ~0.7s at 24 KB, and the op's bar is 500ms.
_READ_CAP_BYTES = 24_000
_BATCH_READ_CAP_BYTES = 6_000

#: Outline bounds: a guide's entry stays under ~450 compact-JSON bytes so a batch file with
#: K guides stays far below the relay cap.
_OUTLINE_MAX_HEADINGS = 10
_OUTLINE_TITLE_MAX = 60
_ENTRY_MAX_BYTES = 450

#: Weight of a term that appears in a guide's slug or headings, against body-only (1).
_HEADING_WEIGHT = 3

#: A term present in more than this share of guides names no guide in particular.
_COMMON_TERM_SHARE = 0.5

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{3,}")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_STOPWORDS = frozenset(
    "this that with from have will when then than into your they them their there were been "
    "also only some such what which while about after before should would could does done "
    "each more most other over under same these those must never always file files line lines "
    "name text true false none null note notes see use used uses using".split()
)


def _read_head(path: Path, cap: int = _READ_CAP_BYTES) -> str:
    try:
        with path.open("rb") as fh:
            return fh.read(cap).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _split_markdown(text: str) -> tuple[list[tuple[int, str]], str]:
    """(headings, body-without-frontmatter-or-fences). Headings inside fences are not headings."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    headings: list[tuple[int, str]] = []
    body: list[str] = []
    in_fence = False
    for line in lines[start:]:
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            headings.append((len(m.group(1)), m.group(2)))
        body.append(line)
    return headings, "\n".join(body)


def _clip_title(title: str) -> str:
    """Whole words only; a clipped title ends in an explicit ellipsis. The outline travels inside
    a digest-checked slice, so a mid-word cut gets "repaired" by the reader and breaks the digest."""
    if len(title) <= _OUTLINE_TITLE_MAX:
        return title
    head = title[: _OUTLINE_TITLE_MAX + 1]
    cut = head.rfind(" ")
    words = title[:cut].rstrip() if cut > 0 else title.split(" ", 1)[0]
    return words + "…"


def _outline(headings: list[tuple[int, str]], slug: str, path: str) -> list[str]:
    out: list[str] = []
    for level, title in headings[:_OUTLINE_MAX_HEADINGS]:
        out.append(f"{'#' * level} {_clip_title(title)}")
    while out and len(json.dumps({"slug": slug, "path": path, "outline": out}, separators=(",", ":")).encode()) > _ENTRY_MAX_BYTES:
        out.pop()
    return out


def _within_byte_budget(
    root: Path, ranked: list[int], guides: list[dict[str, Any]], k: int, budget: int
) -> list[int]:
    """The best-first prefix of `ranked`, at most `k` long, whose summed on-disk bytes stay within
    `budget`. Stops at the first guide that would overflow (rank order is never skipped past);
    rank 1 is always kept. An unstattable guide counts 0 bytes."""
    kept: list[int] = []
    total = 0
    for gi in ranked[:k]:
        try:
            size = (root / guides[gi]["path"]).stat().st_size
        except OSError:
            size = 0
        if kept and total + size > budget:
            break
        kept.append(gi)
        total += size
    return kept


def build_index(repo_root: str | Path, wiki_slugs: dict[str, str]) -> dict[str, Any]:
    """Index every guide in `wiki_slugs` (slug -> repo-relative path). Returns an opaque
    dict holding guide entries, an inverted term index and per-term IDF; pass it to
    relevant_guides_for_batches."""
    root = Path(repo_root)
    guides: list[dict[str, Any]] = []
    inverted: dict[str, list[tuple[int, int]]] = {}
    for slug in sorted(wiki_slugs):
        path = wiki_slugs[slug]
        headings, body = _split_markdown(_read_head(root / path))
        heading_terms = _tokens(slug.replace("-", " ") + " " + " ".join(t for _, t in headings))
        body_terms = _tokens(body) - heading_terms
        gi = len(guides)
        guides.append({"slug": slug, "path": path, "outline": _outline(headings, slug, path)})
        for t in heading_terms:
            inverted.setdefault(t, []).append((gi, _HEADING_WEIGHT))
        for t in body_terms:
            inverted.setdefault(t, []).append((gi, 1))
    n = max(len(guides), 1)
    idf: dict[str, float] = {}
    for term in list(inverted):
        df = len(inverted[term])
        if n >= 4 and df > _COMMON_TERM_SHARE * n:
            del inverted[term]
            continue
        idf[term] = math.log(1 + n / df)
    return {"guides": guides, "inverted": inverted, "idf": idf}


def _batch_terms(root: Path, files: Iterable[str]) -> list[str]:
    terms: set[str] = set()
    for rel in files:
        terms |= _tokens(rel.replace("/", " ").replace("-", " ").replace("_", " "))
        terms |= _tokens(_read_head(root / rel, _BATCH_READ_CAP_BYTES))
    return sorted(terms)


def relevant_guides_for_batches(
    repo_root: str | Path,
    batches: list[list[str]],
    wiki_slugs: dict[str, str],
    *,
    k: int = RELEVANT_GUIDES_K,
    index: dict[str, Any] | None = None,
    byte_budget: int = GUIDE_BYTE_BUDGET,
) -> list[list[dict[str, Any]]]:
    """For each batch (a list of repo-relative file paths) the top `k` guides by IDF-weighted
    term overlap whose summed on-disk bytes fit `byte_budget` (rank 1 always kept), as
    `{"slug", "path", "outline"}` dicts, best first. A batch sharing no term with any guide
    gets []."""
    root = Path(repo_root)
    idx = index if index is not None else build_index(root, wiki_slugs)
    guides, inverted, idf = idx["guides"], idx["inverted"], idx["idf"]
    result: list[list[dict[str, Any]]] = []
    for files in batches:
        scores: dict[int, float] = {}
        for term in _batch_terms(root, files):
            postings = inverted.get(term)
            if not postings:
                continue
            weight = idf[term]
            for gi, w in postings:
                scores[gi] = scores.get(gi, 0.0) + weight * w
        ranked = sorted(scores, key=lambda gi: (-scores[gi], guides[gi]["slug"]))
        ranked = _within_byte_budget(root, ranked, guides, k, byte_budget)
        result.append([dict(guides[gi]) for gi in ranked])
    return result


# --- example-retrieval-repo ranking (optional, read-only) ----------------------------------------------
# example-retrieval-repo is downstream of claude-klabauter: its absence is the normal case. Everything below reads
# its graph.db through sqlite in read-only mode and never writes the store; any refusal or
# sqlite error degrades to the lexical ranking above.

#: Settings-home-relative key of the repo's index, `<slug>` being the repo directory name with
#: '-' -> '_'. A refusal names this key.
RAG_INDEX_KEY = "project-rag/indexes/<repo-name>/root/graph.db"

#: The index must cover at least this share of the guides to rank from; a stale or foreign
#: index (other tree, other branch) would rank from headings that no longer exist.
_RAG_MIN_COVERAGE = 0.5

_SQL_CHUNK = 400


class RagIndexUnavailable(RuntimeError):
    """The example-retrieval-repo index cannot serve this repo. `key` names what is missing."""

    def __init__(self, key: str, detail: str = "") -> None:
        self.key = key
        super().__init__(f"example-retrieval-repo index unavailable: {key}" + (f" ({detail})" if detail else ""))


def rag_graph_db(repo_root: str | Path, settings_home_dir: str | Path | None = None) -> Path:
    """The repo's example-retrieval-repo graph.db under the settings home; raises RagIndexUnavailable
    naming RAG_INDEX_KEY when the file is absent."""
    if settings_home_dir is None:
        from coordinator_core._settings_home import settings_home

        settings_home_dir = settings_home()
    slug = Path(repo_root).resolve().name.replace("-", "_")
    path = Path(settings_home_dir) / "project-rag" / "indexes" / slug / "root" / "graph.db"
    if not path.is_file():
        raise RagIndexUnavailable(RAG_INDEX_KEY.replace("<repo-name>", slug), str(path))
    return path


def _open_ro(db: Path):
    import sqlite3

    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=0.2)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rag_headings(db: Path, paths: list[str]) -> dict[str, list[tuple[int, int, str]]]:
    """path -> [(line, level, heading)] from md_symbols, in file order. An index without the
    table refuses."""
    import sqlite3

    out: dict[str, list[tuple[int, int, str]]] = {}
    try:
        conn = _open_ro(db)
        try:
            for i in range(0, len(paths), _SQL_CHUNK):
                chunk = paths[i : i + _SQL_CHUNK]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT file, line, heading_level, name FROM md_symbols "
                    f"WHERE kind='heading' AND file IN ({marks}) ORDER BY file, line",
                    chunk,
                )
                for file, line, level, name in rows:
                    out.setdefault(file, []).append((line or 0, level or 1, name))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RagIndexUnavailable("md_symbols", f"{type(exc).__name__}: {exc}") from exc
    return out


def rag_relevant_guides(
    repo_root: str | Path,
    batches: list[list[str]],
    wiki_slugs: dict[str, str],
    db: Path,
    *,
    k: int = RELEVANT_GUIDES_K,
    byte_budget: int = GUIDE_BYTE_BUDGET,
) -> list[list[dict[str, Any]]]:
    """Top `k` guides per batch (within `byte_budget` summed on-disk bytes) ranked by IDF-weighted overlap between the batch's terms and
    the guides' slug and heading terms as the example-retrieval-repo index holds them (no guide is read).
    Same entry shape and tie-break as relevant_guides_for_batches. Raises RagIndexUnavailable
    when the index does not cover the guides."""
    root = Path(repo_root)
    slugs = sorted(wiki_slugs)
    headings = _rag_headings(db, [wiki_slugs[s] for s in slugs])
    covered = sum(1 for s in slugs if wiki_slugs[s] in headings)
    if not slugs or covered < _RAG_MIN_COVERAGE * len(slugs):
        raise RagIndexUnavailable("md_symbols", f"index covers {covered}/{len(slugs)} guides")
    guides: list[dict[str, Any]] = []
    inverted: dict[str, list[int]] = {}
    for slug in slugs:
        path = wiki_slugs[slug]
        hs = [(level, name) for _, level, name in headings.get(path, [])]
        gi = len(guides)
        guides.append({"slug": slug, "path": path, "outline": _outline(hs, slug, path)})
        for t in _tokens(slug.replace("-", " ") + " " + " ".join(n for _, n in hs)):
            inverted.setdefault(t, []).append(gi)
    n = len(guides)
    idf: dict[str, float] = {}
    for term in list(inverted):
        df = len(inverted[term])
        if n >= 4 and df > _COMMON_TERM_SHARE * n:
            del inverted[term]
            continue
        idf[term] = math.log(1 + n / df)
    result: list[list[dict[str, Any]]] = []
    for files in batches:
        scores: dict[int, float] = {}
        for term in _batch_terms(root, files):
            for gi in inverted.get(term, ()):
                scores[gi] = scores.get(gi, 0.0) + idf[term]
        ranked = sorted(scores, key=lambda gi: (-scores[gi], guides[gi]["slug"]))
        ranked = _within_byte_budget(root, ranked, guides, k, byte_budget)
        result.append([dict(guides[gi]) for gi in ranked])
    return result


def ranked_guides_for_batches(
    repo_root: str | Path,
    batches: list[list[str]],
    wiki_slugs: dict[str, str],
    *,
    k: int = RELEVANT_GUIDES_K,
    settings_home_dir: str | Path | None = None,
    index: dict[str, Any] | None = None,
    byte_budget: int = GUIDE_BYTE_BUDGET,
) -> tuple[str, list[list[dict[str, Any]]]]:
    """(source, lists): the example-retrieval-repo ranking when the repo's index serves it, source "rag";
    otherwise the lexical ranking, source "lexical". Never raises on a missing or unusable
    index."""
    try:
        db = rag_graph_db(repo_root, settings_home_dir)
        return "rag", rag_relevant_guides(repo_root, batches, wiki_slugs, db, k=k, byte_budget=byte_budget)
    except RagIndexUnavailable:
        return "lexical", relevant_guides_for_batches(
            repo_root, batches, wiki_slugs, k=k, index=index, byte_budget=byte_budget
        )


def rag_known_paths(
    repo_root: str | Path,
    paths: Iterable[str],
    settings_home_dir: str | Path | None = None,
) -> set[str]:
    """The subset of repo-relative `paths` the example-retrieval-repo index holds (markdown or data
    files), as ALREADY_CAPTURED existence evidence. Raises RagIndexUnavailable when the index
    cannot answer; callers fall back to the filesystem."""
    import sqlite3

    db = rag_graph_db(repo_root, settings_home_dir)
    wanted = sorted(set(paths))
    found: set[str] = set()
    try:
        conn = _open_ro(db)
        try:
            for i in range(0, len(wanted), _SQL_CHUNK):
                chunk = wanted[i : i + _SQL_CHUNK]
                marks = ",".join("?" * len(chunk))
                for table in ("md_symbols", "data_symbols"):
                    found.update(
                        r[0]
                        for r in conn.execute(f"SELECT DISTINCT file FROM {table} WHERE file IN ({marks})", chunk)
                    )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RagIndexUnavailable("md_symbols", f"{type(exc).__name__}: {exc}") from exc
    return found
