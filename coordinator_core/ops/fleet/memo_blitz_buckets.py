"""
coordinator_core.ops.fleet.memo_blitz_buckets — memo.blitz_buckets COMPUTE_ONLY UDS op.

Purpose: return the MECHANICAL half of an inbox blitz as data, so the ceremony
that triages a grown `cross-repo/inbox/` spends its model budget on judgment
instead of on grep-grade bucketing. Four outputs, one read of the inbox:

  - `bucket`      — one entry per open memo, tagged with the bucket a
                    dispatch should carry it in (`fyi` / `dominant` / `rest`),
                    plus its `kind`, `from`, `space`, and age in days.
  - `bucket_summary` — the counts-by-kind, counts-by-sender, and the resolved
                    dominant correspondent. The sender distribution is the
                    load-bearing structural fact, not a nicety: example-retrieval-repo's
                    2026-07-28 pass found ONE correspondent owning over half
                    the inbox, and those memos were not N independent items but
                    a handful of running threads.
  - `supersession_candidate` — CANDIDATE pairs only (see negative-spec).
                    Three independent bases now feed it — `self-declared`
                    (in-body prose), `declared` (`supersedes:` frontmatter),
                    and `same-sender-same-locus`, the last of which carries an
                    explicit `advisory` marker rather than reading as
                    comparably strong to the two declaration bases.
  - `trigger`     — open count, oldest-open age, and whether either escalation
                    leg is tripped, for a ceremony that wants to decide
                    inventory-vs-blitz without re-deriving the counts.

Provenance: cross-repo/inbox/2026-07-28-example-retrieval-repo-em-inbox-blitz-proven-
pattern.md (example-retrieval-repo-em's pattern report + proposal, adopted 2026-07-28 with
the housing changed to a /workday-start escalation rather than a new skill).
The mechanical/judgment split this op implements is theirs verbatim — bucketing,
candidate detection, and the age/count triggers are mechanical; re-routing a
mislabeled `fyi`, confirming a supersession actually RESOLVES, fix-vs-implement-
vs-plan-weight, and naming a problem space when no `space:` field exists all
stay with the EM.

Negative-spec:
  - Does NOT write, move, or flip any memo's lifecycle state. Pure read; always
    returns the `dry_run:true` envelope, and rejects `dry_run:false` rather than
    silently ignoring it (same posture as memo.list).
  - Does NOT CONFIRM a supersession — it emits candidates. Confirming that a
    later memo RESOLVES an earlier one, rather than merely touching the same
    topic, is judgment and stays with the EM. Loose matching would silently drop
    live asks; example-retrieval-repo was strict about this deliberately and said so.
  - Does NOT treat `space:` as authoritative. It is a sender hint; the receiver
    may override it, and a memo without one is not an error — it falls back to
    an inferred space key (see `_space_key`) that is explicitly marked
    `space_declared: false` so a consumer can tell a sender's own grouping from
    this op's guess.
  - Does NOT model the inbox as a flat list of independent items — the
    `dominant` bucket exists precisely because one correspondent owning half the
    inbox is the normal shape, not an anomaly.
  - Does NOT grow an index or cache. Every call re-reads the inbox directory
    fresh (the store-less-ness invariant memo.list/memo.send hold).
  - Does NOT read `cross-repo/archive/`. Buckets describe work still OPEN; an
    archived memo is already dispositioned. Supersession candidates are
    therefore also open-vs-open only.

Trust boundary (Review: code-reviewer F7): the `inbox_dir` override takes any
caller-supplied absolute path with no containment check against a repo root.
This is deliberate, not an oversight — it is the documented seam that lets
fixture-driven tests and a sibling-audit ceremony point this op at a directory
that is not the calling repo's own worktree, and constraining it to a subtree
would break that seam. It is safe only because this op is COMPUTE_ONLY,
engine-local, and reachable exclusively over the local engine's UDS surface —
not attacker-reachable over an untrusted network boundary. The override is
CALLER-TRUSTED BY DESIGN: it returns frontmatter-derived metadata (sender,
title, space, path, cited-locus basenames) for whatever directory it is
pointed at, so a caller must not point it at a directory whose metadata the
caller is not already entitled to read.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.schema_validate import parse_yaml

_LOG = logging.getLogger(__name__)

_MODE = "blitz_buckets"

_INBOX_DIRNAME = ("cross-repo", "inbox")

# Statuses that mean "still needs a disposition". `actioned`/`closed`/
# `superseded`/`action_taken`/`reviewed` are all terminal-or-past-triage and are
# excluded — a blitz grinds the open pile, not the paper trail.
_OPEN_STATUSES = frozenset({"open", "in_progress"})

# Default escalation thresholds. Both legs matter and the AGE leg is the
# load-bearing one: example-retrieval-repo's failure was 16 days of accretion where no
# single day ever looked bad enough to act on, not volume on any one day.
# PM-set starting values (2026-07-28), deliberately tunable per call.
_DEFAULT_OPEN_THRESHOLD = 10
_DEFAULT_AGE_DAYS_THRESHOLD = 7

# A sender owning at least this share of the open pile is the "dominant
# correspondent" and earns its own bucket. Half is the threshold example-retrieval-repo's
# run actually exhibited (16 of 30); expressed as a fraction so a smaller inbox
# with the same shape still resolves one.
_DOMINANT_SHARE = 0.4

# Minimum open memos before a dominant correspondent is resolved at all — below
# this, "40% of the pile" is one or two memos and the split buys nothing.
_DOMINANT_MIN_OPEN = 5

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")

# Locus citation — a repo-relative-looking path or a dotted symbol path in the
# memo body/title. Two memos from the same sender citing the same locus are a
# supersession CANDIDATE. Deliberately coarse: this is a recall-oriented filter
# whose false positives cost the EM one read, while a miss silently retires
# nothing (the pair simply isn't offered).
_LOCUS_RE = re.compile(r"\b[\w./-]+\.(?:py|js|ts|md|json|yaml|yml|toml|sh)\b")

# Review: code-reviewer F6 — a URL path segment (e.g.
# https://example.com/v2/config.yaml) matches _LOCUS_RE and, once reduced to
# a basename, can collide with an unrelated repo-relative citation of the
# same trailing segment (config.yaml). Stripping URLs out of the text before
# matching removes that false-positive source without narrowing the regex
# itself — a narrower "must contain a / before the extension" fix was
# considered and rejected: bare filename citations like `memo_send.py` are
# common and legitimate, and requiring a slash would gut recall on those.
_URL_RE = re.compile(r"https?://\S+")

# PAIRS-per-locus bound (audit: state/audits/2026-08-12-supersession-candidate-
# pair-blowup.md). `_discriminating_locus_cutoff` bounds how many MEMOS may
# cite a locus, but the candidate set is PAIRWISE — a locus sitting exactly at
# that cutoff (14 memos on the 264-memo corpus measured 2026-08-12) still
# contributes up to 14*13/2 = 91 pairs, and the memo-count cutoff has no way
# to see that quadratic term. This bound caps the number of PAIRS any single
# locus may contribute to `same-sender-same-locus`, independent of corpus
# size: a locus that is "discriminating enough" by the memo-frequency test is
# not, by that fact, entitled to an unbounded fan-out of pairs. Held at the
# same floor as `_MAX_MEMOS_PER_DISCRIMINATING_LOCUS` deliberately — pairing
# more densely doesn't make a locus a *stronger* signal, so there's no
# corpus-scaling argument for raising it the way `_DISCRIMINATING_LOCUS_SHARE`
# raises the memo-frequency cutoff on large corpora.
_MAX_PAIRS_PER_LOCUS = 3

# A locus cited across many memos carries no thread signal — `SKILL.md`,
# `CLAUDE.md`, `__init__.py` and friends are ubiquitous surfaces, not shared
# subject matter, and pairing on them manufactures supersession candidates an
# EM reading the two memos would reject outright (observed on this repo's own
# 61-memo pile: `SKILL.md` alone was cited by 9 unrelated memos). A locus
# counts as DISCRIMINATING only when at most this many distinct open memos
# cite it. Corpus-derived rather than a hand-maintained blocklist, so a
# ubiquitous surface this repo has not met yet is still excluded, and a name
# that stops being ubiquitous stops being excluded.
#
# Direction of the tradeoff is deliberate: a MISSED candidate costs the EM one
# read they were going to do anyway (every memo is triaged regardless), while a
# FALSE candidate risks retiring a live ask. Precision over recall.
#
# Review: code-reviewer F4 — a bare absolute count doesn't scale across
# corpus sizes: on a small inbox (e.g. 4 open memos) an absolute floor alone
# would treat a locus most of a small team's genuinely-related thread cites
# as noise, while on a large inbox (200+ memos) that same absolute count
# would exclude a locus cited by only 2% of the corpus — a proportionally
# much stronger discriminating signal than the same count on the 61-memo
# corpus this constant was tuned against. `_MAX_MEMOS_PER_DISCRIMINATING_LOCUS`
# is therefore a FLOOR (unchanged, keeps small-inbox behaviour identical to
# before this fix), and `_DISCRIMINATING_LOCUS_SHARE` raises the effective
# cutoff on large corpora as a share of the open count — see
# `_discriminating_locus_cutoff`.
_MAX_MEMOS_PER_DISCRIMINATING_LOCUS = 3
_DISCRIMINATING_LOCUS_SHARE = 0.05

# Basis strength ordering for the final candidate sort (AC3) — lower ranks
# first. `self-declared` sorts above `declared`, which stays above
# `same-sender-same-locus`; an unrecognized basis sorts last rather than
# raising, so a future basis added without updating this table degrades to
# "sorts last" instead of crashing the op.
_BASIS_RANK = {
    "self-declared": 0,
    "declared": 1,
    "same-sender-same-locus": 2,
}

# In-body self-declaration — a sender announcing supersession in memo PROSE
# rather than via the `supersedes:` frontmatter field. Added 2026-08-03 after
# DoE-claude ran this op against a real 52-memo pile: `same-sender-same-locus`
# went 0-for-46, while the two real confirmations both announced themselves in
# the body and neither carried `supersedes:` (so neither fired `declared`
# either). The two confirmed strings — "Superseding it" and "read this one as
# authoritative where the two disagree" — are syntactically unalike (one names
# the act with a supersession verb, the other is a precedence claim that never
# says "supersede" at all), so this is TWO independent phrase patterns, not one
# template loosened until both match.
#
# Bias to precision over recall, deliberately (plan Anti-scope): a MISSED
# self-declaration costs nothing new — the locus basis still offers the pair
# advisorily and every memo is read regardless — while a FALSE self-declared
# candidate promotes a wrong pair to the TOP-ranked basis, the one place in
# this op where a false positive is expensive. That is why detection requires
# co-occurrence with a concrete reference to another memo (AC5) rather than
# firing on the phrase alone: "we are superseding our old process" with no
# other memo in sight must not fire, and a generic "the previous memo" phrase
# only pairs when exactly one same-sender earlier memo exists to be it —
# an ambiguous multi-candidate case is skipped rather than guessed.
_SUPERSEDING_VERB_RE = re.compile(r"\bsupersed(?:e|es|ed|ing)\b", re.IGNORECASE)
# Review: code-reviewer F1 — a bare "authoritative ... disagree" proximity
# window false-positives on an unrelated same-line clause pair, e.g. "Our
# position remains authoritative, though regional offices disagree on this
# point." The confirmed real string is "read this one as authoritative WHERE
# the two disagree" — anchoring on the "where" clause-linking word (rather
# than widening the excluded-character class to chase every possible
# clause-boundary punctuation/conjunction) is truer to the one shape actually
# observed, and rejects the constructed false positive above (which has no
# "where" at all).
_AUTHORITATIVE_PRECEDENCE_RE = re.compile(
    r"\bauthoritative\b\s+where\b(?:(?!\.).){0,80}?\bdisagree\b", re.IGNORECASE
)

# A reference to ANOTHER memo that names no basename at all — the generic
# "the earlier/previous/prior/other memo" phrase, and nothing else.
#
# The literal token `in_reply_to` was an arm of this pattern until 2026-08-30,
# so a memo whose frontmatter carried a real `in_reply_to:` matched here and
# was then paired by `_nearest_earlier_same_sender` — date proximity, no
# topical link whatsoever — while the frontmatter naming the actual thread sat
# unread two fields away. The op held the answer and discarded it, and the
# pair it guessed instead was promoted to the TOP-ranked basis. `in_reply_to:`
# is now resolved from its own frontmatter value (`_declared_in_reply_to`) and
# never routes to the proximity fallback; a value that does not resolve inside
# the open pile yields NO candidate rather than a nearest-dated substitute.
_MEMO_REFERENCE_PHRASE_RE = re.compile(
    r"\b(?:the\s+)?(?:earlier|previous|prior|other|that)\s+memo\b",
    re.IGNORECASE,
)


def _has_supersession_phrase(text: str) -> bool:
    """True if `text` contains either DoE-confirmed self-declaration form."""
    return bool(
        _SUPERSEDING_VERB_RE.search(text) or _AUTHORITATIVE_PRECEDENCE_RE.search(text)
    )


def _nearest_earlier_same_sender(newer: dict, records: list[dict]) -> Optional[dict]:
    """The single closest-dated earlier open memo from `newer`'s own sender.

    Used only for the generic "the previous memo" reference form, which names
    no specific memo — precision-over-recall means this returns the nearest
    candidate, but the caller only accepts it when it is the UNIQUE nearest
    (see `_self_declared_candidates`); it does not disambiguate among ties.

    Review: code-reviewer F3 — nearest-by-date is a heuristic PROXY for "the
    memo this prose refers to," with no topical correlation at all. A sender
    running two concurrent unrelated threads could have this resolve to the
    wrong one: the true referent is an older thread, but a different,
    unrelated same-sender memo happens to be nearer in time. Known, bounded
    limitation, not fixed here — the locus basis still offers the pair
    advisorily, and a human reads every candidate regardless (see this
    module's Anti-scope commentary at `_SUPERSEDING_VERB_RE`).
    """
    if not newer["created_known"]:
        return None
    earlier = [
        record
        for record in records
        if record is not newer
        and record["sender"] == newer["sender"]
        and record["created_known"]
        and record["created"] < newer["created"]
    ]
    if not earlier:
        return None
    earlier.sort(key=lambda record: record["created"], reverse=True)
    return earlier[0]


def _discriminating_locus_cutoff(open_count: int) -> int:
    """Corpus-scaled cutoff for `_MAX_MEMOS_PER_DISCRIMINATING_LOCUS`.

    `max(floor, ceil(share * open_count))` — the floor governs on small and
    medium inboxes exactly as the bare constant did before this fix; the
    share only overtakes the floor once the corpus is large enough that a
    5% share exceeds it (60+ open memos at the current constants).
    """
    return max(
        _MAX_MEMOS_PER_DISCRIMINATING_LOCUS,
        math.ceil(_DISCRIMINATING_LOCUS_SHARE * open_count),
    )


def _validate_params(params: dict):
    """Validate memo.blitz_buckets params; return the validated tuple or an error dict.

    Required: dry_run (bool) — must be True; this op has no act mode.
    Optional: open_threshold (int > 0), age_days_threshold (int > 0) — the two
        escalation legs, defaulted from the module constants. Supplied
        explicitly so a caller can tune the trigger without this op owning the
        ceremony's policy.
    Optional: inbox_dir (str) — absolute path to an inbox to read INSTEAD of
        the calling repo's own. Exists for fixture-driven tests and for a
        ceremony auditing a sibling's pile; when absent the calling worktree's
        cross-repo/inbox/ is used.

        Trust boundary (Review: code-reviewer F7): this override is
        CALLER-TRUSTED BY DESIGN — no containment check restricts it to a
        known worktree root, and it is used directly to glob + read
        frontmatter from whatever directory it names. Constraining it would
        break the documented test/sibling-audit seam this param exists for.
        Safe only because the op is COMPUTE_ONLY and reachable exclusively
        over the local engine's UDS surface, not an untrusted network
        boundary; a caller must not point it at a directory whose metadata
        the caller is not already entitled to read.
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.blitz_buckets: dry_run must be bool, got "
            + repr(type(dry_run).__name__),
        )
    if dry_run is False:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.blitz_buckets: dry_run must be true — this is a pure read op "
            "with no act mode (it never writes, regardless of this flag).",
        )

    thresholds = {}
    for key, default in (
        ("open_threshold", _DEFAULT_OPEN_THRESHOLD),
        ("age_days_threshold", _DEFAULT_AGE_DAYS_THRESHOLD),
    ):
        value = params.get(key, default)
        # bool is an int subclass — reject it explicitly, or `open_threshold:
        # true` would silently mean 1.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.blitz_buckets: {key} must be a positive int when "
                f"supplied, got {value!r}",
            )
        thresholds[key] = value

    inbox_dir = params.get("inbox_dir")
    if inbox_dir is not None and not isinstance(inbox_dir, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.blitz_buckets: inbox_dir, when supplied, must be a string path",
        )

    return (
        dry_run,
        thresholds["open_threshold"],
        thresholds["age_days_threshold"],
        inbox_dir,
    )


def _created_date(frontmatter: dict, path: Path) -> Optional[datetime.date]:
    """Resolve a memo's creation date from `created:`, falling back to the
    DR-026 filename date prefix.

    The filename fallback is not cosmetic: the age leg of the escalation
    trigger is what catches slow accretion, and a memo whose `created:` field
    is missing or unparseable would otherwise be silently ageless and drag the
    oldest-open figure toward "nothing is old here."
    """
    raw = frontmatter.get("created")
    if isinstance(raw, datetime.date):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.date.fromisoformat(raw.strip()[:10])
        except ValueError:
            pass
    match = _DATE_PREFIX_RE.match(path.name)
    if match:
        try:
            return datetime.date.fromisoformat(match.group(1))
        except ValueError:
            return None
    return None


def _space_key(frontmatter: dict, path: Path) -> tuple[str, bool]:
    """Return (space_key, declared_by_sender).

    Prefers the sender-declared `space:` field. When absent, falls back to the
    memo's topic slug — the filename with its `YYYY-MM-DD-<sender>-` prefix
    stripped — which is a weaker but non-empty grouping key. The boolean is the
    point of the return shape: a consumer must be able to tell a sender's own
    thread declaration from this op's guess, because only the former collapses
    the expensive judgment step the `space:` field was added to collapse.
    """
    declared = frontmatter.get("space")
    if isinstance(declared, str) and declared.strip():
        return declared.strip(), True
    stem = path.name[:-3] if path.name.endswith(".md") else path.name
    stem = _DATE_PREFIX_RE.sub("", stem)
    return stem, False


def _reference_basename(reference: str) -> str:
    """The basename form a memo reference is matched by, `.md` suffix assured.

    `supersedes:` and `in_reply_to:` are authored in every root the corpus
    admits -- a bare basename, a repo-relative `cross-repo/inbox/<name>.md`,
    a `state/memo-outbox/sent/<name>.md`, and absolute POSIX paths from
    another machine's checkout. The record index these resolve against is
    keyed on basenames (`_cited_loci`'s own reason), so a reference carrying
    ANY directory prefix missed every key and the declaration was dropped
    without a trace -- taking with it the `declared` basis that would have
    outranked a prose-guessed pairing for the same memo.
    """
    name = reference.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return name if name.endswith(".md") else f"{name}.md"


def _declared_supersedes(frontmatter: dict) -> list[str]:
    """Normalize `supersedes:` to a list of basename references (see
    `_reference_basename` for why the authored path root is discarded)."""
    raw = frontmatter.get("supersedes")
    if isinstance(raw, str) and raw.strip():
        return [_reference_basename(raw)]
    if isinstance(raw, list):
        return [
            _reference_basename(entry)
            for entry in raw
            if isinstance(entry, str) and entry.strip()
        ]
    return []


def _declared_in_reply_to(frontmatter: dict) -> Optional[str]:
    """The basename `in_reply_to:` names, or None when the key is absent or
    carries no usable value."""
    raw = frontmatter.get("in_reply_to")
    if isinstance(raw, str) and raw.strip():
        return _reference_basename(raw)
    return None


def _cited_loci(title: str, body: str) -> set[str]:
    """Path-shaped loci cited in a memo's title or body, as basenames.

    Basenames rather than full paths because two memos in one thread routinely
    cite the same file by different relative roots (`coordinator/x.py` vs
    `./x.py`), and a same-sender pair citing the same file is exactly the
    signal this feeds.
    """
    text = _url_stripped_text(title, body)
    return {
        match.rsplit("/", 1)[-1]
        for match in _LOCUS_RE.findall(text)
    }


def _url_stripped_text(title: str, body: str) -> str:
    """`title`+`body` with URLs removed (Review: code-reviewer F6).

    Shared by `_cited_loci` and the self-declared phrase detection (see
    `_has_supersession_phrase`'s caller in `_build_candidates`) so a URL path
    segment or query string containing incidental phrase/locus tokens is
    guarded consistently rather than only for locus extraction.
    """
    return _URL_RE.sub(" ", f"{title}\n{body}")


def _read_memo(path: Path) -> Optional[dict]:
    """Read one inbox memo into the record shape the bucketing works over.

    Returns None for a file with no parseable frontmatter — a malformed or
    non-memo file in the inbox is skipped rather than failing the whole sweep,
    since the pile this op exists to grind is exactly where a stray file is
    most likely to have accumulated. The skip is COUNTED and surfaced (see
    `_unreadable` in the handler) rather than swallowed silently.
    """
    try:
        # Review: code-reviewer F1 — UnicodeDecodeError (a ValueError subclass,
        # not an OSError subclass) is raised by read_text() on a non-UTF-8
        # file and must be caught here too, or one stray binary/mis-encoded
        # file in the inbox crashes the whole sweep, contradicting this
        # function's own "never fails the whole sweep" guarantee above.
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    split = split_frontmatter(content)
    if split is None:
        return None
    try:
        frontmatter = parse_yaml(split.fm_text)
    except ValueError:
        # Review: code-reviewer F5 — parse_yaml's only documented parse-
        # failure signal is ValueError (see _parse_inline_list's malformed-
        # inline-list raise in schema_validate.py); narrowed from a bare
        # `except Exception` so a genuine parser bug (e.g. AttributeError
        # from a regression) propagates instead of masquerading as a
        # malformed memo.
        return None
    if not isinstance(frontmatter, dict):
        return None
    return {
        "path": path,
        "frontmatter": frontmatter,
        "body": split.body_with_leading_newline,
    }


def _dominant_sender(by_sender: dict[str, int], open_count: int) -> Optional[str]:
    """Resolve the dominant correspondent, or None when the pile has no shape.

    None below `_DOMINANT_MIN_OPEN` open memos or below `_DOMINANT_SHARE` of the
    pile — a "dominant" sender resolved off two memos would split the fan-out
    into a bucket of two and a bucket of three for no gain. Ties resolve to the
    alphabetically-first sender so repeated calls over an unchanged inbox agree.
    """
    if open_count < _DOMINANT_MIN_OPEN or not by_sender:
        return None
    top = min(by_sender.items(), key=lambda item: (-item[1], item[0]))
    sender, count = top
    return sender if count >= _DOMINANT_SHARE * open_count else None


def _self_declared_candidates(records: list[dict], seen_pairs: set[frozenset[str]]) -> list[dict]:
    """The `self-declared` basis — supersession announced in memo BODY prose.

    Fires for a same-sender (newer, older) pair when the newer memo's
    title+body matches a supersession phrase pattern (AC2) AND names the
    older memo concretely (AC5), resolved in strictly descending order of
    evidence: a directly-cited basename (already captured in `loci`), then
    the memo's own `in_reply_to:` frontmatter, then — only when neither
    exists — a generic "the previous/earlier memo" phrase resolved to the
    unique nearest earlier same-sender memo. Ambiguous references — a
    citation matching more than one same-sender memo's basename — are
    skipped rather than guessed, and the generic phrase only resolves when
    exactly one same-sender earlier memo exists to be it (see
    `_nearest_earlier_same_sender`); see the precision-over-recall
    commentary at `_SUPERSEDING_VERB_RE`.

    A memo carrying `supersedes:` NEVER enters this pass at all. Ranking
    `self-declared` above `declared` is about which INFERENCE to trust when
    the sender left the field empty — it was never a licence for prose to
    overrule the sender's own structured statement of the same fact, which
    is what the unconditional version of this pass did (see the `continue`
    on `_declared_supersedes` below).

    Ranked above `declared` and `same-sender-same-locus` (AC3) for the memos
    it does still cover: this function runs FIRST in
    `_supersession_candidates`, so it claims a pair in `seen_pairs` before
    either of the other passes sees it.
    """
    candidates: list[dict] = []
    all_names = {record["path"].name: record for record in records}

    for newer in records:
        if not _has_supersession_phrase(newer["text"]):
            continue
        if _declared_supersedes(newer["frontmatter"]):
            # The memo states in its own frontmatter what it supersedes. That
            # is the sender's structured claim about the very question this
            # pass infers from prose, so prose does not get to answer it: the
            # memo is yielded to the `declared` pass rather than claiming its
            # pair in `seen_pairs` against a target the body merely implies.
            # Without this, a memo declaring `supersedes: A` whose prose also
            # trips the phrase pattern was emitted against a different memo B
            # at the TOP-ranked basis — the shape DoE-claude reported on
            # 2026-08-30, where a klabauter memo resolved to the wrong target.
            continue
        older = None
        cited = [
            record
            for name, record in all_names.items()
            if name in newer["loci"] and record is not newer
        ]
        cited_same_sender = [r for r in cited if r["sender"] == newer["sender"]]
        reply_target = _declared_in_reply_to(newer["frontmatter"])
        if len(cited_same_sender) == 1:
            older = cited_same_sender[0]
        elif reply_target is not None:
            # A declared thread outranks every inference below it, and its
            # ABSENCE from the open pile is an answer too: the thread it names
            # is archived, or was never received here, so there is no open
            # pair to offer. Never fall through to proximity from here.
            older = all_names.get(reply_target)
            if older is newer:
                older = None
        elif not cited and _MEMO_REFERENCE_PHRASE_RE.search(newer["text"]):
            # No basename citation at all (any sender), and no declared
            # thread — only THEN fall back to the generic "the previous memo"
            # resolution. A citation that named a different sender's memo is
            # not evidence for guessing a same-sender pairing instead.
            older = _nearest_earlier_same_sender(newer, records)
        if older is None:
            continue
        if older["sender"] != newer["sender"]:
            # `in_reply_to` crosses senders routinely — a reply names the memo
            # it answers — and a cross-sender pair is a correspondence edge,
            # not a supersession: nobody supersedes someone else's memo.
            continue
        if not newer["created_known"] or not older["created_known"]:
            continue
        if older["created"] >= newer["created"]:
            # A "self-declaration" citing a memo that is not, in fact, older
            # is not a supersession claim this op can trust mechanically.
            continue
        pair = frozenset({newer["path"].name, older["path"].name})
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        candidates.append({
            "kind": "supersession_candidate",
            "id": f"{newer['path'].name} -> {older['path'].name}",
            "newer": newer["path"].name,
            "older": older["path"].name,
            "sender": newer["sender"],
            "basis": "self-declared",
            "shared_loci": [],
            "advisory": False,
            "note": (
                "sender announced this supersession in the memo BODY, in "
                "prose — still a CANDIDATE: confirm the newer memo actually "
                "RESOLVES the older one rather than merely re-raising it."
            ),
        })

    return candidates


def _supersession_candidates(records: list[dict]) -> list[dict]:
    """Emit supersession CANDIDATES — never confirmations (see module negative-spec).

    Three independent signals, each reported with its own `basis` so the EM
    can weigh them differently, ranked strongest-first:

      - `self-declared` — a later memo's BODY PROSE announces the
        supersession (e.g. "Superseding it", "read this one as authoritative
        where the two disagree"), scoped to prose that also names the older
        memo. Added 2026-08-03: this is the signal that actually fired on
        DoE-claude's real pile when neither of the other two bases did.
      - `declared` — a later memo's own `supersedes:` names an earlier one.
        The sender said so; this is the strong structured signal, and the
        whole reason the field was added.
      - `same-sender-same-locus` — same sender, later date, overlapping cited
        loci. This is the inference example-retrieval-repo ran by hand; against
        DoE-claude's 52-memo pile it went 0-for-46 read as a primary signal,
        which is why it now carries an explicit `advisory` marker (AC4)
        rather than being read as comparably strong to the two declaration
        bases. It is a CANDIDATE and nothing more: two memos can touch the
        same file without either resolving the other.

    A pair is emitted AT MOST ONCE, preferring the strongest basis available
    (AC3) — enforced by running the passes in rank order and having each
    later pass skip a pair already claimed in `seen_pairs`.

    Review: code-reviewer F2 — `seen_pairs` is keyed on an UNORDERED
    frozenset({name_a, name_b}), not an ordered tuple. The declared pass runs
    first and its direction is the sender's own claim, so declared wins
    unconditionally; the inferred pass only ever SKIPS a pair already in
    `seen_pairs` rather than re-emitting it with a (possibly inverted)
    direction. An ordered-tuple key would let a malformed/backdated
    `created:` disagree with the sender's declared direction and silently
    double-emit the same pair with contradictory newer/older claims.
    """
    candidates: list[dict] = []
    seen_pairs: set[frozenset[str]] = set()

    candidates.extend(_self_declared_candidates(records, seen_pairs))

    by_basename = {record["path"].name: record for record in records}

    locus_frequency: dict[str, int] = {}
    for record in records:
        for locus in record["loci"]:
            locus_frequency[locus] = locus_frequency.get(locus, 0) + 1
    cutoff = _discriminating_locus_cutoff(len(records))
    discriminating = {
        locus
        for locus, count in locus_frequency.items()
        if count <= cutoff
    }

    for record in records:
        newer_name = record["path"].name
        for ref_name in _declared_supersedes(record["frontmatter"]):
            older = by_basename.get(ref_name)
            if older is None or older["path"].name == newer_name:
                # A declared reference pointing outside the OPEN pile is not a
                # candidate to offer — it names a memo that is already
                # archived/dispositioned, or one this repo never received.
                continue
            pair = frozenset({newer_name, older["path"].name})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            candidates.append({
                "kind": "supersession_candidate",
                "id": f"{newer_name} -> {older['path'].name}",
                "newer": newer_name,
                "older": older["path"].name,
                "sender": record["frontmatter"].get("from"),
                "basis": "declared",
                "shared_loci": [],
                "advisory": False,
                "note": (
                    "sender declared this supersession via supersedes: — "
                    "still a CANDIDATE: confirm the newer memo actually "
                    "RESOLVES the older one rather than merely re-raising it."
                ),
            })

    # Per-locus pair budget (see `_MAX_PAIRS_PER_LOCUS`) — a locus that has
    # already spent its budget stops contributing NEW pairs, but a pair with
    # at least one locus still under budget is still emitted (reported against
    # only the loci that actually admitted it, so `shared_loci` never lists a
    # locus that didn't count toward this pair's admission).
    locus_pair_count: dict[str, int] = {}

    for i, newer in enumerate(records):
        for older in records[i + 1:]:
            newer_rec, older_rec = newer, older
            # Review: code-reviewer F3 — a record with no resolvable date
            # stores the `datetime.date.min` sentinel (see `_build_candidates`
            # below) so it always sorts as "older" against a genuinely dated
            # record. Skipping either side when `created_known` is False
            # keeps that sentinel out of the ordering comparison entirely,
            # rather than letting it synthesize a confident but bogus
            # direction between a real memo and a placeholder.
            if not newer_rec["created_known"] or not older_rec["created_known"]:
                continue
            if newer_rec["created"] == older_rec["created"]:
                continue
            if newer_rec["created"] < older_rec["created"]:
                newer_rec, older_rec = older_rec, newer_rec
            if newer_rec["sender"] != older_rec["sender"]:
                continue
            pair = frozenset({newer_rec["path"].name, older_rec["path"].name})
            if pair in seen_pairs:
                continue
            shared_all = newer_rec["loci"] & older_rec["loci"] & discriminating
            if not shared_all:
                continue
            shared = sorted(
                locus for locus in shared_all
                if locus_pair_count.get(locus, 0) < _MAX_PAIRS_PER_LOCUS
            )
            if not shared:
                continue
            seen_pairs.add(pair)
            for locus in shared:
                locus_pair_count[locus] = locus_pair_count.get(locus, 0) + 1
            candidates.append({
                "kind": "supersession_candidate",
                "id": f"{newer_rec['path'].name} -> {older_rec['path'].name}",
                "newer": newer_rec["path"].name,
                "older": older_rec["path"].name,
                "sender": newer_rec["sender"],
                "basis": "same-sender-same-locus",
                "shared_loci": sorted(shared),
                # AC4 — distinguishes this basis from the two declaration
                # bases (`self-declared`, `declared`): 0-for-46 against
                # DoE-claude's real pile as a PRIMARY signal, so a consumer
                # must be able to tier it without re-deriving what the basis
                # string means.
                "advisory": True,
                "note": (
                    "same sender, later date, overlapping cited locus — a "
                    "CANDIDATE only. Two memos can touch the same file "
                    "without either resolving the other; confirm before "
                    "retiring the older one."
                ),
            })

    return sorted(candidates, key=lambda c: (_BASIS_RANK.get(c["basis"], 99), c["id"]))


def _build_candidates(
    inbox_dir: Path, open_threshold: int, age_days_threshold: int, today: datetime.date
) -> list[dict[str, Any]]:
    """Build the full candidate list — memo buckets, summary, candidates, trigger."""
    paths = sorted(inbox_dir.glob("*.md")) if inbox_dir.is_dir() else []

    records: list[dict] = []
    unreadable: list[str] = []
    for path in paths:
        read = _read_memo(path)
        if read is None:
            unreadable.append(path.name)
            continue
        frontmatter = read["frontmatter"]
        status = frontmatter.get("status")
        if not isinstance(status, str) or status.strip() not in _OPEN_STATUSES:
            continue
        created = _created_date(frontmatter, path)
        space, space_declared = _space_key(frontmatter, path)
        sender = frontmatter.get("from")
        title = frontmatter.get("title")
        title_text = title if isinstance(title, str) else ""
        records.append({
            "path": path,
            "frontmatter": frontmatter,
            "sender": sender if isinstance(sender, str) else "(unknown)",
            # kind absent is valid on the pre-DR-214-D4 corpus; the reader's
            # documented default is `ask`, applied here rather than emitting a
            # null bucket the consumer would have to special-case.
            "kind": frontmatter.get("kind") if isinstance(frontmatter.get("kind"), str) else "ask",
            "space": space,
            "space_declared": space_declared,
            "created": created or datetime.date.min,
            "created_known": created is not None,
            "loci": _cited_loci(title_text, read["body"]),
            # URL-stripped title+body, for the self-declared basis's
            # phrase-pattern pass — kept separate from `loci` since phrase
            # detection reads prose, not path-shaped tokens, but shares the
            # same URL-stripping precaution (Review: code-reviewer F6).
            "text": _url_stripped_text(title_text, read["body"]),
        })

    open_count = len(records)
    by_kind: dict[str, int] = {}
    by_sender: dict[str, int] = {}
    for record in records:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
        by_sender[record["sender"]] = by_sender.get(record["sender"], 0) + 1

    dominant = _dominant_sender(by_sender, open_count)

    candidates: list[dict[str, Any]] = []
    for record in records:
        # fyi wins over dominant deliberately: the fyi sweep's whole value is
        # re-judging a label the sender applied from THEIR vantage, and folding
        # a dominant correspondent's fyi memos into the thread-reconstruction
        # bucket would lose exactly the re-judgement that surfaced a
        # break-class defect in example-retrieval-repo's run.
        if record["kind"] == "fyi":
            bucket = "fyi"
        elif dominant is not None and record["sender"] == dominant:
            bucket = "dominant"
        else:
            bucket = "rest"
        age_days = (today - record["created"]).days if record["created_known"] else None
        candidates.append({
            "kind": "bucket",
            "id": record["path"].name,
            "bucket": bucket,
            "memo_kind": record["kind"],
            "from": record["sender"],
            "space": record["space"],
            "space_declared": record["space_declared"],
            "created": record["created"].isoformat() if record["created_known"] else None,
            "age_days": age_days,
            "path": str(record["path"]),
        })

    # Snapshot before the non-bucket entries are appended — deriving these
    # counts from `candidates` after the fact would silently start counting
    # whatever else lands in the list.
    bucket_counts = {
        name: sum(1 for c in candidates if c.get("bucket") == name)
        for name in ("fyi", "dominant", "rest")
    }
    spaces_declared = sum(1 for c in candidates if c.get("space_declared"))

    candidates.append({
        "kind": "bucket_summary",
        "id": "bucket_summary",
        "open_count": open_count,
        "by_kind": dict(sorted(by_kind.items())),
        "by_sender": dict(sorted(by_sender.items(), key=lambda kv: (-kv[1], kv[0]))),
        "dominant_sender": dominant,
        "dominant_count": by_sender.get(dominant, 0) if dominant else 0,
        "bucket_counts": bucket_counts,
        "spaces_declared": spaces_declared,
        "unreadable": unreadable,
        "note": (
            "unreadable[] lists inbox files skipped for absent/unparseable "
            "frontmatter — counted, not silently swallowed."
            if unreadable else None
        ),
    })

    candidates.extend(_supersession_candidates(records))

    ages = [
        (today - record["created"]).days
        for record in records
        if record["created_known"]
    ]
    oldest_age = max(ages) if ages else 0
    count_leg = open_count > open_threshold
    age_leg = oldest_age > age_days_threshold
    candidates.append({
        "kind": "trigger",
        "id": "trigger",
        "open_count": open_count,
        "oldest_open_age_days": oldest_age,
        "open_threshold": open_threshold,
        "age_days_threshold": age_days_threshold,
        "count_leg_tripped": count_leg,
        "age_leg_tripped": age_leg,
        "fires": count_leg or age_leg,
        "note": (
            "Either leg fires the escalation. The AGE leg is the load-bearing "
            "one — accretion, not volume on any one day, is the observed "
            "failure mode."
        ),
    })

    return candidates


@register_op("memo.blitz_buckets")
def _memo_blitz_buckets(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'memo.blitz_buckets' COMPUTE_ONLY UDS op handler.

    Return the mechanical half of an inbox blitz — buckets, sender/kind
    distribution, supersession CANDIDATES, and the count+age escalation
    trigger — over the calling repo's own `cross-repo/inbox/`.

    repo_root is the git common dir (`_OP_KEY_SCOPE = "common_dir"`), from
    which the caller's own worktree is derived — this op reads the CALLING
    repo's inbox, exactly as memo.list_outbox reads the calling repo's outbox.
    An explicit `inbox_dir` param overrides that derivation and is the only
    path that does not need a resolved worktree.

    Params:
        dry_run            (bool, required): must be True — no act mode.
        open_threshold     (int, optional):  count leg, default 10.
        age_days_threshold (int, optional):  age leg in days, default 7.
        inbox_dir          (str, optional):  absolute inbox path to read
                                             instead of the calling repo's own.

    Returns:
        The `build_dry_run_result` envelope; new structured data rides INSIDE
        `candidates` under a `kind` discriminator (`bucket` /
        `bucket_summary` / `supersession_candidate` / `trigger`) rather than as
        new top-level envelope keys — the fleet.* wire envelope is frozen at
        the top level (contract §2.1), the same constraint memo.list works
        under.
    """
    validated = _validate_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    dry_run, open_threshold, age_days_threshold, inbox_dir_param = validated

    if inbox_dir_param is not None:
        inbox_dir = Path(inbox_dir_param)
    else:
        if repo_root is None:
            return build_setup_error_result(
                _MODE, dry_run,
                "memo.blitz_buckets: no repo_root supplied and no inbox_dir "
                "override — this op reads the CALLING repo's own "
                "cross-repo/inbox/ and requires a resolved worktree "
                "(common_dir-keyed op).",
            )
        inbox_dir = main_worktree_root(Path(repo_root)).joinpath(*_INBOX_DIRNAME)

    candidates = _build_candidates(
        inbox_dir, open_threshold, age_days_threshold, datetime.date.today()
    )
    return build_dry_run_result(_MODE, candidates)
