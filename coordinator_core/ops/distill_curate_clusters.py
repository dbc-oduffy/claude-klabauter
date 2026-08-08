"""
coordinator_core.ops.distill_curate_clusters — JSON-RPC "distill.curate_clusters" operation.

Purpose: the mechanical gate between `/distill` Wave 1 (nugget extraction, each
nugget tagged with a free-text ``system_tag``) and Wave 2 (one ``docs/wiki/<topic>.md``
per cluster). Given the observed ``system_tag`` set, returns a deterministic
keep/normalize/merge/drop verdict per tag — so no model is ever asked "is this a
misc bucket?" (PM ruling, 2026-08-06: no "misc" bucket is permitted in results;
the discrimination must be mechanical, not a dice roll delegated to an agent).

Wire params:
    tag_counts (dict[str, int], REQUIRED) — ``{raw_system_tag: nugget_count}``.
        Passed directly rather than re-derived from a nugget-store file on disk:
        Wave 1's orchestrator already holds the parsed nugget records in memory/
        context by the time this gate runs, and re-parsing a nugget storage
        format here would (a) couple this op to that format's schema drift and
        (b) risk reading a DIFFERENT snapshot than the one the caller is
        actually curating, which breaks purity (same call, same params, same
        output — DR-208 COMPUTE_ONLY). A plain ``{tag: count}`` dict is the
        minimal, format-agnostic input this gate actually needs. Scope "none":
        no repo-specific state is read (mirrors workflow_validate.py's
        registration pattern, one level purer still — no file I/O at all).
    keep_threshold (int, optional) — minimum aggregate nugget count (summed
        across every tag folded into one cluster) for that cluster to stand
        on its own. Below this, the cluster has no volume to justify a
        dedicated wiki file even after every shape/merge fold-in.

        Explicit value: used verbatim, always — no auto-adjustment. This is
        load-bearing: callers comparing the SAME corpus at two thresholds
        (e.g. a sibling repo's 1-vs-2 measurement) need a promise that
        passing a value never gets silently overridden underneath them.

        Omitted: derived, corpus-aware. The threshold exists to assert a
        cluster has enough volume to justify a dedicated wiki file — that
        premise breaks down on a cold-start corpus where nearly every family
        is count-1, so threshold 2 is no longer discriminating between
        clusters, it is just deleting the corpus (2026-08-06 census: a
        20-nugget first run loses 71% of its nuggets at threshold 2). The
        derivation computes what SHARE of total nuggets threshold 2 would
        drop; if that share exceeds 25%, threshold falls back to 1, else
        stays at 2. Expressed as a share of nuggets lost — the unit that
        actually matters — rather than a magic corpus-size cutoff. 25% is
        calibrated against the census: the mature 433-nugget corpus loses
        17.1% at threshold 2 (stays at 2), while small cold-start corpora
        clear the bar and flip to 1. Pure function of tag_counts only — no
        wallclock, no randomness, no I/O (DR-208 COMPUTE_ONLY).

Reply fields:
    {
      "verdicts": [
        {"tag": str, "verdict": "keep"|"normalize"|"merge"|"drop",
         "canonical_slug": str|None, "merge_target": str|None,
         "count": int, "reason": str,
         "drop_cause": "placeholder"|"bare-no-sibling"|"below-threshold"|None},
        ...
      ],  # sorted by tag, deterministic
      "counts": {
        "total": int, "keep": int, "normalize": int, "merge": int, "drop": int,
        "drop_by_cause": {"placeholder": int, "bare-no-sibling": int, "below-threshold": int},
      },
      "threshold_applied": int,   # the keep_threshold actually used
      "threshold_auto": bool,     # True iff it was derived, not supplied
      "nugget_drop_share": float, # dropped nugget count / total nugget count, rounded to 4dp
      "degraded": bool,
    }

Downstream-consumer contract (ratified 2026-08-06 with example-doctrine-repo-em, whose
`/distill` C3 leg records dropped nuggets as EPHEMERAL-with-reason rather than
letting them vanish silently; that logging is load-bearing on both fields
below, so they are a stability commitment, not an implementation detail):

  - **The drop set is enumerable** — every input tag yields exactly one verdict
    entry, so the dropped set is ``[v for v in verdicts if v["verdict"] ==
    "drop"]``. It is a filter over ``verdicts``, deliberately not a separate
    top-level array: one entry per raw input tag is the invariant that keeps
    the two views from ever disagreeing.
  - **Every drop carries a non-empty, machine-stable ``reason``** naming which
    of the three drop paths fired (placeholder / bare-token-with-no-compound-
    sibling / family-total-below-``keep_threshold``).
  - **Every ``merge`` verdict carries a non-None ``merge_target``** — the
    canonical slug the tag folds INTO, which is what a clustering consumer
    needs; the merge verdict alone does not identify the destination.
    (``canonical_slug`` on a merge entry is that tag's OWN normalized slug, not
    the destination — the two fields are distinct and both populated.)

Weakening any of the three is a breaking change to a named sibling consumer.
Asserted by ``test_downstream_consumer_contract_drop_and_merge_fields``.

The discriminator (why this closes the door the PM named, and why it is NOT
another synonym denylist):

A catch-all bucket is identified STRUCTURALLY, by what a tag's normalized slug
FAILS TO CONTAIN — a compound domain-qualifier shape — not by matching its
name against a guessed word list. This corpus's tag vocabulary is, by
convention, two-part: a DOMAIN token (the subject — "git", "review-trail",
"performance", "claude-klabauter.ops.fleet-archival") plus a QUALIFIER token (the aspect
of that subject — "safety", "scope", "optimization"). A tag whose normalized
slug is a single bare token (no hyphen-separated qualifier, e.g. "meta",
"coordination", "governance", or an invented one like "stewardship") never
acquired that qualifier structure — it names an activity or an abstraction,
not a subject. That absence is the structural signal, and it is exactly what
a denylist-of-guessed-synonyms cannot generalize past (a ninth unlisted bare
word closes the same way a first one does, because the test is about SHAPE,
not vocabulary — see the memo_triage.py "_GENERIC_BIGRAM_VOCAB" postmortem
this deliberately does not repeat).

A bare tag is not automatically dropped, though: if the corpus separately
contains a COMPOUND tag sharing its leading token (e.g. bare "coordination"
alongside a hypothetical "coordination-ledger"), the bare tag folds into that
family via MERGE — it turns out to have a home after all, discovered
structurally (shared leading token), never declared by name. Only a bare tag
with no such family — no compound sibling — is DROP-verdicted, and even then
the closed, small, DOCUMENTED-AS-SECONDARY word list
(_BARE_ABSTRACTION_VOCAB) is used ONLY to annotate the reason string with a
confirming note; it never gates the verdict. Removing that list entirely
changes zero verdicts — deleting it is a safe no-op check any reviewer can
run. This is the bounded-linguistic-category carve-out the brief permits
("meta"/"decisions"/"governance"/... IS a closed grammatical class — pure
abstraction nouns — not an open-ended guess at synonyms for "misc").

Fragmentation (performance-*, git-*, review-trail-* — 9/8/5 near-duplicate
tags naming the SAME subject) is folded the same structural way: every
COMPOUND slug sharing a leading token is one family. The family's total
nugget count (summed across every member, since the whole point of folding
near-duplicates together is that the fragments' individual counts under-
report the subject's real volume) decides keep-vs-drop for the family as a
unit; the highest-count COMPOUND member (never a bare member, even if it
happens to have the largest individual count — a cluster's canonical name
must itself carry the domain-qualifier shape) becomes the merge target for
every other member.

Placeholder tags (``N/A``, empty, ``tbd``, ``unknown``, ...) are a distinct,
prior check — not part of the domain-token structural test at all; a
placeholder never even reaches slug/family analysis.

Honesty contract: this op must never report a clean verdict set it cannot
support. ``degraded`` is True (and every verdict list empty) when
``tag_counts`` is missing, not a dict, or empty — the failure mode this
gate exists to prevent is exactly a silent "0 problems found" over an
un-parseable or absent input (memo.triage's 2026-08-06 live-corpus incident:
``promote: 0, degraded: false`` over a corpus that in fact demanded 131
promotions). An op that returns a clean-looking empty verdict list from a
broken input is not merely unhelpful here — it is the FAILURE MODE this op
exists to close, so it is checked first and loudly.

Negative-spec:
  - Does NOT call any LLM or model — purely structural/deterministic.
  - Does NOT read any file — tag_counts arrives as a wire param.
  - Does NOT maintain or consult a denylist of guessed catch-all synonyms
    ("misc", "other", "general", "assorted", ...) as the PRIMARY mechanism —
    see discriminator section above. The one closed word list this module
    does carry (_BARE_ABSTRACTION_VOCAB) is documented as secondary/
    annotation-only and is asserted as such by a dedicated test.
  - Does NOT write any file, issue any git command, or mutate any coordinator
    substrate — pure compute, scope "none".
  - Does NOT gate ``drop_cause`` on ``_BARE_ABSTRACTION_VOCAB`` — the three
    causes ("placeholder", "bare-no-sibling", "below-threshold") are decided
    structurally, exactly like ``verdict`` itself; the vocab list stays
    annotation-only (see the discriminator section and its dedicated test).
  - Does NOT consult wallclock time, randomness, or any I/O when deriving an
    omitted ``keep_threshold`` — the derivation is a pure function of
    ``tag_counts`` alone (DR-208 COMPUTE_ONLY).

Spec backlink: PM ruling 2026-08-06 (see dispatch brief; no standing plan doc
yet for this gate — this op predates one).
"""

from __future__ import annotations

import re
from typing import Optional

from coordinator_core.ipc import register_op

_PLACEHOLDER_VALUES = {"n/a", "na", "", "tbd", "unknown", "null", "none"}
# Review: code-reviewer Finding (2026-08-06) — "misc"/"other" were previously
# included here, which made this denylist their PRIMARY (and only) gating
# mechanism, contradicting the module's own Negative-spec claim that a
# denylist is never the primary mechanism, and making the corresponding
# entries in _BARE_ABSTRACTION_VOCAB dead code (unreachable, since this
# pre-check consumed those raw strings before the structural test ever ran).
# Removed so "misc"/"other" fall through to the structural bare-token test
# below, exactly like every other bare-abstraction word.

# Bounded, closed, purely-grammatical class (pure-abstraction nouns) — SECONDARY
# signal only. Used exclusively to annotate the "reason" string on an
# already-structurally-dropped bare tag; removing this set entirely changes
# NO verdict (see module docstring's discriminator section / the dedicated
# test asserting this). NOT the gating mechanism — the gating mechanism is
# the structural bare-vs-compound / family-membership test below.
_BARE_ABSTRACTION_VOCAB = {
    "meta",
    "decisions",
    "coordination",
    "maintenance",
    "governance",
    "ordering",
    "misc",
    "other",
}

_DEFAULT_KEEP_THRESHOLD = 2

# Corpus-aware auto-threshold cap: the share of total nuggets threshold-2
# is allowed to drop before the derivation self-limits to threshold 1. See
# module docstring's `keep_threshold` param section for the calibration.
_AUTO_THRESHOLD_DROP_SHARE_CAP = 0.25
_COLD_START_FALLBACK_THRESHOLD = 1

# Sentinel distinguishing "caller omitted keep_threshold" (auto-derive) from
# any real int value (verbatim, no auto-adjustment) — including an explicit
# 0 or negative value, which the wire handler normalizes to "omitted"
# before this function ever sees it.
_AUTO = object()

_WHITESPACE_RE = re.compile(r"\s+")
_DASH_COLLAPSE_RE = re.compile(r"-{2,}")


def _normalize_shape(raw: str) -> str:
    """Canonical kebab-slug form: dots/underscores/whitespace -> "-",
    lowercase, collapsed/trimmed dashes. Purely a SHAPE transform — does not
    decide keep/merge/drop, only what the tag would look like if it survives."""
    s = raw.strip()
    s = s.replace(".", "-").replace("_", "-")
    s = _WHITESPACE_RE.sub("-", s)
    s = s.lower()
    s = _DASH_COLLAPSE_RE.sub("-", s)
    return s.strip("-")


def _is_placeholder(raw: str, slug: str) -> bool:
    return raw.strip().lower() in _PLACEHOLDER_VALUES or slug == ""


def _leading_token(slug: str) -> str:
    return slug.split("-", 1)[0]


def _degraded_reply() -> dict:
    return {
        "verdicts": [],
        "counts": {
            "total": 0,
            "keep": 0,
            "normalize": 0,
            "merge": 0,
            "drop": 0,
            "drop_by_cause": {"placeholder": 0, "bare-no-sibling": 0, "below-threshold": 0},
        },
        "threshold_applied": 0,
        "threshold_auto": False,
        "nugget_drop_share": 0.0,
        "degraded": True,
    }


def _classify(tag_counts: dict, keep_threshold: int) -> list:
    """Pure classification pass at a FIXED keep_threshold. Returns one
    verdict dict per raw input tag (unsorted). Split out from
    ``curate_clusters`` so the auto-threshold derivation can probe
    threshold-2 behaviour without duplicating the pass logic."""
    # --- Pass 1: shape-normalize every raw tag; split placeholders out. -----
    raw_to_slug: dict[str, Optional[str]] = {}
    placeholders: set[str] = set()
    for raw in tag_counts:
        slug = _normalize_shape(raw)
        if _is_placeholder(raw, slug):
            placeholders.add(raw)
            continue
        raw_to_slug[raw] = slug

    # --- Pass 2: aggregate counts per canonical slug (folds shape-drift
    # duplicates — e.g. a dotted and a Title-Case raw tag normalizing to the
    # same slug — into one entity BEFORE family/threshold analysis). --------
    slug_counts: dict[str, int] = {}
    for raw, slug in raw_to_slug.items():
        slug_counts[slug] = slug_counts.get(slug, 0) + int(tag_counts[raw])

    # --- Pass 3: group canonical slugs into families by leading token. -----
    families: dict[str, list[str]] = {}
    for slug in slug_counts:
        families.setdefault(_leading_token(slug), []).append(slug)

    # --- Pass 4: per-family disposition. ------------------------------------
    # slug -> ("keep" | "merge" | "drop", merge_target|None, reason, drop_cause|None)
    slug_disposition: dict[str, tuple] = {}
    for _leading, members in families.items():
        members_sorted = sorted(members)
        compound_members = [s for s in members_sorted if len(s.split("-")) >= 2]
        bare_members = [s for s in members_sorted if len(s.split("-")) == 1]

        if not compound_members:
            # Every member of this family is a single bare token with no
            # compound sibling anywhere in the corpus — no domain-qualifier
            # structure was ever formed. Structural drop, not name lookup.
            for slug in bare_members:
                note = ""
                if slug in _BARE_ABSTRACTION_VOCAB:
                    note = " (also matches the closed bare-abstraction word list — secondary, non-gating confirmation)"
                slug_disposition[slug] = (
                    "drop",
                    None,
                    "bare token, no compound domain-qualifier sibling in the corpus"
                    " (no merge target)" + note,
                    "bare-no-sibling",
                )
            continue

        family_total = sum(slug_counts[s] for s in members_sorted)
        # Deterministic primary: highest family-member count among COMPOUND
        # members only (a bare member can never be the cluster's canonical
        # name — see module docstring); ties break alphabetically.
        primary = min(
            compound_members,
            key=lambda s: (-slug_counts[s], s),
        )

        if family_total < keep_threshold:
            reason = (
                f"family (leading token {_leading!r}) total count "
                f"{family_total} < keep_threshold {keep_threshold}, no merge target"
            )
            for slug in members_sorted:
                slug_disposition[slug] = ("drop", None, reason, "below-threshold")
            continue

        slug_disposition[primary] = (
            "keep",
            None,
            f"canonical family member (leading token {_leading!r}), "
            f"family total {family_total} >= keep_threshold {keep_threshold}",
            None,
        )
        for slug in members_sorted:
            if slug == primary:
                continue
            slug_disposition[slug] = (
                "merge",
                primary,
                f"shares leading token {_leading!r} with larger/canonical sibling {primary!r}",
                None,
            )

    # --- Pass 5: emit one verdict per RAW input tag. ------------------------
    verdicts = []
    for raw in placeholders:
        verdicts.append(
            {
                "tag": raw,
                "verdict": "drop",
                "canonical_slug": None,
                "merge_target": None,
                "count": int(tag_counts[raw]),
                "reason": "placeholder value (empty/N-A/tbd/unknown-shaped)",
                "drop_cause": "placeholder",
            }
        )
    for raw, slug in raw_to_slug.items():
        disposition, target, reason, drop_cause = slug_disposition[slug]
        if disposition == "drop":
            verdicts.append(
                {
                    "tag": raw,
                    "verdict": "drop",
                    "canonical_slug": None,
                    "merge_target": None,
                    "count": int(tag_counts[raw]),
                    "reason": reason,
                    "drop_cause": drop_cause,
                }
            )
        elif disposition == "merge":
            verdicts.append(
                {
                    "tag": raw,
                    "verdict": "merge",
                    "canonical_slug": slug,
                    "merge_target": target,
                    "count": int(tag_counts[raw]),
                    "reason": reason,
                    "drop_cause": None,
                }
            )
        else:  # keep, possibly with shape drift -> "normalize" instead
            if raw == slug:
                verdicts.append(
                    {
                        "tag": raw,
                        "verdict": "keep",
                        "canonical_slug": slug,
                        "merge_target": None,
                        "count": int(tag_counts[raw]),
                        "reason": reason,
                        "drop_cause": None,
                    }
                )
            else:
                verdicts.append(
                    {
                        "tag": raw,
                        "verdict": "normalize",
                        "canonical_slug": slug,
                        "merge_target": None,
                        "count": int(tag_counts[raw]),
                        "reason": "shape drift only (dotted/Title-Case/snake_case); "
                        f"canonical form is {slug!r}. " + reason,
                        "drop_cause": None,
                    }
                )

    return verdicts


def _resolve_keep_threshold(tag_counts: dict, keep_threshold) -> tuple:
    """Resolve the threshold to apply. Returns (threshold, was_auto_derived).

    Explicit ``keep_threshold`` (anything other than the ``_AUTO`` sentinel)
    is returned verbatim, never adjusted. Omitted (``_AUTO``) is derived as a
    pure function of ``tag_counts``: probe classification at threshold 2,
    measure the SHARE of total nuggets it would drop, and fall back to
    threshold 1 only if that share exceeds the cold-start cap. See module
    docstring's `keep_threshold` param section for the full rationale.
    """
    if keep_threshold is not _AUTO:
        return keep_threshold, False

    total_nuggets = sum(int(v) for v in tag_counts.values())
    if total_nuggets == 0:
        return _COLD_START_FALLBACK_THRESHOLD, True

    probe_verdicts = _classify(tag_counts, _DEFAULT_KEEP_THRESHOLD)
    dropped_nuggets = sum(v["count"] for v in probe_verdicts if v["verdict"] == "drop")
    drop_share = dropped_nuggets / total_nuggets
    if drop_share > _AUTO_THRESHOLD_DROP_SHARE_CAP:
        return _COLD_START_FALLBACK_THRESHOLD, True
    return _DEFAULT_KEEP_THRESHOLD, True


def curate_clusters(
    tag_counts: dict,
    *,
    keep_threshold=_AUTO,
) -> dict:
    """Pure classification core. See module docstring for the full contract.

    Deterministic: identical (tag_counts, keep_threshold) always produces an
    identical verdicts list (sorted by raw tag) and counts summary — no
    wallclock, no randomness, no I/O. This holds even when keep_threshold is
    omitted: the auto-derivation is itself a pure function of tag_counts.
    """
    if not isinstance(tag_counts, dict) or not tag_counts:
        return _degraded_reply()

    threshold, threshold_auto = _resolve_keep_threshold(tag_counts, keep_threshold)
    verdicts = _classify(tag_counts, threshold)

    verdicts.sort(key=lambda v: v["tag"])
    drop_by_cause = {"placeholder": 0, "bare-no-sibling": 0, "below-threshold": 0}
    for v in verdicts:
        if v["verdict"] == "drop":
            drop_by_cause[v["drop_cause"]] += 1
    counts = {
        "total": len(verdicts),
        "keep": sum(1 for v in verdicts if v["verdict"] == "keep"),
        "normalize": sum(1 for v in verdicts if v["verdict"] == "normalize"),
        "merge": sum(1 for v in verdicts if v["verdict"] == "merge"),
        "drop": sum(1 for v in verdicts if v["verdict"] == "drop"),
        "drop_by_cause": drop_by_cause,
    }

    total_nuggets = sum(int(c) for c in tag_counts.values())
    dropped_nuggets = sum(v["count"] for v in verdicts if v["verdict"] == "drop")
    nugget_drop_share = round(dropped_nuggets / total_nuggets, 4) if total_nuggets else 0.0

    return {
        "verdicts": verdicts,
        "counts": counts,
        "threshold_applied": threshold,
        "threshold_auto": threshold_auto,
        "nugget_drop_share": nugget_drop_share,
        "degraded": False,
    }


@register_op("distill.curate_clusters")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "distill.curate_clusters" handler.

    Scope "none" (see coordinator_core/op_scopes.py): reads no repo-specific
    state — tag_counts arrives entirely as a wire param, repo_root is unused.

    Params:
        tag_counts (dict[str, int], REQUIRED).
        keep_threshold (int, optional) — omitted or invalid (non-int, `bool`
            — a `bool` is rejected even though it is an `int` subclass in
            Python, since a wire caller sending JSON `true`/`false` made a
            type error, not a deliberate threshold choice — or < 1) reaches
            the corpus-aware auto-derivation path; an explicit valid int is
            used verbatim, never auto-adjusted.

    Returns the curate_clusters(...) outcome dict verbatim (see module
    docstring). `degraded: True` with all-empty verdicts iff tag_counts is
    missing, not a dict, or empty.
    """
    tag_counts = params.get("tag_counts")
    keep_threshold = params.get("keep_threshold", _AUTO)
    if keep_threshold is not _AUTO and (
        isinstance(keep_threshold, bool)
        or not isinstance(keep_threshold, int)
        or keep_threshold < 1
    ):
        keep_threshold = _AUTO
    return curate_clusters(tag_counts, keep_threshold=keep_threshold)
