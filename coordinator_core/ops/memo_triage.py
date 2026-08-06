"""
coordinator_core.ops.memo_triage — JSON-RPC "memo.triage" operation.

Purpose: Deterministic pre-filter over ``cross-repo/archive/*.md`` memos that
proposes a small "promote" candidate set — memos worth distilling into durable
doctrine (``docs/decisions/``, ``CLAUDE.md``, lessons/auto-memory) — for a
downstream LLM triage wave to confirm. This op is COMPUTE_ONLY and purely
mechanical: frontmatter scoring + an "already-captured" cross-check + legacy
backfill + observability counters. It does NOT call Haiku/Sonnet and does NOT
decide final promotion — that judgment belongs to example-doctrine-repo's C6 background-Workflow
LLM triage wave, which consumes this op's candidate list as its input, not a
replacement for it.

Two-tier read model:
  1. If a memo's frontmatter already carries ``distill_fate`` (example-doctrine-repo's C2 schema:
     one of ``ephemeral`` | ``commitment`` | ``ratification``), that value is
     read verbatim and used to place the memo directly into the outcome bucket
     (``ratification``/``commitment`` → promote; ``ephemeral`` → not promoted)
     — no scoring is performed for that memo. ``commitment`` is a promote
     signal, not merely a non-disqualifier: the ``/distill`` skill's Guard 7
     (``check_harvest_provenance``, ``coordinator_core/distill/delete_guard.py``)
     blocks deletion of a ``commitment`` candidate unless a ``docs/wiki/**`` or
     ``docs/decisions/**`` file cites it — excluding ``commitment`` from the
     promote set therefore created a permanent deletion deadlock (never
     promoted → never harvested → never cited → Guard 7 blocks forever). This
     is a thin forward-compatible read; conformance-recheck when example-doctrine-repo's
     distill_fate schema lands for real.
  2. Otherwise (the common case today — no memo in the wild carries
     ``distill_fate`` yet), the deterministic pre-filter scores the memo:
       - bare ``decision: accepted`` (no boundary signal) = 0 points.
       - a boundary keyword present in ``decision_note`` = +2 points.
       - an "already-captured" cross-check hit = disqualified outright
         (never promoted, regardless of score).
     Memos scoring > 0 (i.e. score >= 2, since 0 and 2 are the only two
     reachable pre-disqualification values) are candidates for promotion.
     This intentionally targets a single-digit promote set out of the live
     corpus — a wide-net keyword matcher is explicitly the wrong shape (that
     is example-doctrine-repo's cascade's job, not this pre-filter's).

Already-captured cross-check (SPEC ALTITUDE, not "grep the terms"): the
candidate terms are the memo's *distinctive* tokens — DR-id-shaped /
proper-noun-shaped tokens and multi-word slug fragments drawn from ``title``
and ``decision_note`` — explicitly EXCLUDING generic boundary vocabulary
("boundary", "owner", "contract", "ownership", ...) which saturates
``CLAUDE.md`` and would disqualify nearly everything. Matching is whole-token,
case-insensitive, against three corpora: ``docs/decisions/*.md`` (filenames +
content), ``CLAUDE.md`` content, and the auto-memory index (one ``*.md`` file
per lesson under ``~/.claude/projects/<project-slug>/memory/``, honouring
``CLAUDE_HOME`` for test isolation — mirrors ``deliverable_rollup.py``'s
``_claude_home()`` convention). A single whole-token hit against any of the
three disqualifies the memo from promotion — but ONLY on the ``pre_filter``
path. A ``distill_fate``-stamped memo (``ratification``/``commitment``) is the
author's own explicit declaration that durable capture is owed; the
cross-check exists to catch an UNSTAMPED memo's incidental vocabulary
overlap with existing docs, not to override an explicit fate stamp — and
``check_distill_fate``/``check_harvest_provenance`` in ``delete_guard.py``
independently re-verify actual capture at delete time, so gating a
fate-stamped memo here is redundant enforcement, not additional safety. An
earlier version of this cross-check gated bigram terms on a NAMED denylist of
the ordinary two-word coordinator phrases ("load-bearing", "auto-push",
"code-change", ...) observed in one evidence sample — a denylist only ever
closes the specific instances someone happened to see, so the next unlisted
ordinary phrase ("routine-load", "findings-ack", ...) reopened the same
saturation and blanket-disqualified fate-stamped memos wholesale. The
generating fix (see ``_distinctive_tokens``) drops the denylist and instead
never treats an alpha-only word pair as distinctive at all — only a genuine
identifier shape (digit-bearing single word, or an id-prefix+number pair like
"dr-208") qualifies as a cross-check term. Restricting the cross-check to the
``pre_filter`` path additionally bounds its blast radius to the correct scope
(unstamped memos, where the cross-check is the ONLY signal available).

Self-registration: importing this module calls register_op("memo.triage", _handler)
as a side-effect. This module IS imported by coordinator_core/ops/__init__.py and
memo.triage IS wired into authz/classification.py's _OP_KEY_SCOPE (as
"common_dir", matching this op's main_worktree_root(repo_root) resolution) and
into ipc.py's _OP_KEY_SCOPE — confirmed by test_op_registration.py's real-wiring
assertion. (Review: workflow-review 2026-07-12 — this docstring previously claimed
the seams were unwired; commit 5eaf67a wired all three.)

Negative-spec:
  - Does NOT call any LLM (Haiku/Sonnet) — deterministic pre-filter only.
  - Does NOT write any file, issue any git command, or mutate any coordinator
    substrate — read-only, COMPUTE_ONLY (DR-208).
  - Does NOT treat generic boundary vocabulary ("boundary", "owner", "contract")
    as a distinguishing cross-check term — those saturate CLAUDE.md and would
    disqualify nearly the entire corpus.
  - IS wired into ops/__init__.py, authz/classification.py, and ipc.py's
    _OP_KEY_SCOPE (all three seams landed in commit 5eaf67a) — per the lesson
    2026-07-06-compute-only-op-registration-needs-an-op (an op absent from
    _OP_KEY_SCOPE silently degrades to central-scope), this op is NOT in that
    unwired state; it is correctly keyed "common_dir".

Scan-failure handling: the three corpus scanners (_collect_memo_records,
_corpus_tokens_docs_decisions, _corpus_tokens_auto_memory) use `os.scandir`
(not `Path.glob`, which silently swallows `PermissionError` while walking) to
list their respective directories; an unreadable directory sets a `degraded`
flag surfaced verbatim on the ``triage_memos``/handler result — an
empty/partial corpus under a scan failure must not silently bias promotion
decisions toward "nothing here, everything is clean".

Self-contradiction gate: ``promote == 0`` while ``distill_fate_reads > 0`` is
never returned as a clean result — ``triage_memos`` raises
``MemoTriageContradictionError`` instead. A stamped ``distill_fate`` is the
author's own promotion request; a run that read one or more such stamps and
still produced zero promotions is a broken pre-filter/cross-check, not a
genuinely empty corpus, and a well-formed empty answer must not be
indistinguishable from a broken one (2026-08-06 live-corpus incident:
``promote: 0, degraded: false`` returned cleanly over a corpus where 131
fate-stamped memos demanded promotion). Deliberately fail-loud, not a
"warning" field — a ``harvested-disposition rows (0)`` warning fired
correctly on every run for a month while every downstream consumer read the
debt number as truth regardless.

Spec backlink: docs/plans/2026-07-12-distill-ceremony-mechanical-substrate-joint-design.md § C5
"""

from __future__ import annotations
import sys

import logging
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

import yaml

from coordinator_core.ipc import register_op

_LOG = logging.getLogger(__name__)

_CLAUDE_HOME_ENV = "CLAUDE_HOME"


class MemoTriageContradictionError(RuntimeError):
    """Raised by triage_memos() for the implausible-verdict shape: promote==0
    while distill_fate_reads>0 (see module docstring's Self-contradiction
    gate). Fail-loud, not a "degraded"-style flag on the return dict — a
    softened warning has a measured track record of going unconsumed."""

# distill_fate values (example-doctrine-repo's forward C2 schema) that count as a promote signal.
# ``commitment`` MUST stay in this set — see the module docstring's Guard 7
# deadlock rationale (delete_guard.py's check_harvest_provenance blocks a
# commitment candidate's deletion until a docs/wiki or docs/decisions file
# cites it, which never happens if commitment memos are never promoted for
# harvest). Narrowing this back to {"ratification"} reopens that deadlock.
_FATE_PROMOTE = {"ratification", "commitment"}
_FATE_KNOWN = {"ephemeral", "commitment", "ratification"}

# Generic boundary vocabulary EXCLUDED from the cross-check term set — these
# saturate CLAUDE.md / docs/decisions and would disqualify almost every memo
# if treated as distinguishing tokens.
_GENERIC_BOUNDARY_VOCAB = {
    "boundary",
    "boundaries",
    "owner",
    "owns",
    "owned",
    "ownership",
    "contract",
    "contracts",
    "scope",
    "claude-klabauter",
    "doe",
    "coordinator",
    "central",
    "rag",
    "cockpit",
    "plan",
    "plans",
    "the",
    "and",
    "for",
    "with",
    "from",
    "landed",
    "accepted",
}

# decision values eligible to score at all — "declined" never carries a
# genuine ratification worth promoting, regardless of decision_note vocabulary.
_SCORABLE_DECISIONS = {"accepted", "partial"}

# decision_note boundary keywords that add +2 to the pre-filter score — signal
# that a memo settled a genuine cross-repo ownership/boundary question, not
# routine ack/fyi traffic.
_DECISION_NOTE_BOUNDARY_KEYWORDS = {
    "boundary",
    "ownership",
    "owns",
    "owned",
    "scope",
    "authoritative",
    "ratified",
}

_WORD_RE = re.compile(r"[a-z0-9]+")
# DR-id / proper-noun-shaped tokens: "dr208", "c11", "b6143a5"-style — a single
# word-token that mixes letters AND digits (NOT a bare number like "2026" or
# "208", which are common-enough noise — date fragments, line counts — that
# treating them alone as distinctive would produce false cross-check hits).
_DISTINCTIVE_WORD_RE = re.compile(r"^(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z0-9]+$")
# An alpha-only prefix word immediately preceding a bare number is treated as
# an id-prefix (e.g. "dr" before "208", "c" before "11", "ac" before "9") —
# the pair together IS the distinctive DR-id/C-id/AC-number, not two words.
# Bounded to <=3 chars: every real id-prefix observed in this corpus (dr, c,
# ac, b) is short, while the ordinary English words that routinely precede a
# bare number in prose (phase, chunk, step, figure, page, line — "Phase 2",
# "chunk 5", "step 3") are all 4+ chars. This is a shape bound, not a
# denylist of those words: it closes the whole "ordinary word + number"
# class the same way _DISTINCTIVE_WORD_RE closes the digit-bearing-word
# class, rather than enumerating instances (see module docstring). Review:
# coordinator-code-reviewer bd2f004c — the unbounded [a-z]{1,10} reopened the
# saturation bug via ordinary-word+number instead of alpha-only bigrams.
_ID_PREFIX_RE = re.compile(r"^[a-z]{1,3}$")


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation.

    Mirrors coordinator_core/ops/deliverable_rollup.py's _claude_home() convention.
    """
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _words(text: str) -> List[str]:
    """Lowercase word-split (hyphens/underscores treated as separators)."""
    if not text:
        return []
    return _WORD_RE.findall(text.lower().replace("_", "-").replace("-", " "))


def _tokenize(text: str) -> Set[str]:
    """Whole-token corpus set: every individual word PLUS every adjacent
    word-pair (bigram, hyphen-joined) — e.g. "owner-axis plural-typed" yields
    {"owner", "axis", "owner-axis", "plural", "typed", "plural-typed", ...}.
    Bigrams let a distinctive multi-word slug fragment (e.g. "owner-axis")
    match as a unit against corpus prose without single generic words like
    "owner" alone counting as a corpus hit.
    """
    words = _words(text)
    tokens: Set[str] = set(words)
    for i in range(len(words) - 1):
        tokens.add(f"{words[i]}-{words[i + 1]}")
    return tokens


def _distinctive_tokens(*, title: str, decision_note: str) -> Set[str]:
    """Distinctive cross-check terms: genuine-identifier-shaped tokens from
    title + decision_note. The memo's own filename slug is deliberately NOT a
    term source — matching a memo's slug against itself (or against another
    memo's distinct slug) is meaningless self/cross noise, not a
    captured-elsewhere signal.

    "Distinctive" = a genuine identifier, never an ordinary word or word
    pair: a single word containing a digit (dr208 → "dr208"; c11; b6143a5),
    or a SHORT (<=3 char) alpha id-prefix immediately followed by a bare
    number (dr-208, c-11, ac-9) — see ``_ID_PREFIX_RE``. An ALPHA-ONLY word
    pair never qualifies, however lexically distinctive it looks
    ("drift-anchor", "owner-axis") — there is no shape-based test that
    reliably separates a genuine proper-noun slug from an ordinary two-word
    coordinator phrase ("load-bearing", "auto-push"), and a denylist
    enumerating the ordinary phrases someone happened to observe only ever
    closes THOSE instances, not the class (see module docstring). Requiring
    a digit closes the whole class at once. The id-prefix+number bigram
    shape is likewise length-bounded rather than denylisted: an ordinary
    English word immediately followed by a bare number ("phase 2", "chunk
    5", "step 3", "figure 6", "page 9", "line 40") is common prose in a repo
    organised in phases/chunks/steps, and every one of those words is 4+
    chars while every real id-prefix observed in this corpus (dr, c, ac, b)
    is <=3 chars — a shape bound, not an enumeration of the ordinary words
    to exclude.
    """
    title_words = _words(title)
    note_words = _words(decision_note)

    candidates: Set[str] = set()

    # Digit-bearing single words (DR-ids, commit-shas, C-numbers).
    for w in title_words + note_words:
        if _DISTINCTIVE_WORD_RE.match(w) and len(w) > 2:
            candidates.add(w)

    # id-prefix + bare-number pair (e.g. "dr"+"208", "c"+"11", "ac"+"9") IS
    # the distinctive DR-id/C-id/AC-number as a unit — the ONLY bigram shape
    # that qualifies. An alpha-only word pair (both halves non-digit) is
    # never added, however lexically distinctive it looks — see this
    # function's docstring for why that class is closed entirely rather than
    # denylisted instance-by-instance.
    for words in (title_words, note_words):
        for i in range(len(words) - 1):
            a, b = words[i], words[i + 1]
            if a in _GENERIC_BOUNDARY_VOCAB or b in _GENERIC_BOUNDARY_VOCAB:
                continue
            if b.isdigit() and _ID_PREFIX_RE.match(a):
                candidates.add(f"{a}-{b}")

    return candidates


def _corpus_tokens_docs_decisions(worktree_root: Path) -> Tuple[Set[str], bool]:
    """Whole-token corpus from docs/decisions/*.md — filenames + content.

    Returns `(tokens, degraded)`. `degraded` is True ONLY when `docs/decisions/`
    itself could not be listed (e.g. permission-denied) — uses `os.scandir()`,
    NOT `Path.glob("*.md")`, which silently swallows `PermissionError` while
    walking (empirically re-verified: a chmod-000 dir yields an empty iterator
    from `glob()`, no exception). An empty-but-degraded corpus must never be
    read as "genuinely no docs/decisions/ content" — a degraded cross-check
    corpus can wrongly fail to disqualify a memo that IS already captured.
    """
    decisions_dir = worktree_root / "docs" / "decisions"
    tokens: Set[str] = set()
    if not decisions_dir.is_dir():
        return tokens, False
    try:
        entries = sorted(os.scandir(decisions_dir), key=lambda e: e.name)
    except OSError as exc:
        _LOG.warning(
            "memo.triage: cannot scan %s — %s; docs/decisions/ cross-check corpus "
            "is degraded (NOT the same as \"docs/decisions/ has no content\")",
            decisions_dir,
            exc,
        )
        return tokens, True
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        fpath = Path(entry.path)
        tokens |= _tokenize(fpath.stem)
        try:
            tokens |= _tokenize(fpath.read_text(encoding="utf-8"))
        except OSError:
            print(f"skip: _corpus_tokens_docs_decisions: tokens |= _tokenize(fpath.read_text(encoding=\"utf-8\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return tokens, False


def _corpus_tokens_claude_md(worktree_root: Path) -> Set[str]:
    """Whole-token corpus from the repo's CLAUDE.md content."""
    fpath = worktree_root / "CLAUDE.md"
    if not fpath.is_file():
        return set()
    try:
        return _tokenize(fpath.read_text(encoding="utf-8"))
    except OSError:
        return set()


def _corpus_tokens_auto_memory(project_slug: Optional[str]) -> Tuple[Set[str], bool]:
    """Whole-token corpus from the auto-memory lesson index.

    One *.md file per lesson under
    ~/.claude/projects/<project_slug>/memory/*.md (honours CLAUDE_HOME).
    Graceful-absent: returns `(set(), False)` when the directory or
    project_slug is unresolvable — the auto-memory corpus is best-effort, not
    load-bearing.

    Returns `(tokens, degraded)`. `degraded` is True ONLY when the memory
    directory exists but could not be listed — uses `os.scandir()`, NOT
    `Path.glob("*.md")`, per the same silent-PermissionError-swallowing
    rationale as `_corpus_tokens_docs_decisions`.
    """
    if not project_slug:
        return set(), False
    memory_dir = Path(_claude_home()) / "projects" / project_slug / "memory"
    tokens: Set[str] = set()
    if not memory_dir.is_dir():
        return tokens, False
    try:
        entries = sorted(os.scandir(memory_dir), key=lambda e: e.name)
    except OSError as exc:
        _LOG.warning(
            "memo.triage: cannot scan %s — %s; auto-memory cross-check corpus "
            "is degraded (NOT the same as \"auto-memory has no content\")",
            memory_dir,
            exc,
        )
        return tokens, True
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        fpath = Path(entry.path)
        tokens |= _tokenize(fpath.stem)
        try:
            tokens |= _tokenize(fpath.read_text(encoding="utf-8"))
        except OSError:
            print(f"skip: _corpus_tokens_auto_memory: tokens |= _tokenize(fpath.read_text(encoding=\"utf-8\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return tokens, False


def _already_captured(terms: Set[str], corpus: Set[str]) -> bool:
    """True if any distinctive term whole-token-matches the capture corpus."""
    return bool(terms & corpus)


def _parse_frontmatter(raw: str) -> Optional[dict]:
    """Extract + parse the YAML frontmatter block of a memo file.

    Returns None (quarantine) on missing/malformed frontmatter — mirrors the
    quarantine convention in plan_match.py / goals_match.py.
    """
    if not raw.startswith("---\n"):
        return None
    parts = raw.split("---\n", 2)
    if len(parts) < 2:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:  # noqa: BLE001 — quarantine parity with sibling ops
        return None
    return fm if isinstance(fm, dict) else None


def _score_memo(fm: dict) -> dict:
    """Compute the deterministic pre-filter outcome for one memo.

    Returns a dict describing the classification path (fate-read vs
    pre-filter-scored) and the numeric score (pre-filter path only). Does NOT
    apply the already-captured cross-check — the caller (which has corpus
    access) combines this base score with the cross-check disqualification.
    """
    fate = fm.get("distill_fate")
    if isinstance(fate, str) and fate in _FATE_KNOWN:
        return {
            "path": "distill_fate",
            "fate": fate,
            "promote": fate in _FATE_PROMOTE,
            "score": None,
        }

    decision = fm.get("decision")
    decision_note = fm.get("decision_note") or ""
    if not isinstance(decision_note, str):
        decision_note = str(decision_note)

    score = 0
    # Only a SETTLED decision (accepted/partial) is eligible to score — a
    # "declined" memo never carries a genuine ratification worth distilling,
    # regardless of what incidental vocabulary its decision_note contains.
    if decision in _SCORABLE_DECISIONS:
        # bare decision:accepted (or :partial) is 0 points baseline; a
        # boundary keyword in decision_note is the only way to earn points.
        note_tokens = _tokenize(decision_note)
        if note_tokens & _DECISION_NOTE_BOUNDARY_KEYWORDS:
            score += 2

    return {
        "path": "pre_filter",
        "fate": None,
        "promote": score > 0,
        "score": score,
    }


def _collect_memo_records(archive_dir: Path) -> Tuple[List[dict], bool]:
    """Enumerate cross-repo/archive/*.md as parsed memo records.

    Each record: {memo_id, slug, path, title, decision, decision_note, fm}.
    Files with missing/malformed frontmatter are quarantined (skipped, warned).

    Returns `(records, degraded)`. `degraded` is True ONLY when `archive_dir`
    itself could not be listed — uses `os.scandir()`, NOT `Path.glob("*.md")`,
    which silently swallows `PermissionError` while walking (empirically
    re-verified: a chmod-000 dir yields an empty iterator from `glob()`, no
    exception). An empty-but-degraded record list must never be read as
    "genuinely no memos to triage" — that would silently bias promotion
    decisions toward an artificially-clean corpus.
    """
    records: List[dict] = []
    if not archive_dir.is_dir():
        return records, False

    try:
        entries = sorted(os.scandir(archive_dir), key=lambda e: e.name)
    except OSError as exc:
        _LOG.warning(
            "memo.triage: cannot scan %s — %s; memo corpus is degraded "
            "(NOT the same as \"no memos to triage\")",
            archive_dir,
            exc,
        )
        return records, True

    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        fpath = Path(entry.path)
        try:
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError as exc:
            _LOG.warning("memo.triage: skipping %s — read error: %s", fpath.name, exc)
            continue

        fm = _parse_frontmatter(raw)
        if fm is None:
            _LOG.warning(
                "memo.triage: skipping %s — missing/malformed frontmatter", fpath.name
            )
            continue

        records.append(
            {
                "memo_id": fpath.stem,
                "slug": fpath.stem,
                "path": str(fpath),
                "title": fm.get("title") if isinstance(fm.get("title"), str) else "",
                "decision": fm.get("decision"),
                "decision_note": fm.get("decision_note")
                if isinstance(fm.get("decision_note"), str)
                else "",
                "fm": fm,
            }
        )

    return records, False


def triage_memos(
    records: Iterable[dict],
    *,
    capture_corpus: Set[str],
    degraded: bool = False,
) -> dict:
    """Pure classification core — given parsed memo records and a pre-built
    already-captured corpus (whole tokens from docs/decisions + CLAUDE.md +
    auto-memory), returns the triage outcome.

    Args:
        degraded: True when the caller's I/O layer could not fully scan the
            memo corpus and/or a cross-check corpus (docs/decisions, CLAUDE.md,
            auto-memory) — e.g. a permission-denied directory. Surfaced
            verbatim as the "degraded" key so callers do NOT mistake an
            empty/incomplete scan for a genuinely clean triage result; a
            silently-truncated corpus/cross-check must not silently bias
            promotion decisions.

    Returns:
        {
            "promote": [memo_id, ...],       # sorted, deterministic
            "disqualified": [memo_id, ...],  # already-captured hits
            "candidates": [
                {"memo_id": str, "score": int|None, "path": "distill_fate"|"pre_filter",
                 "fate": str|None, "disqualified": bool, "terms": [str, ...]},
                ...
            ],
            "counts": {"total": int, "promote": int, "disqualified": int,
                       "distill_fate_reads": int, "pre_filter_scored": int},
            "degraded": bool,  # True iff a scan surface was unreadable (see Args)
        }
    """
    candidates: List[dict] = []
    promote: List[str] = []
    disqualified: List[str] = []
    distill_fate_reads = 0
    pre_filter_scored = 0

    for rec in records:
        outcome = _score_memo(rec["fm"])
        if outcome["path"] == "distill_fate":
            distill_fate_reads += 1
        else:
            pre_filter_scored += 1

        terms = _distinctive_tokens(
            title=rec["title"], decision_note=rec["decision_note"]
        )
        # The already-captured cross-check only gates the unstamped
        # pre_filter path. A distill_fate-stamped memo (ratification/
        # commitment) is an explicit author declaration that durable capture
        # is owed; delete_guard.py's check_distill_fate/check_harvest_provenance
        # independently re-verify actual capture at delete time, so this
        # cross-check's generic-vocabulary-prone terms (see module docstring)
        # gating a fate-stamped memo is redundant at best and a blanket
        # disqualifier at worst — it must not override an explicit stamp.
        if outcome["path"] == "distill_fate":
            is_disqualified = False
        else:
            is_disqualified = _already_captured(terms, capture_corpus)

        promoted = outcome["promote"] and not is_disqualified

        if is_disqualified:
            disqualified.append(rec["memo_id"])
        if promoted:
            promote.append(rec["memo_id"])

        candidates.append(
            {
                "memo_id": rec["memo_id"],
                "score": outcome["score"],
                "path": outcome["path"],
                "fate": outcome["fate"],
                "disqualified": is_disqualified,
                "terms": sorted(terms),
                "promoted": promoted,
            }
        )

    if len(promote) == 0 and distill_fate_reads > 0:
        raise MemoTriageContradictionError(
            f"memo.triage: promote=0 but distill_fate_reads={distill_fate_reads} — "
            "at least one memo's explicit distill_fate stamp is the author's own "
            "promotion request; a zero-promote verdict over a corpus that read "
            "one is a contradiction, not a genuinely empty result. Refusing to "
            "return a clean-looking result — see module docstring's "
            "Self-contradiction gate."
        )

    return {
        "promote": sorted(promote),
        "disqualified": sorted(disqualified),
        "candidates": candidates,
        "counts": {
            "total": len(candidates),
            "promote": len(promote),
            "disqualified": len(disqualified),
            "distill_fate_reads": distill_fate_reads,
            "pre_filter_scored": pre_filter_scored,
        },
        "degraded": degraded,
    }


@register_op("memo.triage")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "memo.triage" handler.

    Params:
        archive_dir (str, optional): override for cross-repo/archive/ — mostly
            for test isolation. Defaults to ``<worktree_root>/cross-repo/archive``.
        project_slug (str, optional): the auto-memory project-slug directory
            name under ``~/.claude/projects/`` — e.g.
            ``-Users-example-operator-X-claude-klabauter``. Absent → auto-memory corpus is
            empty (graceful-absent; cross-check still runs against
            docs/decisions + CLAUDE.md).

    Returns: the ``triage_memos`` outcome dict (see docstring above), or
        ``{"promote": [], "disqualified": [], "candidates": [], "counts": {...},
        "degraded": False}`` with all-zero counts when repo_root is unresolved.
        ``degraded`` is True iff any of the three scan surfaces (memo archive,
        docs/decisions, auto-memory) could not be fully listed (permission
        denied, etc.) — an empty/partial corpus under such a failure must not
        be read as "genuinely nothing to triage".

    Worktree resolution mirrors plan_match.py / goals_match.py:
      - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
      - None → return the empty/zero outcome with a logged warning.
    """
    from coordinator_core.ops.fleet._common import main_worktree_root

    if repo_root is None:
        _LOG.warning("memo.triage: no repo_root resolved — returning empty outcome")
        return triage_memos([], capture_corpus=set())

    worktree_root = main_worktree_root(repo_root)

    archive_override = params.get("archive_dir")
    archive_dir = (
        Path(archive_override) if isinstance(archive_override, str) and archive_override
        else worktree_root / "cross-repo" / "archive"
    )

    project_slug = params.get("project_slug")
    project_slug = project_slug if isinstance(project_slug, str) and project_slug else None

    docs_decisions_tokens, docs_decisions_degraded = _corpus_tokens_docs_decisions(worktree_root)
    auto_memory_tokens, auto_memory_degraded = _corpus_tokens_auto_memory(project_slug)
    capture_corpus = (
        docs_decisions_tokens
        | _corpus_tokens_claude_md(worktree_root)
        | auto_memory_tokens
    )

    records, records_degraded = _collect_memo_records(archive_dir)
    degraded = docs_decisions_degraded or auto_memory_degraded or records_degraded
    return triage_memos(records, capture_corpus=capture_corpus, degraded=degraded)
