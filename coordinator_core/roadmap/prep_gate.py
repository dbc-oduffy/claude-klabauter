"""
coordinator_core.roadmap.prep_gate — the mise-prep authoring bar, evaluated.

Purpose: answers one question about one plan, from disk alone — **is every input
a fire-time driver would otherwise have to ask a human about DECLARED?** A plan
that clears the bar may be certified for a hands-off run; a plan that does not is
reported class by class, because the four classes are fixed in four different
places.

The bar is ONE rule, not four. "Nothing to declare" is itself a declaration —
which is why the spine's ``writes: []`` is a different value from an absent
``writes:`` (``spine_read.UNDECLARED``), why ``census: []`` is a plan asserting
it rests on no counted premise, and why an ``external_gate`` entry's
``requires:`` is an author saying which side of DR-127 their cross-repo
dependency falls on. The four report classes are a routing aid, not four bars.

  SPINE          ``read_spine()`` succeeds, no row carries ``UNDECLARED`` writes,
                 ``build_waves()`` returns without ``WaveCycleError``. EXECUTED,
                 never reviewed — the predicate is the engine's own reader
                 answering, not a human reading a spine and agreeing.
  CENSUS         ``census:`` present in frontmatter, every entry declaring
                 ``question`` + ``command`` + ``result``. ``census: []`` passes.
  EXTERNAL_DEPS  Every row whose declared paths leave this repo carries an
                 ``external_gate``; every uncleared gate declares ``requires:``.
                 BOTH ``requires:`` values withhold only their own ROW and the
                 plan certifies on the rows that remain. They stay
                 distinguishable in the detail line because they route
                 differently once withheld: ``landed-work`` waits for a peer's
                 landing, ``commit-in-owner-repo`` needs a cross-repo commit
                 dispatched under per-session assent.
  PRIME_EXIT     ``prime_exit_criterion`` with a non-empty ``statement`` and a
                 non-empty ``derived_from``, at EVERY size — not only M/L/XL.

Domain vocabulary: the bar, a report class, a verdict (PREPPED / NOT-PREPPED),
a withheld row, a declared-empty.

Consumed by ``coordinator_core.ops.plan_prep_gate`` (the ``plan.prep_gate`` op,
which reports) and ``coordinator_core.ops.plan_stamp_prepped`` (the
``plan.stamp_prepped`` op, which refuses on a non-PREPPED verdict and stamps the
four-field attest under a lock). This module registers nothing and writes
nothing.

Read-side twin, and the authority for every predicate here: DoE-claude
``coordinator/bin/mise-prep-gate.py``. That script and this module must agree —
they answer the same question over two different corpora, and a verdict that
depends on which one ran is not a bar. Where a predicate is restated here it is
restated to the letter; where it necessarily differs (see ``fleet_siblings``) the
difference is named.

Spec backlink: DoE-claude coordinator/docs/wiki/mise-prepped-authoring-bar.md
               DoE-claude coordinator/docs/wiki/mise-prepped-attest.md
               .coordinator-local/memo-outbox/sent/mise-prepped-shape-ruling.md

Budget: pure reads, ZERO spawns, no git. One bounded read per plan plus the
engine's own pure spine readers; cost scales with plan COUNT, not corpus bytes.
ONE PLAN PER CALL is a budget decision, not a convenience — see ``gate_plan``
for the measurement that forces it and the corpus-census surface it routes to.

Negative-spec:
  - Does NOT stamp, write, or mutate anything. A closed gate is REPORTED here;
    refusing on it is the caller's act — the same doctrine ``plan_gate.py``
    states, for the same reason: an authorization decision behind a derived read
    silently blocks work when the read goes stale rather than mis-reporting it.
  - Does NOT collapse the four classes into a boolean. The per-class split IS the
    product: the corpus's dominant defect (no spine block at all) and its rarest
    (a spine the reader refuses) differ by an order of magnitude and are fixed in
    different places, and a boolean reports the first under the name of the
    second.
  - Does NOT re-run a declared ``census[].command``. This module is spawn-free by
    contract; re-asking HEAD the same question is fire-time work for the runner,
    which is the whole reason the command is recorded rather than the answer
    alone. The bar checks that the question is ASKABLE.
  - Does NOT scan narrative prose. No predicate here fires on the SHAPE of a
    sentence: a check that did would teach authors to phrase around it and could
    not say what was missing. Every leg reads a declared field.
  - Does NOT read ``surface:`` as a path. Its own schema description admits "a
    single path or SUBSYSTEM" and the corpus uses that licence for values that
    are not paths; ``surface:`` is read only by the sibling-NAME leg, where prose
    cannot accidentally spell a repo shortname.
  - Does NOT refuse a whole plan for a ``landed-work`` gate. That withholding is
    ROW granularity; refusing the plan would discard schedulable rows alongside
    the blocked one.
  - Does NOT probe the machine-local registry for fleet repo names. That is a
    subprocess on a box already running dozens of sessions; the fleet list below
    is a closed constant.
  - Does NOT import ``coordinator_core.ops.*`` at module scope. The spine readers
    are imported inside the predicate that needs them, so a module under ``ops/``
    importing this one cannot pull ``ops/__init__`` back through itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The two verdicts, spelled exactly as the read-side twin spells them —
#: consumers on both sides of the repo boundary compare these strings.
PREPPED = "PREPPED"
NOT_PREPPED = "NOT-PREPPED"

#: RETIRED, and named rather than deleted because its ABSENCE is the operative
#: rule: a reader who does not know this was retired reads a plan touching a
#: sibling repo as un-fireable and reinstates the whole-plan refusal. PM ruling —
#: a plan is not rejected because part of it needs code in another repo. That
#: work is withheld and routed, exactly as ``landed-work`` already was. Nothing
#: produces this verdict; the constant and ``EXIT_REFUSED`` stay reserved so no
#: consumer's string comparison or exit-code mapping shifts under them.
REFUSED = "REFUSED"

#: Report-class order. Fixed, because the refusal message enumerates in it and a
#: message whose line order varies per plan is harder to diff than one that does
#: not.
CLASS_ORDER = ("SPINE", "CENSUS", "EXTERNAL_DEPS", "PRIME_EXIT", "SCHEMA")

#: ``external_gate[].requires`` — the discriminant the three-way split turns on.
#: ``condition:`` is reader-facing prose the schema itself says no consumer parses
#: for truth, so the split cannot be read off it without guessing at a sentence.
REQUIRES_COMMIT = "commit-in-owner-repo"
REQUIRES_LANDED = "landed-work"
REQUIRES_VALUES = (REQUIRES_LANDED, REQUIRES_COMMIT)

#: Fleet repo shortnames, the vocabulary ``external_gate[].owner_repo`` accepts.
#: A closed list rather than a probe: these are fleet constants, not machine
#: facts, and resolving them would mean spawning `machine-local` on a box already
#: carrying many concurrent sessions.
#:
#: EVERY FLEET NAME, INCLUDING THE DOCTRINE REPO'S, and the read-side twin's
#: ``SIBLING_REPOS`` carries the same eight. Neither half hard-omits a name:
#: both run over whichever repo the caller stands in, so the repo's own name is
#: subtracted at CALL time (``fleet_siblings``) rather than at authoring time. A
#: hard omission would be right only for a gate that could run over one corpus
#: alone; a gate that takes a repo root and finds the doctrine repo a sibling
#: must be able to name it.
FLEET_REPOS = (
    "claude-klabauter",
    "claude-klabauter",
    "example-retrieval-repo",
    "coordinator-claude",
    "example-game-workbench-repo",
    "example-cockpit-repo",
    "example-market-data-repo",
    "DoE-claude",
)

#: Keys every ``census[]`` entry declares. Presence-and-non-blank, never a value
#: check — the command is recorded so a runner can re-ask it, not so this module
#: can answer it.
CENSUS_ENTRY_KEYS = ("question", "command", "result")

#: Scaffold placeholders. A key whose value is still the generator's marker is
#: PRESENT and NON-BLANK, so every predicate here that tests "non-empty" would
#: pass it — which is the one way a live-emitted stub could clear this bar
#: without an author ever having answered it. That is the form-filling failure
#: the bar exists to prevent, so the markers are named and refused.
#:
#: This is what lets the PRODUCER emit `prime_exit_criterion` live (as
#: `coordinator/templates/plans/plan.md.tmpl` already does) instead of commented
#: out: a plan is then born with the key present and visibly unanswered, and the
#: gate still refuses it until it is answered. Key-absent and key-placeholder are
#: reported as different defects because the repairs differ — one adds a key, the
#: other replaces a marker.
PLACEHOLDER_MARKERS = ("<REPLACE:", "<replace:", "REPLACE ME", "TODO:", "TBD")


def is_placeholder(value: Any) -> bool:
    """True when ``value`` is still a scaffold marker rather than an answer.

    Substring, not prefix: a marker survives YAML block-scalar folding with
    leading whitespace and trailing prose around it.
    """
    if not isinstance(value, str):
        return False
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


# ---------------------------------------------------------------------------
# Per-corpus inputs
# ---------------------------------------------------------------------------


def fleet_siblings(repo_root: Path) -> tuple:
    """``FLEET_REPOS`` minus the repo the scan is standing in.

    The discriminant is the worktree directory's own NAME, which is the only
    repo-identity fact available without a registry read. A row naming the repo
    it already lives in is a ``depends_on`` edge mis-spelled as a path, not a
    cross-repo dependency, and reporting it as one would fire the DR-127 leg on
    every plan that cites its own tree by name. That reasoning is why the
    subtraction happens; it is not a reason to omit a name from ``FLEET_REPOS``,
    because the repo being stood in changes per invocation and the constant does
    not.

    Case-folded, for the reason ``_path_leaves_repo`` folds case: a clone at
    ``doe-claude/`` and one at ``DoE-claude/`` are the same repo, and a
    subtraction that missed on case would report every self-naming row in one of
    them as a cross-repo dependency.
    """
    own = repo_root.name.casefold()
    return tuple(name for name in FLEET_REPOS if name.casefold() != own)


def repo_root_names(repo_root: Path) -> frozenset:
    """Top-level entry names in ``repo_root``, for the ROOT-EXISTENCE leg.

    Read once per scan rather than per row: a directory listing is cheap, and
    doing it per path would turn a whole-corpus sweep into a stat storm on a
    shared box.
    """
    try:
        return frozenset(entry.name for entry in repo_root.iterdir())
    except OSError:
        return frozenset()


# ---------------------------------------------------------------------------
# Verdict primitives
# ---------------------------------------------------------------------------


def _pass(detail: str, withheld: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"status": "PASS", "kind": None, "detail": detail, "withheld": withheld or []}


def _defect(kind: str, detail: str, withheld: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"status": "DEFECT", "kind": kind, "detail": detail, "withheld": withheld or []}


#: RETIRED with the REFUSED verdict it produced, and kept as the one shape that
#: reaches it, so a predicate reintroducing a whole-plan refusal has to name this
#: helper and be seen doing it rather than inventing a second refusal path. No
#: predicate calls it; see ``REFUSED`` for the ruling.
def _refuse(kind: str, detail: str, withheld: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"status": "REFUSE", "kind": kind, "detail": detail, "withheld": withheld or []}


# ---------------------------------------------------------------------------
# Predicate: SPINE
# ---------------------------------------------------------------------------


def _spine(plan_path: Path, text: str) -> Dict[str, Any]:
    """``read_spine()`` + ``build_waves()``, EXECUTED. Never a review of shape.

    The absent-block case is reported separately from a parse error because the
    fixes differ and the counts differ by an order of magnitude. Collapsing them
    would report the corpus's dominant defect under the name of its rarest.

    ``read_spine`` re-reads ``plan_path`` rather than accepting ``text`` — that is
    its contract, and the one caller that holds bytes under a lock
    (``plan.stamp_prepped``) reads the same file it locked, so the two agree by
    construction. Not worth a second spine parser to avoid one bounded read.
    """
    from coordinator_core.ops.dispatch_emit.spine_read import (
        UNDECLARED,
        SpineReadError,
        read_spine,
    )
    from coordinator_core.ops.dispatch_emit.wave_map import WaveCycleError, build_waves

    if "```yaml plan-tasks" not in text:
        return _defect(
            "spine-absent",
            "no ```yaml plan-tasks block — a plan with no spine declares no scope to schedule",
        )
    try:
        rows = read_spine(plan_path)
    except SpineReadError as exc:
        return _defect(type(exc).__name__, str(exc).strip().splitlines()[0][:300])
    undeclared = [row.id for row in rows if row.writes is UNDECLARED]
    if undeclared:
        return _defect(
            "writes-undeclared",
            f"rows with no writes: {', '.join(undeclared)} "
            "(a row that writes nothing declares `writes: []`)",
        )
    try:
        waves = build_waves(rows)
    except WaveCycleError as exc:
        return _defect("wave-cycle", str(exc).strip().splitlines()[0][:300])
    return _pass(f"{len(rows)} dispatchable row(s) across {len(waves)} wave(s)")


# ---------------------------------------------------------------------------
# Predicate: CENSUS
# ---------------------------------------------------------------------------


def _census(fm: Dict[str, Any]) -> Dict[str, Any]:
    """``census:`` present, every entry ``question`` + ``command`` + ``result``.

    Presence is the predicate, and ``census: []`` passes. Requiring the KEY asks
    the author one question a reader can falsify by reading the plan: does this
    plan rest on a counted premise? A ``census: []`` that is wrong is a defect a
    reviewer can point at; a missing census is one nobody can see.
    """
    if "census" not in fm:
        return _defect(
            "census-undeclared",
            "no census: key (a plan resting on no counted premise declares `census: []`)",
        )
    entries = fm.get("census")
    if entries is None:
        return _defect(
            "census-undeclared",
            "census: is empty rather than declared-empty (write `census: []`)",
        )
    if not isinstance(entries, list):
        return _defect("census-malformed", "census: is not a list")
    bad: List[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            bad.append(f"[{index}] not a mapping")
            continue
        missing = [k for k in CENSUS_ENTRY_KEYS if not str(entry.get(k) or "").strip()]
        if missing:
            bad.append(f"[{index}] missing {', '.join(missing)}")
            continue
        unanswered = [k for k in CENSUS_ENTRY_KEYS if is_placeholder(entry.get(k))]
        if unanswered:
            bad.append(f"[{index}] scaffold placeholder in {', '.join(unanswered)}")
    if bad:
        return _defect("census-incomplete", "; ".join(bad))
    if not entries:
        return _pass("declared-empty — this plan rests on no counted premise")
    return _pass(f"{len(entries)} re-runnable census entr(ies)")


# ---------------------------------------------------------------------------
# Predicate: EXTERNAL_DEPS
# ---------------------------------------------------------------------------


def _path_leaves_repo(
    field: str, value: str, root_names: frozenset, siblings: Sequence[str]
) -> Optional[str]:
    """Why ``value`` names something outside this repo, or None.

    Two legs, reading different fields on purpose:

    SIBLING-NAME (``writes:``, ``reads:``, ``surface:``) — the value begins with a
    fleet repo shortname. The match is on the name plus any non-alphanumeric
    separator rather than on one punctuation: an author writing
    ``claude-klabauter@coordinator_core/...`` has said the same thing as one writing
    ``claude-klabauter/coordinator_core/...``, and a leg that saw only one of them
    would report the corpus as mostly clean. A repo shortname is a NAME, not a
    path shape, so this leg reads ``surface:`` safely.

    The name match is CASE-FOLDED, and the separator rule is unchanged by that.
    The corpus does not agree with itself on the case of a fleet shortname — the
    doctrine repo is spelled both ``DoE-claude`` and ``doe-claude`` by its own
    peers, in plan prose and in cross-repo archives — so a case-sensitive ``==``
    made a row declaring a genuine cross-repo surface in the corpus's OWN
    spelling invisible to this leg. Folding case widens which spellings are SEEN;
    it does not widen what counts as a separator, so ``claude_klabauter2/x`` is
    still not a match.

    ROOT-EXISTENCE (``writes:``, ``reads:`` only) — the value is a MULTI-SEGMENT
    path whose first segment is not an entry at this repo's root, so as a
    repo-relative path it cannot resolve here. It does NOT run against
    ``surface:``, whose schema description admits "a single path OR SUBSYSTEM";
    reading those values as paths reports prose.

    A SINGLE-SEGMENT value is exempt, and the exemption is structural rather than
    asserted. What this leg genuinely catches is a nameless path INTO another
    repo's tree (``coordinator_core/ops/foo.py``), and a path into a tree has a
    tree to be into: it carries a separator by construction. A value with no
    separator names one entry at THIS repo's root — the plan is creating it, and
    a plan is allowed to create a root-level file or directory. That was the
    false-positive shape this docstring previously named and dismissed as
    unobserved; it has since been observed (example-retrieval-repo
    ``docs/plans/2026-09-06-inbox-blitz-xs-s-bundle.md``, row T8, a new
    root-level ``ADOPTERS`` file), and the only way its author could pass the bar
    was to delete the declared write from ``writes:`` — the leg forced an
    UNDER-declaration, inverting the one rule the whole bar enforces. No author
    assertion clears this leg: the discriminant is read off the declared value's
    own shape, so it cannot be claimed, only spelled.

    The residual the exemption accepts, named rather than assumed away: a row
    declaring a sibling's whole tree as one bare directory name (``writes:
    [coordinator_core]``) reads as a new local root entry. It is bounded — such a
    row declares no file it would touch, so SPINE's write set is useless for it
    either way — and it is zero in both measured corpora.
    """
    stripped = value.strip()
    if not stripped:
        return None
    folded = stripped.casefold()
    for sibling in siblings:
        key = sibling.casefold()
        if folded == key or (
            folded.startswith(key) and not folded[len(key)].isalnum()
        ):
            return f"names {sibling}"
    if field == "surface":
        return None
    normalized = stripped.replace("\\", "/")
    if "/" not in normalized.strip("/"):
        return None
    first = normalized.split("/")[0]
    if first and first not in root_names:
        return f"first path segment {first!r} does not exist in this repo"
    return None


#: Dispositions ``dispatch_emit/spine_read.py`` treats as done, alongside
#: ``deferred: true``. A literal set rather than an import: this gate is read by
#: callers that have not loaded the emitter, and a gate that needs another
#: subsystem to answer is a gate that fails for the wrong reason.
_UNSCHEDULABLE_DISPOSITIONS = frozenset({"coded", "spun_off", "backlogged", "wont_do"})


def _row_is_unschedulable(row: Dict[str, Any]) -> bool:
    """Will the wave-builder decline to schedule this row at all?

    ``spine_read.py`` excludes a row that is explicitly ``deferred: true`` or
    carries a closed disposition. Such a row is never dispatched, so nothing it
    declares is ever resolved by a driver -- which is what makes an unresolvable
    declaration on it moot rather than defective.
    """
    if row.get("deferred") is True:
        return True
    return str(row.get("disposition") or "").strip() in _UNSCHEDULABLE_DISPOSITIONS


def _path_is_unresolved_placeholder(value: str) -> bool:
    """True when a declared path is still an angle-bracketed stand-in.

    Carried as its own finding because the single-segment exemption in
    ``_path_leaves_repo`` would otherwise silently drop the one real catch the
    ROOT-EXISTENCE leg had at single-segment depth. Measured across both corpora,
    every single-segment value that leg reported was either a legitimate local
    entry or this: one ``<...>`` stand-in a generator left behind
    (``'<isolated-registration-surface-resolved-in-chunk>'``). The module's own
    prior measurement already called that value "itself a defect this bar should
    catch", so it is caught by name instead of as a side effect of depth.

    Reported separately from ``PLACEHOLDER_MARKERS`` because the shapes differ: a
    scaffolded FRONTMATTER key carries the generator's literal marker, while a
    hand-authored path carries the author's own bracketed stand-in. A gate never
    clears it — an ``external_gate`` says who owns a path, not what the path is.
    """
    return "<" in value or ">" in value


def _row_declared_paths(row: Dict[str, Any]) -> List[tuple]:
    """``(field, value)`` for every path-shaped declaration on a row.

    ``surface:`` rides alongside ``writes:``/``reads:`` because it is where the
    corpus actually names cross-repo work. A gate reading only the two array
    fields would call a plan clean on the strength of the field its author did
    not use.
    """
    out: List[tuple] = []
    for key in ("writes", "reads"):
        value = row.get(key)
        if isinstance(value, list):
            out.extend((key, item) for item in value if isinstance(item, str))
    surface = row.get("surface")
    if isinstance(surface, str):
        out.append(("surface", surface))
    return out


def _gate_is_cleared(entry: Dict[str, Any]) -> bool:
    """``cleared: true`` and nothing else, matching
    ``spine_read._has_uncleared_execution_gate``.

    ``closure_evidence`` never clears a gate here either — the two readers must
    agree or a gate's visibility depends on which one saw it first.
    """
    return entry.get("cleared") is True


def _external_deps(
    rows: List[Dict[str, Any]], root_names: frozenset, siblings: Sequence[str]
) -> Dict[str, Any]:
    """The three-way split, at the granularity each leg earns.

    UNDECLARED (row leaves the repo, no gate) and MISSING-REQUIRES (gate present,
    discriminant absent) are both NOT-PREPPED: an author fixes them here.

    BOTH ``requires:`` values withhold their own row and nothing else; the plan
    certifies on the rows that remain.

    COMMIT-IN-OWNER-REPO used to refuse the whole plan, reasoning that a
    hands-off run has no session in which to obtain the per-session assent a
    cross-repo commit needs. That is sound about the ROW and wrong about the
    PLAN: withholding the row already keeps the run from writing into a sibling's
    tree unassented, and refusing on top of that discarded every row which had
    nothing to do with the sibling. It is the same argument this leg already made
    for LANDED-WORK — refusing the plan would discard every schedulable row
    alongside the blocked one — which had never been applied to the other value.

    The two remain distinguishable in the detail line because they route
    differently once withheld: LANDED-WORK waits for a peer's landing, and
    COMMIT-IN-OWNER-REPO needs a cross-repo commit dispatched. Both are the
    successor's work, never a reason to refuse the plan.
    """
    undeclared: List[str] = []
    placeholders: List[str] = []
    missing_requires: List[str] = []
    bad_requires: List[str] = []
    commit_gated: List[str] = []
    withheld: List[str] = []

    for row in rows:
        row_id = str(row.get("id") or "<row with no id>")
        gates = row.get("external_gate")
        gates = [g for g in gates if isinstance(g, dict)] if isinstance(gates, list) else []
        # A row the wave-builder will not schedule declares nothing a fire-time driver
        # must resolve, because no driver will fire it: `dispatch_emit/spine_read.py`
        # excludes `deferred: true` and closed-disposition rows from every wave, so
        # their declared paths are never opened. Refusing the whole plan over a
        # placeholder in one of them discards the rows that had nothing to do with it —
        # the same reasoning this function already applies to a withheld cross-repo row.
        #
        # Measured case (DoE-claude
        # docs/plans/2026-07-30-boot-payload-residue-curation-and-dispatch-guards.md):
        # C8b declares `writes: ~/.claude/projects/<project>/memory/` and is
        # `deferred: true` / `pm_approved: false`, withheld behind a FRONTMATTER-level
        # `external_gate: EG1`. An integrator had removed the ROW-level external_gate on
        # correct grounds — the schema types it {owner_repo, condition} for a CROSS-REPO
        # blocker, and EG1 names a machine-local store, not a sibling repo. Both sides
        # were right and the plan still could not certify, because the row was withheld
        # by a mechanism this leg does not read. Scoped to the placeholder leg only.
        #
        # Skipped, NOT withheld: `withheld` becomes `mise_prepped_findings`, which the
        # attest contract (DoE-claude coordinator/docs/wiki/mise-prepped-attest.md)
        # defines as rows held by an uncleared external_gate — work waiting on
        # somebody else. A coded or wont_do row is finished, and a deferred one is
        # out of scope; listing them made a 59-of-70-coded plan on example-game-repo read as a
        # PARTIAL-FIRE excluding 62 rows.
        if _row_is_unschedulable(row):
            continue
        for field, value in _row_declared_paths(row):
            if field != "surface" and _path_is_unresolved_placeholder(value):
                placeholders.append(
                    f"{row_id}: {field} {value.strip()!r} is an unreplaced placeholder, "
                    "not a path (no external_gate clears it)"
                )
                continue
            reason = _path_leaves_repo(field, value, root_names, siblings)
            if reason and not gates:
                undeclared.append(f"{row_id}: {field} {reason}, no external_gate")
        for index, entry in enumerate(gates):
            if _gate_is_cleared(entry):
                continue
            requires = entry.get("requires")
            if requires is None:
                missing_requires.append(
                    f"{row_id}: external_gate[{index}] has no requires: "
                    f"({' | '.join(REQUIRES_VALUES)})"
                )
            elif requires not in REQUIRES_VALUES:
                bad_requires.append(
                    f"{row_id}: external_gate[{index}] requires: {requires!r} is not "
                    f"{' | '.join(REQUIRES_VALUES)}"
                )
            elif requires == REQUIRES_COMMIT:
                owner = entry.get("owner_repo") or "a sibling repo"
                commit_gated.append(f"{row_id}: commit into {owner}")
                withheld.append(row_id)
            else:
                withheld.append(row_id)

    defects = undeclared + missing_requires + bad_requires
    if placeholders:
        # Its own kind, not folded into external-dep-undeclared: the repair
        # differs — one replaces a stand-in with the path it stands for, the
        # other adds a gate — and a tally that names only the second sends the
        # author to the wrong fix.
        return _defect(
            "path-placeholder", "; ".join(placeholders + defects), withheld=withheld
        )
    if defects:
        return _defect("external-dep-undeclared", "; ".join(defects), withheld=withheld)
    if withheld:
        # Both populations named, because the withheld set alone says a row is
        # held and not what would release it — one waits for a peer's landing,
        # the other needs a commit dispatched into a tree this run may not write.
        detail = f"{len(set(withheld))} row(s) withheld: {', '.join(sorted(set(withheld)))}"
        if commit_gated:
            detail += (
                f" — of which needing a cross-repo commit ({'; '.join(commit_gated)}), "
                "to be dispatched under per-session assent rather than refused"
            )
        return _pass(detail, withheld=withheld)
    return _pass("no declared path leaves this repo")


# ---------------------------------------------------------------------------
# Predicate: PRIME_EXIT
# ---------------------------------------------------------------------------


def _prime_exit(fm: Dict[str, Any]) -> Dict[str, Any]:
    """``prime_exit_criterion.statement`` and ``.derived_from`` non-empty, at
    every size.

    The mandate widens WHERE an existing instrument runs; it builds nothing. It is
    a read-side predicate rather than a ``required`` entry in plan.schema.json for
    the reason the falsifier's own M/L/XL rule is read-side: making it structurally
    required would retroactively invalidate every plan that does not carry one, and
    the only way to revalidate those is to write criteria onto plans nobody
    authored them for.

    ``falsifier`` is deliberately NOT required — its proportionality rule is
    unchanged and is not this bar's business.
    """
    criterion = fm.get("prime_exit_criterion")
    if not isinstance(criterion, dict):
        return _defect(
            "prime-exit-absent",
            "no prime_exit_criterion (required at every size, not only M/L/XL)",
        )
    if not str(criterion.get("statement") or "").strip():
        return _defect("prime-exit-empty", "prime_exit_criterion carries no statement")
    if is_placeholder(criterion.get("statement")):
        return _defect(
            "prime-exit-placeholder",
            "prime_exit_criterion.statement is still a scaffold placeholder "
            "(replace the <REPLACE: ...> marker with the falsifiable sentence)",
        )
    if not str(criterion.get("derived_from") or "").strip():
        return _defect(
            "prime-exit-underived",
            "prime_exit_criterion has no derived_from (a link, not a self-declaration)",
        )
    if is_placeholder(criterion.get("derived_from")):
        return _defect(
            "prime-exit-placeholder",
            "prime_exit_criterion.derived_from is still a scaffold placeholder "
            "(replace it with the sizing object or goal KR it derives from)",
        )
    return _pass("declared")


#: The plan schema this gate validates against, resolved off this module's own
#: location for the reason `_UPGRADE_SCRIPT` is: it names the tree that actually
#: answered, not whichever tree a caller's cwd happens to sit in.
_PLAN_SCHEMA = (
    Path(__file__).resolve().parents[1] / "frontmatter" / "schemas" / "plan.schema.json"
)

#: Schema error fields whose defect another class already reports. Suppressed so
#: the message states one fact once: a plan with no `prime_exit_criterion` would
#: otherwise be told so twice, by PRIME_EXIT and again by the schema walk.
_SCHEMA_FIELDS_OWNED_ELSEWHERE = ("prime_exit_criterion",)

#: The mise-prep attest. Excluded from the schema walk ALWAYS — not as noise
#: reduction but because a gate cannot condition on its own output. These four
#: fields are what `plan.stamp_prepped` WRITES after this bar passes, and a
#: partial hand-written quartet is a schema error whose documented repair is a
#: full re-stamp. Letting it defect here deadlocks that repair: the gate refuses
#: the plan over the exact malformation the stamp it is blocking would fix.
_SCHEMA_STAMP_FIELDS = "mise_prepped"


def _schema(fm: Dict[str, Any], prime_exit: Dict[str, Any]) -> Dict[str, Any]:
    """``plan.schema.json`` over the frontmatter this gate is about to certify.

    example-retrieval-repo, 2026-09-11: a plan reached approved AND certified carrying
    ``prime_exit_criterion.derived_from`` with a paragraph of prose where the
    schema wants ``^state/sizings/.+\\.yaml$``. The prep gate passed it because
    its own predicates check PRESENCE and non-placeholder-ness, never SHAPE —
    two different questions about the same field. Only the frontmatter-schema
    hook caught it, and only because the author happened to edit the file for an
    unrelated reason, which is not a mechanism.

    Advisory about its own instrument, never about the plan: a schema that
    cannot be read or does not parse PASSES here. A gate that fails closed on a
    missing schema file would refuse every plan in a tree whose vendored schemas
    have not been re-published yet, which is a defect in this gate, not in the
    plans.
    """
    try:
        from coordinator_core.frontmatter.schema_validate import validate_frontmatter

        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
    except Exception:
        return _pass("not checked: plan.schema.json is unreadable beside this engine")
    errors = [
        e for e in errors if _SCHEMA_STAMP_FIELDS not in str(e.get("field") or "")
    ]
    if prime_exit["status"] != "PASS":
        errors = [
            e
            for e in errors
            if not str(e.get("field") or "").startswith(_SCHEMA_FIELDS_OWNED_ELSEWHERE)
        ]
    if not errors:
        return _pass("valid")
    detail = "; ".join(
        f"{e.get('field')}: {e.get('error')}" for e in errors[:4]
    )
    if len(errors) > 4:
        detail += f" (+{len(errors) - 4} more)"
    return _defect("schema-invalid", f"frontmatter violates plan.schema.json — {detail}")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def plan_frontmatter(text: str) -> Dict[str, Any]:
    """The plan's frontmatter as a mapping, or ``{}`` when it has none or it does
    not parse.

    Splitting is delegated to ``primitives.split_frontmatter`` — a second
    frontmatter locator is a second place the rule can drift from the one every
    other reader in this engine uses.
    """
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(text)
    if split is None:
        return {}
    try:
        loaded = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def raw_spine_rows(text: str) -> List[Dict[str, Any]]:
    """Every spine row AS AUTHORED, including the ones ``read_spine`` excludes.

    ``read_spine`` drops a row carrying an uncleared execution gate — correctly,
    for dispatch. Those rows are this bar's whole subject, so EXTERNAL_DEPS reads
    the raw block. Reading the filtered list would make a plan look cleanest at
    exactly the moment its gates were most numerous.

    An unreadable block yields no rows, which makes EXTERNAL_DEPS pass vacuously —
    deliberately. SPINE has already reported the same file as unreadable in the
    same message, and a second finding derived from rows nobody could parse would
    name a defect that may not exist.
    """
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.plan_tasks_render import load_rows

    result = load_rows(text)
    return list(result.rows) if result.status is LocateStatus.LOCATED else []


# ---------------------------------------------------------------------------
# The bar
# ---------------------------------------------------------------------------


def evaluate_plan(
    plan_path: Path,
    *,
    text: Optional[str] = None,
    root_names: frozenset,
    siblings: Sequence[str],
) -> Dict[str, Any]:
    """The whole bar over one plan. Returns a report; writes nothing.

    ``text`` is the plan's bytes when the caller already holds them — the write
    op holds them under a lock and must gate the exact document it is about to
    stamp, or the recorded sha certifies a body the bar never saw. Omitted, the
    file is read here.
    """
    if text is None:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    fm = plan_frontmatter(text)
    prime_exit = _prime_exit(fm)
    classes = {
        "SPINE": _spine(plan_path, text),
        "CENSUS": _census(fm),
        "EXTERNAL_DEPS": _external_deps(raw_spine_rows(text), root_names, siblings),
        "PRIME_EXIT": prime_exit,
        "SCHEMA": _schema(fm, prime_exit),
    }
    if any(v["status"] == "REFUSE" for v in classes.values()):
        verdict = REFUSED
    elif any(v["status"] == "DEFECT" for v in classes.values()):
        verdict = NOT_PREPPED
    else:
        verdict = PREPPED
    withheld = sorted({r for v in classes.values() for r in v["withheld"]})
    return {
        "path": plan_path.as_posix(),
        "verdict": verdict,
        "withheld_rows": withheld,
        "classes": classes,
        "message": refusal_message(plan_path, verdict, classes, withheld),
    }


#: The converter this gate routes a NOT-PREPPED author to. It is a SIBLING of this
#: engine — `<engine root>/coordinator/bin/mise-prep-upgrade.py` — so it is resolved
#: off this module's own location, which is the only thing that reliably names the
#: tree that actually answered.
_UPGRADE_SCRIPT = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "mise-prep-upgrade.py"


def _upgrade_fix_line() -> str:
    """The NOT-PREPPED `fix:` line, naming a path its READER can run.

    This message is emitted through `plan.prep_gate` into whatever repo is being
    gated, and the bare relative literal it used to carry —
    `coordinator/bin/mise-prep-upgrade.py` — resolves against THAT repo, where it
    does not exist. Measured on example-retrieval-repo-ue-addon: every NOT-PREPPED verdict the
    op returned named a file absent from the repo it was talking about.

    It is the same defect coordinator-claude's own `mise-prep-gate.py` was repaired
    for, and the two doors disagreed for exactly as long as this one went unfixed:
    the CLI resolved the converter absolutely while the op printed a dead relative
    path for the same plan and the same verdict. A bar that answers differently
    depending on which door you came through is not one bar.

    Fail-open on the repair line — an unresolvable converter is not worth failing a
    verdict over — but never a silent guess: an unnamed path is reported as unnamed
    rather than printed as a specific, plausible, dead one.
    """
    if _UPGRADE_SCRIPT.is_file():
        return (
            f"  fix: python {_UPGRADE_SCRIPT} <plan>  "
            "(derives what the body already declares; never invents a census or a criterion)"
        )
    return (
        "  fix: the mise-prep converter is not present beside this engine — reinstall or "
        "republish claude-klabauter, then rerun"
    )


def refusal_message(
    plan_path: Path, verdict: str, classes: Dict[str, Any], withheld: Sequence[str]
) -> str:
    """ONE message enumerating every missing declaration at once.

    A bar that reports its failures one at a time makes an author iterate through
    four round trips to learn one thing, each trip re-reading a plan that has not
    changed. Register: one fact per line, the terse alternative where one exists,
    and no override key — there is no way to pass this bar except by declaring
    what it names.
    """
    name = plan_path.name
    if verdict == PREPPED:
        tail = f" ({len(withheld)} row(s) withheld: {', '.join(withheld)})" if withheld else ""
        return f"mise-prep: PREPPED — {name}{tail}"
    lines = [f"mise-prep: {verdict} — {name}"]
    for key in CLASS_ORDER:
        value = classes[key]
        if value["status"] == "PASS":
            continue
        lines.append(f"  {key:<14} {value['detail']}")
    if verdict == REFUSED:
        lines.append("  route: PM, not the plan author.")
    else:
        # NOT-PREPPED only. A plan authored before this bar existed is missing
        # keys its generator never emitted, and the repair is mechanical for the
        # part that is derivable from the plan's own body. Naming the converter
        # here is what stops each session rediscovering it — a runnable script,
        # never a slash command, because what fails here may have no session.
        lines.append(_upgrade_fix_line())
    return "\n".join(lines)


def gate_plan(worktree_root: Path, plan_path: Path, *, text: Optional[str] = None) -> Dict[str, Any]:
    """The bar over ONE plan, with the two per-corpus inputs derived here.

    The single entrypoint both ops use, so the read op and the write op cannot
    resolve ``root_names``/``siblings`` differently and disagree about the same
    plan. ``text`` is forwarded to ``evaluate_plan`` — see its docstring for why
    the write op supplies it.

    ONE PLAN PER CALL, and that is a budget decision recorded rather than a
    convenience. Measured on this repo's real corpus (422 plans,
    ``docs/plans/*.md``, process time, warm interpreter): a single plan costs
    1.4ms at the median and 182ms at the worst, so one call sits under the 500ms
    brightline with margin. The same scan over the WHOLE corpus costs 5.9s —
    over the bar by an order of magnitude, and 5.9s of a box ~50 peer sessions
    are queued behind. The cost is concentrated in ``read_spine``'s own YAML
    work (3.8s of the 5.9s), which this module consumes and does not own, so
    there is no local fix that makes a corpus sweep an op. A corpus CENSUS is a
    different question with a different budget and it already has a home: DoE's
    ``coordinator/bin/mise-prep-gate.py --tally``, human-invoked, outside the
    per-op budget. Re-measure with
    ``coordinator_core/roadmap/tests/test_prep_gate.py::test_every_real_plan_holds_the_brightline``;
    do not raise the number.
    """
    return evaluate_plan(
        plan_path,
        text=text,
        root_names=repo_root_names(worktree_root),
        siblings=fleet_siblings(worktree_root),
    )


# ---------------------------------------------------------------------------
# The attest, read back
# ---------------------------------------------------------------------------

#: The four-field mise-prep attest, in the order it is written. Declared once
#: here because both ops and the cross-field rule name the same four keys, and
#: three hand-copies of a quartet is how one of them loses a member.
STAMP_FIELDS = (
    "mise_prepped_by",
    "mise_prepped_at",
    "mise_prepped_sha",
    "mise_prepped_findings",
)

#: The four states a consumer resolves, spelled as DoE's consumer contract
#: spells them (coordinator/docs/wiki/mise-prepped-attest.md § Four states).
CERTIFIED = "CERTIFIED"
STALE = "STALE"
UNSTAMPED = "UNSTAMPED"
MALFORMED = "MALFORMED"


def read_stamp(text: str) -> Dict[str, Any]:
    """Resolve the plan's mise-prep attest to exactly one of four states.

    THE PREDICATE IS THE RECOMPUTED SHA, NEVER FIELD PRESENCE. A reader that
    checks presence accepts a certification of a document that no longer exists,
    and in a world where planning runs waves ahead of execution the stamp-to-fire
    window is wide by construction — a stale stamp is the normal case, not an
    edge case.

    STALE and UNSTAMPED are different words on purpose: a reader told "not
    certified" re-stamps, a reader told "the body changed" re-gates, and the
    wrong repair re-stamps a plan whose defects were never re-checked.

    ``mise_prepped_findings: []`` is PRESENT — a declared-empty, structurally
    different from an absent key, exactly as ``writes: []`` is. Treating it as
    absent would report every clean certification MALFORMED.

    The recipe is ``primitives.canonical_body_sha`` — the plan BODY, frontmatter
    excluded, which is what lets a stamp survive its own write and every later
    ``status`` flip. Never ``blitz_land :: _git_blob_sha``, which hashes the
    whole file.
    """
    from coordinator_core.frontmatter.primitives import canonical_body_sha

    fm = plan_frontmatter(text)
    body_sha = canonical_body_sha(text)
    present = [f for f in STAMP_FIELDS if f in fm]
    recorded_sha = fm.get("mise_prepped_sha")
    recorded_sha = str(recorded_sha).strip() if recorded_sha is not None else None
    findings = fm.get("mise_prepped_findings")
    base = {
        "recorded_by": fm.get("mise_prepped_by"),
        "recorded_at": fm.get("mise_prepped_at"),
        "recorded_sha": recorded_sha,
        "findings": findings if isinstance(findings, list) else None,
        "body_sha": body_sha,
    }
    if not present:
        return {"state": UNSTAMPED, **base}
    if len(present) != len(STAMP_FIELDS):
        base["missing"] = [f for f in STAMP_FIELDS if f not in fm]
        return {"state": MALFORMED, **base}
    if recorded_sha and body_sha and recorded_sha.lower() == body_sha.lower():
        return {"state": CERTIFIED, **base}
    return {"state": STALE, **base}
