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
                 ``requires: commit-in-owner-repo`` REFUSES the whole plan;
                 ``requires: landed-work`` withholds only its own ROW.
  PRIME_EXIT     ``prime_exit_criterion`` with a non-empty ``statement`` and a
                 non-empty ``derived_from``, at EVERY size — not only M/L/XL.

Domain vocabulary: the bar, a report class, a verdict (PREPPED / NOT-PREPPED /
REFUSED), a withheld row, a declared-empty.

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

#: The three verdicts, spelled exactly as the read-side twin spells them —
#: consumers on both sides of the repo boundary compare these strings.
PREPPED = "PREPPED"
NOT_PREPPED = "NOT-PREPPED"
REFUSED = "REFUSED"

#: Report-class order. Fixed, because the refusal message enumerates in it and a
#: message whose line order varies per plan is harder to diff than one that does
#: not.
CLASS_ORDER = ("SPINE", "CENSUS", "EXTERNAL_DEPS", "PRIME_EXIT")

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
#: ONE NAME MORE THAN THE READ-SIDE TWIN CARRIES, and the difference is forced.
#: DoE's copy omits its own shortname because an intra-repo blocker is a
#: ``depends_on`` edge, never a gate — so its list cannot name ``DoE-claude``.
#: This module runs over whichever repo the caller stands in, so it carries every
#: fleet name and subtracts the running repo's own at call time
#: (``fleet_siblings``). The two lists are therefore identical for any given
#: corpus, which is the property that matters.
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
    every plan that cites its own tree by name.
    """
    own = repo_root.name
    return tuple(name for name in FLEET_REPOS if name != own)


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

    ROOT-EXISTENCE (``writes:``, ``reads:`` only) — the value's first segment is
    not an entry at this repo's root, so as a repo-relative path it cannot resolve
    here. It does NOT run against ``surface:``, whose schema description admits "a
    single path OR SUBSYSTEM"; reading those values as paths reports prose. Its
    one false-positive shape is a plan legitimately creating a new top-level
    directory — named here rather than assumed away.
    """
    stripped = value.strip()
    if not stripped:
        return None
    for sibling in siblings:
        if stripped == sibling or (
            stripped.startswith(sibling) and not stripped[len(sibling)].isalnum()
        ):
            return f"names {sibling}"
    if field == "surface":
        return None
    first = stripped.replace("\\", "/").split("/")[0]
    if first and first not in root_names:
        return f"first path segment {first!r} does not exist in this repo"
    return None


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
    COMMIT-IN-OWNER-REPO is REFUSED, whole-plan — a hands-off run has no session
    in which to obtain the per-session assent a cross-repo commit needs, so no
    amount of authoring inside this repo clears it. LANDED-WORK withholds its own
    row and nothing else; the plan certifies with that row named as withheld.
    """
    undeclared: List[str] = []
    missing_requires: List[str] = []
    bad_requires: List[str] = []
    commit_gated: List[str] = []
    withheld: List[str] = []

    for row in rows:
        row_id = str(row.get("id") or "<row with no id>")
        gates = row.get("external_gate")
        gates = [g for g in gates if isinstance(g, dict)] if isinstance(gates, list) else []
        for field, value in _row_declared_paths(row):
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
            else:
                withheld.append(row_id)

    if commit_gated:
        return _refuse(
            "cross-repo-commit-gate",
            "; ".join(commit_gated)
            + " — a cross-repo commit needs per-session assent, and a hands-off run has no "
            "session to obtain it in",
            withheld=withheld,
        )
    defects = undeclared + missing_requires + bad_requires
    if defects:
        return _defect("external-dep-undeclared", "; ".join(defects), withheld=withheld)
    if withheld:
        return _pass(
            f"{len(set(withheld))} row(s) withheld on landed-work gates: "
            f"{', '.join(sorted(set(withheld)))}",
            withheld=withheld,
        )
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
    classes = {
        "SPINE": _spine(plan_path, text),
        "CENSUS": _census(fm),
        "EXTERNAL_DEPS": _external_deps(raw_spine_rows(text), root_names, siblings),
        "PRIME_EXIT": _prime_exit(fm),
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
        lines.append(
            "  fix: python coordinator/bin/mise-prep-upgrade.py <plan>  "
            "(derives what the body already declares; never invents a census or a criterion)"
        )
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
