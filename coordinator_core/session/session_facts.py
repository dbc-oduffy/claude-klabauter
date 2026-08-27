"""
coordinator_core.session.session_facts — the session fact facade.

Roadmap `fact-layer-core`, stub `fl-core-01` chunk C2
(docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md). Builds against
docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md, which is
BINDING here, not advisory — this module's return shape is DR-319's contract verbatim,
checked by this package's own contract test rather than by prose review.

SHAPE — a plain Python module, NOT a registered op (DR-319 § (a)). The engine is
warm-on-demand with cold spawn-per-call as the fallback (CLAUDE.md § What this repo
is); a registered op costs one engine invocation per fact read, a leaf-module import
costs zero. `fl-core-02` lifts four more facts onto whatever shape this decision picks,
multiplying the choice by five — a registered op multiplies a real per-call cost by
five before any measurement is taken, a leaf module multiplies zero by five. Same
precedent shape as `coordinator_core/composition_budget.py`: "dependency-free leaf"
there means free of the ENGINE (no dispatch, no op registration, no subprocess to check
a budget) — it does not mean this module may not import the producer whose fact it
fronts, which is the entire point of a facade. This module imports intra-repo
producers only (`branch_resolution.session_commit_count_attributed`,
`branch_resolution._read_session_shape`) and nothing from `coordinator_core.ipc` or
the op registry.

SERVES ALL FIVE `fl-core-02` FACTS (C2, C3, C4, C5, C7b): the attributed session commit
magnitude (`session_magnitude_attributed`), the session pickup kind
(`session_pickup_kind`), the governing plan (`session_governing_plan`) — the ONLY fact
whose value carries a free pass-through string (`scope_mode`), verbatim, per DR-323's
C7b body — the session diff brightline (`session_diff_brightline`), the terminal sizing
scan (`session_terminal_sizings`) — the collision reference implementation, DR-323
§ (b) — and the fold-execution-record sidecar scan (`session_fold_sidecars`).

RETURN-SHAPE CONTRACT (DR-319, binding — verbatim, not summarized):
    Discriminator key `"degraded"` (bool), present on every returned record.

    Computed (`degraded: False`):
        {"degraded": False, "value": <fact payload>, "source": "<producer string>",
         "collision": <bool | None>}
    `collision` is ALWAYS present on a computed record — `None` when the fact has no
    peer-mutable surface, a bool when it does. Never omitted, never coerced to `False`
    (R-11): an omitted key declares nothing, which is the absent-vs-clean conflation
    R-11 forbids; `None` is the fact's own declaration that no collision mode exists.

    Degraded (`degraded: True`):
        {"degraded": True, "evidence": "<free prose>", "source": "<producer string>"}
    No `"value"` key on a degraded record — a caller reading `.get("value")` on a
    degraded record gets `None` by absence, never a fabricated zero.

    NO verdict/recommendation/disposition/action key anywhere (AC8, DR-306's
    detect/decide split) — this facade emits evidence and collision state, never a
    decision replacing an EM judgment.

WHY `collision: None` FOR THIS FACT (R-11's peer-mutability test, not fact identity):
`session_commit_count_attributed` scopes strictly to commits carrying THIS session's own
`Session-Id: <sid>` trailer. A peer session cannot write a commit under a foreign sid —
sid assignment is per-session — so no peer mutation can change what this fact reports
for a given sid, even though the underlying `git log` surface (the whole branch history)
is itself shared. This is R-11's Fact-1 precedent (session-scoped, single-writer): the
fact has no peer-mutable surface, so it declares `collision: None` rather than an
always-`False` field that would read as "checked, clean."

KNOWN ATTRIBUTION LIMIT — DECLARED, NOT SILENTLY INHERITED (the Staff Engineer F5, R-11): the
backing producer's `--grep=Session-Id: <sid>` only sees commits carrying the trailer.
A commit made via plumbing (`git commit-tree`, bypassing the porcelain `commit` path)
loses the trailer, so a computed `value: 0` is reachable BOTH for a session that made
no commits AND for a session that committed real work entirely through plumbing. This
facade cannot recover that distinction — no signal exists to tell the two cases apart —
so it is declared here, at the served fact's own seam, rather than left implicit.  A
caller that needs to rule this out must independently check for un-trailered commits;
this fact's `value: 0` does not do so on its own.

ANTI-SCOPE (this chunk only, not a standing rule): does not build a second session
store (`coordinator_core/session/shape.py` already is one; this module reads through
existing producers, never a store of its own — AC10). Does not converge the 21
hand-rolled dirty-probe implementations (a sibling roadmap-seed's scope, not this
one's). Does not edit `quick_wrap_assemble/__init__.py` — DR-323 § (a)'s
coexistence-then-cut discipline makes C7 the only chunk that cuts the interim readers
over; this module's facts are served ALONGSIDE them until then (C7b's `session_
governing_plan` included — its sibling reader `_read_governing_plan` stays live in
`quick_wrap_assemble` until C7's wave).

Spec backlink: docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md § C2
Contract:      docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md
Authoritative: state/roadmap/fact-layer-core/COORDINATOR-RESOLUTIONS.md (R-09, R-10, R-11)
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.ops.ceremony.branch_resolution import (
    _BRIGHTLINE_COMMITS,
    _BRIGHTLINE_LOC,
    _BRIGHTLINE_SURFACES,
    SCOPING_METHOD_AMBIGUOUS,
    _git_run,
    _read_session_shape,
    _session_surface_count,
    _session_touched_paths,
    analyze_session_scoping,
    session_commit_count_attributed,
)
from coordinator_core.telemetry.op_latency import record_fact_span

#: Instruments the FACT BOUNDARY (C1, plan
#: 2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path): every one of
#: the six facts this facade serves is wrapped so it emits one `op_latency`
#: `"fact_span"` row per call — see `record_fact_span`'s own docstring for
#: why this row kind exists and why it is ONE ROW PER CALL rather than
#: buffered per `brief()` invocation (that buffering needs a flush hook at
#: the call site, and `quick_wrap_assemble/__init__.py` is out of this
#: chunk's `writes:` scope — module docstring's ANTI-SCOPE already forbids
#: editing that file). This wrapper only measures and records; it never
#: changes what the wrapped fact returns (module docstring's "zero `raise`
#: statements by design" is preserved, since `record_fact_span` itself never
#: raises).
#:
#: THE AXIS NAMES MATTER HERE, and the first draft got them wrong.
#: `time.perf_counter` is WALL CLOCK, not process time. The plan's Method
#: section says "`perf_counter` around each fact call, reported in process
#: time" — those are two different clocks, and DR-344 closes with "no
#: wall-clock budget re-entering under a different name", so the conflation is
#: exactly the failure that DR is written against. What this wrapper emits as
#: `elapsed_ms` is therefore recorded as what it is: the C0-ruled SECONDARY,
#: load-dependent wall-clock leg, context only, never an adjudication axis.
#: `process_ms` carries `time.process_time()` — this process's own CPU,
#: load-independent — beside it.
#:
#: `process_ms` UNDER-REPORTS a spawn-dominated fact and must never be read as
#: that fact's true cost: `time.process_time()` excludes child processes, and
#: on Windows (first-class here) `os.times()` reports zero children CPU, so a
#: fact whose cost is mostly `git` subprocesses shows near-zero `process_ms`
#: while costing hundreds of ms. That gap is precisely why the STRUCTURAL leg
#: (spawn count and file-read count, `benchmarks/fact_layer_hot_path.py`) is
#: the primary, load-independent axis and this timing pair is secondary.
def _timed_fact(fact_name: str, worktree_root: Path, sid: Any, fn, *args) -> dict:
    t0 = time.perf_counter()
    c0 = time.process_time()
    t_start = time.time()
    record = fn(*args)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    process_ms = (time.process_time() - c0) * 1000.0
    outcome = "degraded" if record.get("degraded") else "computed"
    record_fact_span(
        fact=f"session_facts.{fact_name}",
        t_start=t_start,
        elapsed_ms=elapsed_ms,
        process_ms=process_ms,
        outcome=outcome,
        repo_root=worktree_root,
        sid=sid if isinstance(sid, str) else None,
    )
    return record

#: Literal, grep-able backing-source string (DR-319 § (b)) — a reader can grep this
#: straight back to the producing function, not a category token.
_SOURCE_SESSION_MAGNITUDE_ATTRIBUTED = (
    "coordinator_core/ops/ceremony/branch_resolution.py::session_commit_count_attributed"
)

#: Same convention, for Fact 1's producer (DR-319 § (b), DR-323 body).
_SOURCE_SESSION_PICKUP_KIND = (
    "coordinator_core/ops/ceremony/branch_resolution.py::_read_session_shape"
)

#: Same convention, for Fact 4's producer. Points at THIS module's own served function
#: (not `quick_wrap_assemble._read_terminal_sizings`, which C7 deletes) — the scan logic
#: is moved wholesale below, so the grep-able producer is this facade itself.
_SOURCE_SESSION_TERMINAL_SIZINGS = (
    "coordinator_core/session/session_facts.py::session_terminal_sizings"
)

#: Same convention, for Fact 3's producer. Points at `session_commit_count_attributed`
#: specifically — DR-323 § (b)'s table names this fact's backing source as "trailer-
#: attributed to this sid (`git log --grep=Session-Id: <sid>`)", which is exactly that
#: function's own call, and it is the ONE sub-read whose degraded state this composed
#: fact propagates verbatim (module docstring below, "WHY `session_diff_brightline`
#: DEGRADES"). The other sub-reads composed into this fact (`_novel_loc_split`'s own
#: `git log --numstat`, `_session_touched_paths`, `analyze_session_scoping`) are
#: reported under the same source string rather than a second field — DR-319 § (b)
#: declares one backing-source string per served fact, not one per sub-read.
_SOURCE_SESSION_DIFF_BRIGHTLINE = (
    "coordinator_core/ops/ceremony/branch_resolution.py::session_commit_count_attributed"
)

#: Local re-declarations of `quick_wrap_assemble`'s doc-only carve-out: the facade
#: must not depend on `quick_wrap_assemble` — that dependency runs the other
#: direction post-C7 (DR-323 § (a), coexistence-then-cut). `_novel_loc_split` and the surface
#: carve-out both need them, and the facade must not import from `quick_wrap_assemble`.
_DOC_ONLY_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".rst", ".txt"})
_DOC_ONLY_PREFIXES: tuple[str, ...] = (
    "docs/",
    "state/",
    "archive/",
    "cross-repo/",
    "tasks/",
)

#: Local re-declaration of `quick_wrap_assemble`'s Fact-4 constant, for the same
#: dependency-direction reason as the doc-only carve-out above.
_TERMINAL_SIZING_STATUSES: frozenset[str] = frozenset({"shipped", "declined", "superseded"})


def _read_frontmatter_kind(path: Path) -> str | None:
    """Read the `kind:` frontmatter scalar off `path`, tolerant of a malformed body.

    Delegates to `frontmatter.primitives.read_fm_field_unquoted` for the same
    reason `_read_frontmatter_scope_mode` does: the local `\\s*$`-anchored regex
    it replaced did not tolerate a trailing `# comment`, so `kind: session-handoff
    # continuation` read as None and the fact fell back to a default
    classification rather than degrading. A misread that silently picks a
    plausible value is invisible to DR-319's contract test, which can only see
    the record's shape.

    Negative-spec: do NOT re-introduce a local regex here or on
    `_read_frontmatter_status` below. Both were fixed together with
    `_read_frontmatter_scope_mode` precisely because they are one defect class.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head = text.split("\n---", 2)[0] if text.startswith("---") else text[:4096]
    return read_fm_field_unquoted(head, "kind")


def _read_frontmatter_status(path: Path) -> str | None:
    """Read the `status:` frontmatter scalar off `path`, tolerant of a malformed body.

    Same rationale and same negative-spec as `_read_frontmatter_kind` above. The
    failure this closes is narrower than it looks: a sizing whose `status:` line
    carries a trailing comment read as None, so a genuinely terminal record was
    excluded from the terminal scan and the fact reported a clean, smaller scan
    rather than degrading.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head = text.split("\n---", 2)[0] if text.startswith("---") else text[:4096]
    return read_fm_field_unquoted(head, "status")


def session_magnitude_attributed(worktree_root: Path, sid: str) -> dict:
    """Serve the attributed session commit magnitude: the count of commits on
    `worktree_root`'s branch carrying a `Session-Id: <sid>` trailer for THIS session,
    via `git log --grep=Session-Id: <sid>` — never `git rev-list --count
    head_at_start..HEAD` (AC4's trap: the positional count credits a session with its
    peers' commits on a shared branch, per R-09).

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <int>, "source": <str>, "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `collision` is always `None` for this fact — see module docstring, "WHY
    `collision: None` FOR THIS FACT".

    KNOWN LIMIT: `value: 0` is reachable both for a genuinely zero-commit session and
    for a session that committed only via plumbing (loses the `Session-Id:` trailer) —
    see module docstring, "KNOWN ATTRIBUTION LIMIT". This facade does not (cannot)
    disambiguate the two; it serves the trailer-attributed count, declared as such.

    TIMED (C1): emits one `op_latency` `"fact_span"` row per call — see
    `_timed_fact`'s own docstring above.
    """
    return _timed_fact(
        "session_magnitude_attributed", worktree_root, sid,
        _session_magnitude_attributed_impl, worktree_root, sid,
    )


def _session_magnitude_attributed_impl(worktree_root: Path, sid: str) -> dict:
    """Untimed body of `session_magnitude_attributed` — see that function's
    docstring for the full contract; this exists only so `_timed_fact` can
    wrap the call without altering what is returned."""
    record = session_commit_count_attributed(worktree_root, sid)
    if record["degraded"]:
        return {
            "degraded": True,
            "evidence": record["evidence"],
            "source": _SOURCE_SESSION_MAGNITUDE_ATTRIBUTED,
        }
    return {
        "degraded": False,
        "value": record["value"],
        "source": _SOURCE_SESSION_MAGNITUDE_ATTRIBUTED,
        "collision": None,
    }


def session_pickup_kind(worktree_root: Path, common_dir: Path, sid: str) -> dict[str, Any]:
    """Serve the picked-up artifact's classification for THIS session
    (`fl-core-02` Fact 1; DR-323's lift of `quick_wrap_assemble._read_pickup_kind`).

    `value["classification"]` is one of `handoff` / `spinoff` / `memo` / `none`,
    resolved from the artifact's own `kind:` frontmatter when the picked-up file is
    still readable, and from the session-shape record alone when it is not (a handoff
    archived mid-session still classifies, it does not fall to `none`).

    AC6 — THE MIXED HANDOFF-PLUS-MEMO CASE, AND ITS PRECEDENCE STATED HERE: a session
    can both consume a handoff AND action memos in the same sitting. This is not data
    loss — `value["actioned_memos"]` always carries the full list regardless of which
    way `classification` resolves. `classification` itself stays single-valued and
    PICKS THE HANDOFF OVER THE MEMOS whenever a handoff pickup happened; it reports
    `"memo"` only on the branch where no handoff pickup happened AND at least one memo
    was actioned. A caller that needs the memo axis when a handoff also happened reads
    `value["actioned_memos"]` directly — it is never dropped to make room for the
    single-valued field.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `value`'s keys, on a computed record: `classification`, `artifact_path`,
    `basename`, `deliverable_id`, `actioned_memos`, `consumed_predecessor` — the same
    fields `_read_pickup_kind` already computed; only the posture and the enclosing
    shape changed here, not the payload.

    `collision` is always `None` for this fact (DR-323 § (b) table): session-shape.json
    is session-scoped and single-writer, the same no-peer-surface reasoning R-11
    applies to `session_magnitude_attributed` above — no peer session can mutate
    another session's own pickup record.

    POSTURE CONVERSION (AC3): `_read_session_shape` itself never raises — it catches
    `json.JSONDecodeError`/`OSError` internally and returns `({}, "absent")`, which is
    ALSO what a session that has never had a session-shape.json written at all returns.
    Those two are not the same fact: one is "nothing to read yet" (a legitimately
    computed `classification: "none"`), the other is "a file exists and could not be
    parsed" (a genuine probe failure). `_read_session_shape` collapses that distinction
    before it reaches this facade, so this function recovers it the only way available
    without editing that producer: checking whether the backing path exists. Source
    `"absent"` with the path PRESENT on disk means the read failed after the file was
    written — degraded, with evidence naming the producer, the call, and the file path.
    Source `"absent"` with the path MISSING is the ordinary "no session-shape.json yet"
    case — computed, `classification: "none"`, same as today.

    TIMED (C1): emits one `op_latency` `"fact_span"` row per call — see
    `_timed_fact`'s own docstring above.
    """
    return _timed_fact(
        "session_pickup_kind", worktree_root, sid,
        _session_pickup_kind_impl, worktree_root, common_dir, sid,
    )


def _session_pickup_kind_impl(worktree_root: Path, common_dir: Path, sid: str) -> dict[str, Any]:
    """Untimed body of `session_pickup_kind` — see that function's docstring
    for the full contract; this exists only so `_timed_fact` can wrap the
    call without altering what is returned."""
    shape_path = common_dir / "coordinator-sessions" / sid / "session-shape.json"
    shape, source = _read_session_shape(common_dir, sid)

    if source == "absent" and shape_path.exists():
        return {
            "degraded": True,
            "evidence": (
                "branch_resolution.py::_read_session_shape(common_dir, sid) returned "
                f"source=\"absent\" for {shape_path}, which exists on disk — its own "
                "try/except around json.load caught a JSONDecodeError or OSError and "
                "logged a warning rather than propagating it, so the file could not be "
                "parsed even though it is present."
            ),
            "source": _SOURCE_SESSION_PICKUP_KIND,
        }

    pickup = shape.get("pickup") or {}
    history = shape.get("pickup_history") or []
    actioned_memos = shape.get("actioned_memos") or []

    basename = (pickup.get("handoff") or "").strip()
    happened = bool(pickup.get("happened")) and bool(basename)

    if not happened and history:
        for entry in reversed(history):
            if entry.get("happened") and entry.get("handoff"):
                basename = str(entry["handoff"]).strip()
                happened = True
                break

    if not happened:
        value = {
            "classification": "memo" if actioned_memos else "none",
            "artifact_path": None,
            "basename": None,
            "deliverable_id": None,
            "actioned_memos": [dict(m) for m in actioned_memos],
            "consumed_predecessor": bool(actioned_memos),
        }
    else:
        artifact_path = None
        kind = None
        for directory in ("state/handoffs", "archive/handoffs"):
            candidate = worktree_root / directory / basename
            if candidate.exists():
                artifact_path = f"{directory}/{basename}"
                kind = _read_frontmatter_kind(candidate)
                break

        value = {
            "classification": (kind or "handoff").strip().lower(),
            "artifact_path": artifact_path,
            "basename": basename,
            "deliverable_id": pickup.get("deliverable_id"),
            "actioned_memos": [dict(m) for m in actioned_memos],
            "consumed_predecessor": True,
        }

    return {
        "degraded": False,
        "value": value,
        "source": _SOURCE_SESSION_PICKUP_KIND,
        "collision": None,
    }


# ---------------------------------------------------------------------------
# session_governing_plan (fl-core-02 C7b) — lift of
# quick_wrap_assemble._read_governing_plan. The lift only — no vocabulary
# question in it (DR-323 body, C7b): the served `scope_mode` is a verbatim
# pass-through of the plan frontmatter's own free string, per § Problem's
# Fact 2 reader table (one producer, five pass-through readers, none of
# which translate/normalize/coerce the value) — this facade is a sixth
# pass-through reader, not the point where normalization is invented.
# ---------------------------------------------------------------------------

#: Same convention, for Fact 2's producer (DR-319 § (b), DR-323 § (b) table).
_SOURCE_SESSION_GOVERNING_PLAN = (
    "coordinator_core/session/claimed_plan.py::list_held_plan_claims"
)

def _read_frontmatter_scope_mode(path: Path) -> str | None:
    """Read the `scope_mode:` frontmatter scalar off `path` VERBATIM — no enum,
    no canonical spelling, no default substitution (AC11).

    Delegates to `frontmatter.primitives.read_fm_field_unquoted`, the canonical
    reader `coverage.py :: _resolve_plan_scope_mode` and the plan emitter both
    already use. C7b originally re-declared `quick_wrap_assemble`'s
    `_SCOPE_MODE_RE` here on the reasoning that an identical character class
    could not introduce normalization. That held for values the regex parses,
    but not for its failure mode: `scope_mode: spec-dispatch  # routed` is
    anchored out by the trailing ``\\s*$`` and returns None, so a declared value
    silently reads as absent — the fail-open posture this plan exists to
    retire, and an AC11 verbatim regression the moment any consumer converges
    onto this facade rather than onto the canonical reader.

    Negative-spec: do NOT re-introduce a local regex to avoid the import. The
    dependency runs to `frontmatter.primitives`, a leaf with no subprocess and
    no engine dispatch, so DR-319 § (a)'s dependency-free-leaf constraint is
    unaffected — that constraint forbids an op registration or a spawn per
    fact read, not importing the producer a facade fronts.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head = text.split("\n---", 2)[0] if text.startswith("---") else text[:4096]
    return read_fm_field_unquoted(head, "scope_mode")


def session_governing_plan(worktree_root: Path, common_dir: Path, sid: str) -> dict[str, Any]:
    """Serve the plan THIS session holds a claim on (`fl-core-02` Fact 2; DR-323's
    lift of `quick_wrap_assemble._read_governing_plan`) — the lift only, no
    vocabulary question in it (DR-323 body, C7b).

    `common_dir` and `sid` are accepted for call-shape parity with the lifted
    reader and the facade's other three-argument facts, but unused in the
    body below — `list_held_plan_claims` re-resolves the session id and the
    git-common-dir from `worktree_root` itself (its own contract, module
    docstring), the same as the interim reader it replaces.

    DELEGATES THE CLAIM READ TO THE SHIPPED RESOLVER
    (`coordinator_core.session.claimed_plan.list_held_plan_claims`), exactly as
    `_read_governing_plan` already does — never hand-rolled. That function's own
    docstring records why: the first version of this read looked for a
    `holder-session-id` file while the writer emits `session_id`, so it matched
    nothing and reported `present: false` for EVERY session, silently passing
    entry-test condition 1 for a plan-driven close that owes
    `/workstream-complete` (`state/lessons/2026-08-18-a-static-reviewer-shares-
    your-premise-ab-292f0a09875e.yaml`). Session-scoping is the resolver's own
    contract: it resolves this session's id and returns only claims THIS
    session holds, so a dozen concurrent peers' claims on the same shared
    worktree are excluded without this facade re-deriving the rule.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE
    CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": <bool>}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `value`'s keys, on a computed record: `present` (bool), `path`
    (repo-relative str | None), `status`, `scope_mode`, `slug`, `claims_held`
    (int, present only when `present` is True) — the same fields
    `_read_governing_plan` already computed; only the posture and the
    enclosing shape changed here, not the payload. `present` stays true when
    the claim exists but the plan file itself is gone — a missing file is not
    an absent plan, and treating it as one would flip entry-test condition 1
    the wrong way (same rule the lifted reader already carried).

    VERBATIM PASS-THROUGH (AC11, a property of THIS chunk too, not only C8's):
    `scope_mode` is whatever string the plan frontmatter's `scope_mode:` scalar
    carries, read by `_read_frontmatter_scope_mode` above — no normalization,
    no enum, no canonical spelling, no default substitution. § Problem's Fact 2
    reader table names five other pass-through readers of this same frontmatter
    field (`coverage.py`, `pipeline_context.py`, `ops/emit/sections/plans.py`,
    the frozen `PlanSummary.scope_mode` cross-repo contract, and
    `plan_assemble/predicates/substrate_seven_dim.py`) — none of them translate
    the value either. Introducing normalization HERE would break every one of
    them by changing an emitted VALUE, not a field name, which is the cross-
    plane hazard this chunk owns alone (no answer from DoE-claude bears on it,
    per § Problem's F1 correction).

    POSTURE CONVERSION (AC3, the whole point of this chunk): the OLD bare
    `except Exception` around the resolver import/call returned the `absent`
    literal (`present: False`), conflating "this session holds no plan" with
    "the resolver blew up" — in the dangerous direction, silently passing
    entry-test condition 1 for a plan-driven close that owes
    `/workstream-complete`. `list_held_plan_claims`'s OWN contract is
    never-raises (its module docstring's negative-spec: "Do NOT raise when no
    plan is claimed... Every failure edge... falls through to returning...
    `[]`... never an exception"), so a caught exception here can only be an
    import/environment failure (e.g. the module itself fails to import in a
    degraded install) — not an ordinary "no plan claimed" result, which
    `list_held_plan_claims` already reports as `[]`, not as a raise. This
    function therefore degrades on that caught exception rather than folding
    it into the ordinary empty-claims branch, with evidence naming the
    producer, the call, and what it raised.

    COLLISION SHAPE (DR-323 § (b) table): `collision` is a real `bool` for this
    fact (never `None`) — the claim store (`plan-claims/`) and the plan's own
    frontmatter are both surfaces a peer session can concurrently mutate
    (hold/release a claim; edit the plan file), unlike Facts 1/3's
    single-writer/sid-scoped surfaces. This function reuses the SAME mechanism
    Facts 4 and 5 already established for detecting a live peer edit —
    `_dirty_paths`'s `git status --porcelain` read — and asks whether the
    RESOLVED plan path itself is currently uncommitted: `collision: True` means
    the plan file this session holds is mid-edit by a peer session right now;
    `False` means it is clean. No plan held, or a claim with no resolvable file
    on disk, short-circuits straight to `collision: False` without calling
    `_dirty_paths` at all — same "nothing found, nothing to fold over"
    shortcut `session_terminal_sizings`/`session_fold_sidecars` take for an
    empty scan. This is a fresh `_dirty_paths` read, not shared with the other
    facts' own calls — module precedent (`session_fold_sidecars`'s docstring):
    each served fact owns its own sub-reads.

    `_dirty_paths`'s own `git status` failure propagates as a degraded FACT
    (same "propagate the sub-read's degraded state" discipline
    `session_diff_brightline`/`session_terminal_sizings`/`session_fold_sidecars`
    already carry) — a scan that cannot tell dirty from clean has no basis to
    report a collision state on the plan it did find.

    TIMED (C1): emits one `op_latency` `"fact_span"` row per call — see
    `_timed_fact`'s own docstring above.
    """
    return _timed_fact(
        "session_governing_plan", worktree_root, sid,
        _session_governing_plan_impl, worktree_root, common_dir, sid,
    )


def _session_governing_plan_impl(
    worktree_root: Path, common_dir: Path, sid: str
) -> dict[str, Any]:
    """Untimed body of `session_governing_plan` — see that function's
    docstring for the full contract; this exists only so `_timed_fact` can
    wrap the call without altering what is returned."""
    try:
        from coordinator_core.session.claimed_plan import list_held_plan_claims

        held = list_held_plan_claims(str(worktree_root))
    except Exception as exc:
        return {
            "degraded": True,
            "evidence": (
                "coordinator_core/session/claimed_plan.py::list_held_plan_claims"
                f"({str(worktree_root)!r}) raised {exc!r} — that resolver's own "
                "contract is never-raises (module docstring negative-spec), so "
                "this is an import/environment failure, not an ordinary "
                "'no plan claimed' result."
            ),
            "source": _SOURCE_SESSION_GOVERNING_PLAN,
        }

    if not held:
        return {
            "degraded": False,
            "value": {
                "present": False,
                "path": None,
                "status": None,
                "scope_mode": None,
                "slug": None,
            },
            "source": _SOURCE_SESSION_GOVERNING_PLAN,
            "collision": False,
        }

    claim_path = held[0][0]
    slug = Path(claim_path.replace("\\", "/")).stem

    plan_path = None
    status = None
    scope_mode = None
    candidates = [worktree_root / claim_path]
    candidates += [worktree_root / d / f"{slug}.md" for d in ("docs/plans", "archive/specs")]
    for candidate in candidates:
        if candidate.exists():
            plan_path = candidate.relative_to(worktree_root).as_posix()
            status = _read_frontmatter_status(candidate)
            scope_mode = _read_frontmatter_scope_mode(candidate)
            break

    if plan_path is None:
        collision = False
    else:
        dirty_result = _dirty_paths(worktree_root)
        if dirty_result["degraded"]:
            return {
                "degraded": True,
                "evidence": dirty_result["evidence"],
                "source": _SOURCE_SESSION_GOVERNING_PLAN,
            }
        collision = plan_path in dirty_result["paths"]

    return {
        "degraded": False,
        "value": {
            "present": True,
            "path": plan_path,
            "status": status,
            "scope_mode": scope_mode,
            "slug": slug,
            "claims_held": len(held),
        },
        "source": _SOURCE_SESSION_GOVERNING_PLAN,
        "collision": collision,
    }


# ---------------------------------------------------------------------------
# session_diff_brightline (fl-core-02 C3) — lift of
# quick_wrap_assemble._read_diff / _novel_loc_split.
# ---------------------------------------------------------------------------


def _resolve_rename_path(path: str) -> str:
    """Reduce a `--numstat -M` path field to the file's CURRENT path.

    Moved wholesale from `quick_wrap_assemble/__init__.py :: _resolve_rename_path`
    (fl-core-02 C3) — logic unchanged, see that module's git history for the rename-
    parsing rationale (`old => new` and the `pre/{old_mid => new_mid}/file.ext`
    compressed form both resolve to the NEW path before any suffix/prefix rule runs).
    """
    text = path.strip()
    if "=>" not in text:
        return text
    if "{" in text and "}" in text:
        head, _, rest = text.partition("{")
        middle, _, tail = rest.partition("}")
        _old, _, new_mid = middle.partition("=>")
        combined = f"{head}{new_mid.strip()}{tail}"
        # `{a => b}` with an empty side collapses to a doubled separator.
        return combined.replace("//", "/").strip()
    _old, _, new = text.partition("=>")
    return new.strip()


def _classify_path(path: str) -> str:
    """`code` or `doc`. Doc-only paths are carved out of the novel LOC/surface counts.

    Moved wholesale from `quick_wrap_assemble/__init__.py :: _classify_path`
    (fl-core-02 C3) — logic unchanged.
    """
    normalized = _resolve_rename_path(path).replace("\\", "/").strip()
    if not normalized:
        return "code"
    if Path(normalized).suffix.lower() in _DOC_ONLY_SUFFIXES:
        return "doc"
    if normalized.startswith(_DOC_ONLY_PREFIXES):
        return "doc"
    return "code"


def _novel_loc_split(worktree_root: Path, sid: str) -> dict[str, Any]:
    """Split this session's changes into gross / doc-only / relocated / novel, for both
    lines AND commits. One `git log --numstat -M` pass, not three.

    Moved wholesale from `quick_wrap_assemble/__init__.py :: _novel_loc_split`
    (fl-core-02 C3) — the parsing logic (`-M`'s "moved unmodified" carve-out,
    `novel_loc = gross_loc - doc_only_loc` floored at 0, `novel_commit_count` applying
    the same doc-only carve-out to the commit axis) is unchanged; only the git-failure
    posture changed, per this chunk's job.

    NOT ITSELF A DR-319 RECORD — a private sub-read `session_diff_brightline` folds
    into its own DR-319 shape. Uses `branch_resolution._git_run` (returncode-checked),
    never `quick_wrap_assemble._git_out` (swallows a git failure into `""`
    indistinguishable from a genuinely-empty diff — the exact fail-open posture this
    lift retires): `{"degraded": False, **counts}` on success,
    `{"degraded": True, "evidence": <str>}` when the underlying `git log` call fails.
    """
    result = _git_run(
        [
            "log",
            "--numstat",
            "-M",
            "--format=@@QWA-COMMIT@@ %H",
            f"--grep=Session-Id: {sid}",
        ],
        worktree_root,
    )
    if result.returncode != 0:
        return {
            "degraded": True,
            "evidence": (
                f"git log --numstat -M --grep=Session-Id: {sid} failed: returncode="
                f"{result.returncode!r} stderr={result.stderr.strip()!r}"
            ),
        }

    marker = "@@QWA-COMMIT@@ "
    gross = 0
    doc_only = 0
    relocated = 0
    novel_commits: set[str] = set()
    current_sha = ""
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            current_sha = line[len(marker) :].strip()
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, deleted_raw, path = parts[0], parts[1], parts[2]
        if added_raw == "-" or deleted_raw == "-":
            continue
        try:
            changed = int(added_raw) + int(deleted_raw)
        except ValueError:
            continue
        gross += changed
        if changed == 0:
            relocated += 1
            continue
        if _classify_path(path) == "doc":
            doc_only += changed
        elif current_sha:
            novel_commits.add(current_sha)
    novel = max(0, gross - doc_only)
    return {
        "degraded": False,
        "gross_loc": gross,
        "doc_only_loc": doc_only,
        "relocated_files": relocated,
        "novel_loc": novel,
        "novel_commit_count": len(novel_commits),
    }


def session_diff_brightline(worktree_root: Path, common_dir: Path, sid: str) -> dict[str, Any]:
    """Serve the session-scoped diff brightline (`fl-core-02` Fact 3; DR-323's lift of
    `quick_wrap_assemble._read_diff` / `_novel_loc_split`).

    Composes `analyze_session_scoping`'s `ScopingVerdict`, `_session_touched_paths`,
    `_session_surface_count`, `session_commit_count_attributed`, and this module's own
    `_novel_loc_split` (the one genuinely new piece, moved wholesale above).
    `_BRIGHTLINE_LOC`/`_BRIGHTLINE_COMMITS`/`_BRIGHTLINE_SURFACES` stay imported from
    `branch_resolution`, never restated, for the same reason `quick_wrap_assemble`'s own
    import comment states: so the review-partition verdict and this brightline cannot
    drift apart silently.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    WHY THIS FACT DEGRADES, AND ON WHAT (the fail-open this chunk retires): the OLD
    `_read_diff` called `session_commit_count_attributed` — already converted to
    DR-319's degraded-with-evidence shape by `session_magnitude_attributed`'s own
    producer — and then discarded the distinction: `commit_count = record["value"] if
    not record["degraded"] else 0`. A degraded probe and a genuinely zero-commit
    session both read as `commit_count: 0` at every downstream call site. This function
    propagates `session_commit_count_attributed`'s degraded state as a degraded FACT
    instead: if that sub-read is degraded, `session_diff_brightline` is degraded,
    full stop, before any of the other sub-reads run. The other git-backed sub-read
    this function OWNS — `_novel_loc_split`'s `git log --numstat` call — gets the same
    treatment for the same reason (see that function's own docstring): a fact that
    cannot compute its novel-LOC/commit split has no basis to report a brightline
    verdict either.

    `_session_touched_paths` and `analyze_session_scoping`'s internal `git log` calls
    are NOT converted here — both are existing producers with their own fail-open
    posture (`_session_touched_paths` returns `[]` on any git failure;
    `analyze_session_scoping`'s pipeline never raises, by its own docstring's design)
    that this chunk does not own and does not silently inherit past acknowledging it:
    a touched-paths read that failed reports the same `[]` as a session that touched
    nothing, so `surface_count`/`novel_surface_count`/`touched_paths` in a computed
    record carry this same known limit forward, undisturbed by this lift. Converging
    those producers' own posture is out of this chunk's scope (DR-323 names C3 as the
    diff-brightline lift, not a `_session_touched_paths` rewrite).

    THREE INDEPENDENT AXES TRAVEL ON THIS FACT — KEPT DISTINCT, STATED HERE SO THE
    NEXT READER DOES NOT REDISCOVER IT:
      - `degraded` (this docstring, above): could the probe run at all.
      - `collision` (DR-323 § (b) table, below): does the backing source have a
        peer-mutable surface. `None` for this fact.
      - `trustworthy` (`value["trustworthy"]`, DERIVED FROM `scoping_method`): is the
        `Session-Id` trailer a RELIABLE ATTRIBUTION SIGNAL for this sid, independent
        of whether the probe that read it succeeded. `scoping_method == "ambiguous-
        x-node"` means the trailer itself is not trustworthy — a foreign or non-
        contiguous commit history makes trailer-based scoping unsafe — which is NOT
        "the probe could not run" (that is `degraded`) and NOT "a peer can mutate this
        fact's source" (that is `collision`). Folding `trustworthy` into `degraded`
        would report an untrustworthy-but-successfully-computed read as a probe
        failure; folding it into `collision` would report a data-quality signal as a
        peer-contention signal. Both are the same class of conflation DR-319 forbids
        for `degraded`/`collision` themselves, recurring one axis over — a computed
        record with `trustworthy: false` is still `degraded: False`, still carries
        real (if lower-confidence) counts, and the caller reads `trustworthy` to
        decide how much weight to give them.

    `collision` is always `None` for this fact (DR-323 § (b) table): trailer
    attribution is scoped strictly to commits carrying THIS session's own
    `Session-Id: <sid>` trailer — a peer session cannot write a commit under a
    foreign sid, so no peer mutation can change what this fact reports for a given
    sid, even though the underlying `git log` surface (the whole shared branch
    history) is itself shared. Same no-peer-surface reasoning
    `session_magnitude_attributed` already carries for a sid-scoped read (module
    docstring, "WHY `collision: None` FOR THIS FACT").

    `value`'s keys, on a computed record: `scoping_method`, `trustworthy`,
    `sha_range`, `commit_count`, `surface_count`, `novel_surface_count`,
    `touched_paths`, `gross_loc`, `doc_only_loc`, `relocated_files`, `novel_loc`,
    `novel_commit_count`, `brightline` (the three threshold constants), `breached`
    (which brightline axes are at/over threshold), `under_brightline` — the same
    fields `_read_diff` already computed; only the posture and the enclosing shape
    changed here, not the payload. `breached`/`under_brightline` are a mechanical
    threshold comparison against fixed constants, not an EM judgment call — quick-
    wrap's own doctrine already computes this brightline (condition 2 of 4; only
    condition 4, "is the work finished," is genuine EM discretion) — so they are not
    AC8's forbidden verdict/recommendation/disposition/action shape.

    TIMED (C1): emits one `op_latency` `"fact_span"` row per call — see
    `_timed_fact`'s own docstring above.
    """
    return _timed_fact(
        "session_diff_brightline", worktree_root, sid,
        _session_diff_brightline_impl, worktree_root, common_dir, sid,
    )


def _session_diff_brightline_impl(
    worktree_root: Path, common_dir: Path, sid: str
) -> dict[str, Any]:
    """Untimed body of `session_diff_brightline` — see that function's
    docstring for the full contract; this exists only so `_timed_fact` can
    wrap the call without altering what is returned."""
    commit_count_record = session_commit_count_attributed(worktree_root, sid)
    if commit_count_record["degraded"]:
        return {
            "degraded": True,
            "evidence": commit_count_record["evidence"],
            "source": _SOURCE_SESSION_DIFF_BRIGHTLINE,
        }

    split = _novel_loc_split(worktree_root, sid)
    if split["degraded"]:
        return {
            "degraded": True,
            "evidence": split["evidence"],
            "source": _SOURCE_SESSION_DIFF_BRIGHTLINE,
        }
    split = {k: v for k, v in split.items() if k != "degraded"}

    verdict = analyze_session_scoping(worktree_root, common_dir, sid)
    touched = _session_touched_paths(worktree_root, sid)
    surface_count = _session_surface_count(touched)
    novel_touched = [p for p in touched if _classify_path(p) == "code"]
    novel_surface_count = _session_surface_count(novel_touched)

    over = {
        "novel_loc": split["novel_loc"] >= _BRIGHTLINE_LOC,
        "novel_commit_count": split["novel_commit_count"] >= _BRIGHTLINE_COMMITS,
        "novel_surface_count": novel_surface_count >= _BRIGHTLINE_SURFACES,
    }
    breached = sorted(k for k, v in over.items() if v)
    trustworthy = verdict.method != SCOPING_METHOD_AMBIGUOUS

    value = {
        "scoping_method": verdict.method,
        "trustworthy": trustworthy,
        "sha_range": verdict.candidate_range or None,
        "commit_count": commit_count_record["value"],
        "surface_count": surface_count,
        "novel_surface_count": novel_surface_count,
        "touched_paths": touched,
        **split,
        "brightline": {
            "novel_loc": _BRIGHTLINE_LOC,
            "novel_commit_count": _BRIGHTLINE_COMMITS,
            "novel_surface_count": _BRIGHTLINE_SURFACES,
        },
        "breached": breached,
        "under_brightline": not breached,
    }

    return {
        "degraded": False,
        "value": value,
        "source": _SOURCE_SESSION_DIFF_BRIGHTLINE,
        "collision": None,
    }


# ---------------------------------------------------------------------------
# session_terminal_sizings (fl-core-02 C4) — lift of
# quick_wrap_assemble._read_terminal_sizings / _dirty_paths. THE COLLISION
# REFERENCE IMPLEMENTATION (DR-323 § (b)): the only fact of the five carrying real
# collision state today, so how per-record `dirty` maps onto DR-319's record-level
# `collision: bool` is the pattern C5/C7b are read against.
# ---------------------------------------------------------------------------


def _dirty_paths(worktree_root: Path) -> dict[str, Any]:
    """Repo-relative forward-slash paths with uncommitted changes, from one
    `git status --porcelain` read.

    Moved from `quick_wrap_assemble/__init__.py :: _dirty_paths` (fl-core-02 C4) —
    the parsing logic (porcelain's 3-char status prefix, the `old -> new` rename
    arrow, forward-slashing a Windows path) is unchanged. The POSTURE changed: the
    old helper called `_git_out`, which swallows any git failure (nonzero exit,
    `OSError`, timeout) into `""` — indistinguishable from a genuinely clean
    worktree. This uses `branch_resolution._git_run` instead (the same seam C3's
    `_novel_loc_split` uses), returncode-checked, so a `git status` failure reports
    as degraded rather than as zero dirty paths.

    Returns `{"degraded": False, "paths": set[str]}` on success,
    `{"degraded": True, "evidence": <str>}` when the underlying `git status` call
    fails.
    """
    result = _git_run(["status", "--porcelain", "--untracked-files=all"], worktree_root)
    if result.returncode != 0:
        return {
            "degraded": True,
            "evidence": (
                "git status --porcelain --untracked-files=all failed: returncode="
                f"{result.returncode!r} stderr={result.stderr.strip()!r}"
            ),
        }
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        dirty.add(entry.strip('"').replace("\\", "/"))
    return {"degraded": False, "paths": dirty}


def session_terminal_sizings(worktree_root: Path) -> dict[str, Any]:
    """Serve the terminal sizing-object scan (`fl-core-02` Fact 4; DR-323's lift of
    `quick_wrap_assemble._read_terminal_sizings` / `_dirty_paths`) —
    **the collision reference implementation** (DR-323 § (b) table): the only fact of
    the five carrying real collision state today.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": <bool>}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `value`'s keys, on a computed record: `scanned` (int, every `*.yaml` under
    `state/sizings/`, terminal or not), `non_terminal_count` (int), `terminal`
    (list of dicts, one per record at `shipped`/`declined`/`superseded`) — the same
    fields `_read_terminal_sizings` already computed, MINUS `movable`. Each `terminal`
    entry carries `path`, `status`, `dirty` (bool), `reason` (str | None) — `movable`
    is DROPPED per DR-323 § (b): `movable: not is_dirty` is a pure restatement of
    `dirty` carrying zero additional information, and it encodes the EM's
    skip-vs-sweep call, squarely DR-319's Negative-spec on disposition/action keys
    (AC12). This function implements that decision; it is not re-scrutinized here.

    COLLISION SHAPE — THE DECISION THIS CHUNK MAKES, STATED HERE FOR THE FACTS THAT
    READ AGAINST IT: `collision` (record-level, DR-319's single required key) is the
    OR across every `terminal` entry's own `dirty` flag — `True` the moment ANY
    terminal-status sizing carries live uncommitted edits, `False` when none do.
    Chosen over folding `collision` away entirely and letting `value["terminal"][i]
    ["dirty"]` carry the whole signal, because DR-319's contract test asserts
    `"collision" in record` unconditionally on every COMPUTED record regardless of
    fact shape (C6, AC9) — a record-level bool that answers "is this fact's overall
    read live-contended right now" is the aggregate signal that assertion is written
    against, and it costs nothing: it is a fold over data this function already
    computes, not a second read. PER-RECORD GRANULARITY SURVIVES INTACT: the OR
    summarizes, it does not replace — every `terminal` entry keeps its own `dirty`
    bool in `value`, because the EM's skip-vs-sweep call is per record (C1's
    depends_on note), and an aggregate `collision: True` alone cannot tell the EM
    WHICH of N terminal sizings to leave alone. A caller doing the skip-vs-sweep
    judgment reads `value["terminal"][i]["dirty"]`, never the top-level `collision`,
    for that decision — `collision` is the fact-level signal DR-319's contract wants,
    `dirty` is the record-level signal the EM's judgment needs, and this shape keeps
    both live rather than collapsing one into the other.

    POSTURE — THREE DISTINCT READS, SPLIT (AC3): today `_read_terminal_sizings`
    returns the identical empty `{"scanned": 0, "terminal": [], "non_terminal_count":
    0}` for THREE different situations, only one of which is genuinely "nothing
    terminal": (1) `state/sizings/` does not exist — a COMPUTED empty scan, no
    sizings have ever been written, `collision: False` (there is nothing to collide
    over); (2) `sizings_dir.glob("*.yaml")` raises `OSError` mid-glob — DEGRADED, the
    scan could not run, evidence names the glob call and what raised; (3)
    `_dirty_paths`'s own `git status` call fails — DEGRADED, propagated verbatim
    (same "propagate the sub-read's degraded state as a degraded FACT" discipline
    `session_diff_brightline` already carries for `session_commit_count_attributed`),
    because a scan that cannot tell dirty from clean has no basis to report
    terminal-sizing records as movable-or-not at all. Case (1) and case (2)/(3) no
    longer share a return value — a scan that ran clean and found nothing is no
    longer indistinguishable from a scan that could not run.

    `collision` is a real `bool` for this fact (never `None`) — DR-323 § (b) table:
    `state/sizings/` is a shared surface with live peer edits, so a collision mode
    genuinely exists, unlike Facts 1/3 above.

    TIMED (C1): emits one `op_latency` `"fact_span"` row per call — see
    `_timed_fact`'s own docstring above. This fact takes no `sid`, so the
    row's `sid` field is `None`.
    """
    return _timed_fact(
        "session_terminal_sizings", worktree_root, None,
        _session_terminal_sizings_impl, worktree_root,
    )


def _session_terminal_sizings_impl(worktree_root: Path) -> dict[str, Any]:
    """Untimed body of `session_terminal_sizings` — see that function's
    docstring for the full contract; this exists only so `_timed_fact` can
    wrap the call without altering what is returned."""
    sizings_dir = worktree_root / "state" / "sizings"
    if not sizings_dir.is_dir():
        return {
            "degraded": False,
            "value": {"scanned": 0, "terminal": [], "non_terminal_count": 0},
            "source": _SOURCE_SESSION_TERMINAL_SIZINGS,
            "collision": False,
        }

    try:
        candidates = sorted(sizings_dir.glob("*.yaml"))
    except OSError as exc:
        return {
            "degraded": True,
            "evidence": (
                f"state/sizings/*.yaml glob under {sizings_dir} raised {exc!r}"
            ),
            "source": _SOURCE_SESSION_TERMINAL_SIZINGS,
        }

    dirty_result = _dirty_paths(worktree_root)
    if dirty_result["degraded"]:
        return {
            "degraded": True,
            "evidence": dirty_result["evidence"],
            "source": _SOURCE_SESSION_TERMINAL_SIZINGS,
        }
    dirty = dirty_result["paths"]

    scanned = 0
    non_terminal_count = 0
    terminal: list[dict[str, Any]] = []
    for path in candidates:
        scanned += 1
        status = _read_frontmatter_status(path)
        if not status or status.lower() not in _TERMINAL_SIZING_STATUSES:
            non_terminal_count += 1
            continue
        rel = path.relative_to(worktree_root).as_posix()
        is_dirty = rel in dirty
        terminal.append(
            {
                "path": rel,
                "status": status.lower(),
                "dirty": is_dirty,
                "reason": (
                    "uncommitted edits present — a git mv would sweep in-flight "
                    "work into archive/"
                    if is_dirty
                    else None
                ),
            }
        )

    return {
        "degraded": False,
        "value": {
            "scanned": scanned,
            "terminal": terminal,
            "non_terminal_count": non_terminal_count,
        },
        "source": _SOURCE_SESSION_TERMINAL_SIZINGS,
        "collision": any(entry["dirty"] for entry in terminal),
    }


# ---------------------------------------------------------------------------
# session_fold_sidecars (fl-core-02 C5) — lift of
# quick_wrap_assemble._read_fold_sidecars. A directory listing over
# state/execution-records/ and state/fold-execution-records/.
# ---------------------------------------------------------------------------

#: Same convention, for Fact 5's producer. Points at THIS module's own served
#: function (not `quick_wrap_assemble._read_fold_sidecars`, which C7 deletes) —
#: same reason as Fact 4's source string above: the listing logic is moved
#: wholesale below, so the grep-able producer is this facade itself.
_SOURCE_SESSION_FOLD_SIDECARS = (
    "coordinator_core/session/session_facts.py::session_fold_sidecars"
)


def _scan_fold_sidecar_roots(worktree_root: Path) -> dict[str, Any]:
    """List every `coordinator-fold-execution-record` sidecar under
    `state/execution-records/` and `state/fold-execution-records/`.

    Moved from `quick_wrap_assemble/__init__.py :: _read_fold_sidecars`
    (fl-core-02 C5) — the listing logic (two fixed roots, `rglob("*.json")`,
    repo-relative forward-slash paths) is unchanged. The POSTURE changed: the
    old helper caught `OSError` PER-ROOT and `continue`d, so a partially-
    failed scan reported the same empty-looking result as a clean scan that
    found nothing — a failure indistinguishable from success (AC3). This
    scans every root before returning rather than short-circuiting on the
    first failure, so a root that raises is named alongside whichever root(s)
    succeeded — "one of two roots was unreadable" is a different fact from
    "neither was," and this is where that distinction survives.

    Returns `{"degraded": False, "paths": list[str]}` when every EXISTING
    root was listable (a root that does not exist at all is not a failure —
    same "nothing to scan" posture `session_terminal_sizings` already carries
    for an absent `state/sizings/`), or `{"degraded": True, "evidence": <str>}`
    naming every root that raised and what it raised, when at least one
    existing root could not be listed.
    """
    roots = [
        worktree_root / "state" / "execution-records",
        worktree_root / "state" / "fold-execution-records",
    ]
    found: list[str] = []
    failures: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in sorted(root.rglob("*.json")):
                found.append(path.relative_to(worktree_root).as_posix())
        except OSError as exc:
            failures.append(f"{root.relative_to(worktree_root).as_posix()} raised {exc!r}")
    if failures:
        return {
            "degraded": True,
            "evidence": "fold-sidecar root(s) could not be listed: " + "; ".join(failures),
        }
    return {"degraded": False, "paths": found}


def session_fold_sidecars(worktree_root: Path) -> dict[str, Any]:
    """Serve fold-execution-record sidecar presence (`fl-core-02` Fact 5;
    DR-323's lift of `quick_wrap_assemble._read_fold_sidecars`) — a directory
    listing over `state/execution-records/` and `state/fold-execution-records/`.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE
    CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": <bool>}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `value`'s keys, on a computed record: `present` (bool), `paths` (list of
    repo-relative forward-slash strings), `count` (int) — the same fields
    `_read_fold_sidecars` already computed; only the posture and the enclosing
    shape changed here, not the payload.

    POSTURE CONVERSION (AC3, the whole point of this chunk): the OLD
    `_read_fold_sidecars` caught `OSError` PER-ROOT and `continue`d, so a root
    that could not be listed (permission denied, a symlink cycle, ...)
    reported the same `present: False` as a scan that genuinely found
    nothing — a partially-failed scan with full confidence. Unlike Fact 1's
    `_read_session_shape` seam (which never raises and must have its
    degradation RECOVERED via a file-existence check), this producer's
    failure is a real exception this facade can catch directly — no
    recovery-by-proxy needed. `_scan_fold_sidecar_roots` above catches it: a
    root that raises degrades the fact, with evidence naming WHICH root and
    WHAT it raised, and every root is attempted before the fact returns so
    "one of two roots was unreadable" is not collapsed into "neither was."

    COLLISION — DIFFERS FROM C4'S REFERENCE SHAPE, STATED HERE PER THE BRIEF:
    C4's `collision` folds an OR across a per-record `dirty` bool it already
    computes as part of its own scan (a git-status cross-reference against
    `state/sizings/*.yaml`, one entry per sizing record). This fact's own
    scan carries no analogous per-entry signal — a directory listing has
    nothing to fold over. DR-323 § (b) still requires `collision: bool` here
    (a peer session can write a sidecar into either root concurrently with
    this scan), so this function reuses the SAME underlying mechanism C4
    already established for detecting a live peer write — `_dirty_paths`'s
    `git status --porcelain` read — and asks whether any of the sidecar paths
    THIS scan found are themselves uncommitted right now. `collision: True`
    means at least one fold-execution-record sidecar this scan found is
    mid-write by a peer session (present on disk, not yet committed);
    `False` means every sidecar found is clean. An empty scan short-circuits
    straight to `collision: False` without calling `_dirty_paths` at all —
    same "nothing found, nothing to fold over" shortcut
    `session_terminal_sizings` takes for an absent `state/sizings/`. This is
    a fresh `_dirty_paths` read when it does run, not shared with
    `session_terminal_sizings`'s own call — each served fact owns its own
    sub-reads (module precedent:
    `session_diff_brightline` calls `session_commit_count_attributed`
    independently of `session_magnitude_attributed` rather than sharing a
    cached result across facts).

    `_dirty_paths`'s own `git status` failure propagates as a degraded FACT
    (same "propagate the sub-read's degraded state" discipline
    `session_diff_brightline` and `session_terminal_sizings` already carry) —
    a scan that cannot tell dirty from clean has no basis to report a
    collision state on the sidecars it did find.

    TIMED (C1): emits one `op_latency` `"fact_span"` row per call — see
    `_timed_fact`'s own docstring above. This fact takes no `sid`, so the
    row's `sid` field is `None`.
    """
    return _timed_fact(
        "session_fold_sidecars", worktree_root, None,
        _session_fold_sidecars_impl, worktree_root,
    )


def _session_fold_sidecars_impl(worktree_root: Path) -> dict[str, Any]:
    """Untimed body of `session_fold_sidecars` — see that function's
    docstring for the full contract; this exists only so `_timed_fact` can
    wrap the call without altering what is returned."""
    scan = _scan_fold_sidecar_roots(worktree_root)
    if scan["degraded"]:
        return {
            "degraded": True,
            "evidence": scan["evidence"],
            "source": _SOURCE_SESSION_FOLD_SIDECARS,
        }
    found = scan["paths"]

    # No sidecar found means there is nothing a peer could be mid-write on — same
    # short-circuit `session_terminal_sizings` takes for an absent `state/sizings/`
    # (collision: False without a sub-read), and it avoids paying `_dirty_paths`'s
    # git-status cost on the empty-scan hot path.
    if not found:
        collision = False
    else:
        dirty_result = _dirty_paths(worktree_root)
        if dirty_result["degraded"]:
            return {
                "degraded": True,
                "evidence": dirty_result["evidence"],
                "source": _SOURCE_SESSION_FOLD_SIDECARS,
            }
        collision = any(path in dirty_result["paths"] for path in found)

    return {
        "degraded": False,
        "value": {
            "present": bool(found),
            "paths": found,
            "count": len(found),
        },
        "source": _SOURCE_SESSION_FOLD_SIDECARS,
        "collision": collision,
    }
