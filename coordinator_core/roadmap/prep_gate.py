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

  SPINE          ``read_spine()`` succeeds, every row carries an executable ``body``
                 (not a title restatement), no row carries ``UNDECLARED`` writes,
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

Four legs (``_created_roots``, the STALE-PREFIX candidate, the archive-write
SPINE check, and refusal collapsing) are ported from that script at DoE-claude
sha ``fbc7bf2bb9f58ef84a11254ef71f0c9391b220f6``; see
``coordinator_core/roadmap/tests/test_prep_gate_four_legs.py`` for one fixture
per leg.

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

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

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

#: A target whose engine call raised something no predicate turns into a DEFECT
#: (a version-skewed engine, a renamed symbol). Computed PER PLAN here (not only
#: at the CLI batch boundary `prep_gate_cli.py` already guarded) so a caller
#: that gates one plan at a time — `coordinator/bin/mise-prep-gate.py :: prep_gate`
#: among them — sees the same ENGINE-ERROR / author-fix distinction the batch
#: door already made. Routes to PM/engineering, never to the plan author.
ENGINE_ERROR = "ENGINE-ERROR"

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
    "project-rag",
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


def repo_nested_names(repo_root: Path, root_names: frozenset) -> Dict[str, tuple]:
    """Second-level directory names in ``repo_root``, mapped to the root entries
    holding them — the STALE-PREFIX candidate for the ROOT-EXISTENCE leg.

    Ported from DoE-claude ``coordinator/bin/mise-prep-gate.py``
    (``_repo_nested_names``) at sha ``fbc7bf2bb9f58ef84a11254ef71f0c9391b220f6``.

    A value like ``cross-repo/inbox/x`` names a first segment this repo does
    not have at its root, but ``cross-repo`` exists one level down under
    ``state/``. That is a path written against a remembered layout, not a
    write into another team's tree, and the two take opposite repairs — one
    moves the prefix, the other declares an ``external_gate``. Reported as an
    undeclared cross-repo dependency, the author goes looking for a gate to
    add and the stale path survives the fix.

    One ``iterdir`` per root entry, read once per scan on the same reasoning
    ``repo_root_names`` gives: the depth is bounded by the root listing, and
    doing it per row would turn a corpus sweep into a stat storm. A name under
    more than one root entry is ambiguous and carries every parent, so the
    detail line offers candidates rather than asserting one.
    """
    nested: Dict[str, list] = {}
    for name in root_names:
        if name.startswith("."):
            continue
        try:
            children = [entry.name for entry in (repo_root / name).iterdir() if entry.is_dir()]
        except OSError:
            continue
        for child in children:
            nested.setdefault(child, []).append(name)
    return {child: tuple(sorted(parents)) for child, parents in nested.items()}


def repo_gitignored_roots(repo_root: Path) -> frozenset:
    """Top-level names ``repo_root``'s own ``.gitignore`` declares untracked —
    the GITIGNORE-ROOT exemption for the ROOT-EXISTENCE leg.

    Bug 8b41ec70da55 (example-cockpit-repo): a row reading
    ``node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts`` was flagged as a
    nameless cross-repo write only because ``pnpm install`` had not run in the
    checkout being scanned — a first segment absent from ``root_names`` for
    install-state reasons, not because it names another team's tree. The
    verdict must not depend on install state, so the discriminant is read off
    the tree's own ``.gitignore`` rather than the directory listing — a
    first segment the repo itself declares it does not track is a
    build/install artifact of THIS repo.

    Literal top-level lines only (no glob expansion, no ``!`` negation, no
    nested-``.gitignore`` walk): the exemption exists for the specific
    "an installed dependency directory is declared ignored at the root"
    shape, and widening it to interpret gitignore glob syntax would let an
    author suppress this leg with a pattern that also happens to match a
    genuine sibling path.
    """
    gitignore = repo_root / ".gitignore"
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    roots: set = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        normalized = stripped.replace("\\", "/").strip("/")
        if normalized and "/" not in normalized:
            roots.add(normalized)
    return frozenset(roots)


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


def _spine(plan_path: Path, text: str, repo_root: Optional[Path] = None) -> Dict[str, Any]:
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
        executable_body,
        read_spine,
    )
    from coordinator_core.ops.dispatch_emit.wave_map import (
        WaveCycleError,
        _compute_held_out,
        _predecessors,
        build_waves,
    )

    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.plan_tasks_render import load_rows

    # A raw substring test over the UNBLANKED body falls through to `read_spine()`
    # for a plan whose only ```yaml plan-tasks``` token lives inside an HTML
    # comment -- `coordinator-doc-new` scaffolds every new plan with exactly this
    # shape as documentation. `load_rows`/`locate_fenced_block` already blank
    # comments before matching, so the same reader used for EXTERNAL_DEPS'
    # `raw_spine_rows` decides presence here too, instead of a second, cheaper,
    # comment-blind guess.
    if load_rows(text).status is LocateStatus.ABSENT:
        return _defect(
            "spine-absent",
            "no ```yaml plan-tasks block — a plan with no spine declares no scope to schedule",
        )
    try:
        rows = read_spine(plan_path)
    except SpineReadError as exc:
        return _defect(type(exc).__name__, str(exc).strip().splitlines()[0][:300])
    # A row held out of the emit already -- because an earlier row's `epistemic-premise`
    # depends_on edge decides its writes, directly OR transitively through a chain of
    # non-epistemic-premise edges -- declares nothing a fire-time driver must resolve, since
    # `dispatch_emit.emit`/`build_waves` never requires its writes either. Ported from DoE-claude
    # `coordinator/bin/mise-prep-gate.py`'s `_spine` (code-reviewer Finding 1,
    # 2026-09-08-hoexec-close/mise-prep-gate.md): a direct-edge-only check misses route 2 of
    # `_compute_held_out`'s walk, so the full transitive predecessor graph is passed here too.
    held = _compute_held_out(rows, _predecessors(rows))
    undeclared = [row.id for row in rows if row.writes is UNDECLARED and row.id not in held]
    if undeclared:
        return _defect(
            "writes-undeclared",
            f"rows with no writes: {', '.join(undeclared)} "
            "(a row that writes nothing declares `writes: []`; a row whose files an earlier "
            "row decides declares an epistemic-premise `depends_on` edge instead)",
        )
    try:
        waves = build_waves(rows)
    except WaveCycleError as exc:
        return _defect("wave-cycle", str(exc).strip().splitlines()[0][:300])
    no_body = [row.id for row in rows if not executable_body(row.title, row.body)]
    if no_body:
        return _defect(
            "body-absent",
            f"rows with nothing to execute: {', '.join(no_body)} "
            "(a row's `body:` states the work, not only its title)",
            withheld=no_body,
        )
    archive_defect = _archive_writes_refused_in_wave(rows)
    if archive_defect is not None:
        return archive_defect
    shape_defect = _writes_shape_refused_at_emit(rows, repo_root)
    if shape_defect is not None:
        return shape_defect
    unroutable, first = _unroutable_rows(waves)
    if unroutable:
        return _defect(type(first).__name__, str(first).strip()[:300], withheld=unroutable)
    return _pass(f"{len(rows)} dispatchable row(s) across {len(waves)} wave(s)")


def _archive_writes_refused_in_wave(rows: List[Any]) -> Optional[Dict[str, Any]]:
    """A dispatched row writing under ``archive/`` is BLOCKED in-wave: the
    engine's ``block_subagent_archive_write`` refuses every subagent write
    there outside its carve-outs, so the row's executor cannot land it.

    Restated from DoE-claude ``coordinator/bin/mise-prep-gate.py``'s
    ``writes-archive-refused-in-wave`` leg (2026-09-11, reported by
    example-store-repo-fb, whose mise run halted on a chunk writing
    ``archive/specs/...``). The guard's own allow-predicates are CALLED, not
    restated; operator rows never reach here, since ``read_spine`` excludes
    them before this function's caller sees ``rows``.

    Checks BOTH ``writes:`` (concrete paths) and ``writes_under:`` (prefixes,
    for names chosen at run time — what archiving a resolved memo into a
    dated directory does). The guard's carve-outs are file-shaped
    (``.../<date>.md$``) and cannot be evaluated against a directory prefix,
    which is not a gap in this check: a prefix declares the NAMES are chosen
    later, so nothing here can know whether they will land inside a carve-out,
    and the guard refuses in-wave whenever they do not.
    """
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
    from coordinator_core.write_guards.block_subagent_archive_write import (
        refuses_path as _guard_refuses,
    )

    archive_writes = [
        f"{row.id} ({value})"
        for row in rows
        if row.writes is not UNDECLARED
        for value in row.writes
        if _guard_refuses(str(value))
    ]
    archive_writes += [
        f"{row.id} ({prefix}, writes_under)"
        for row in rows
        for prefix in getattr(row, "writes_under", ()) or ()
        if _guard_refuses(str(prefix))
    ]
    if not archive_writes:
        return None
    return _defect(
        "writes-archive-refused-in-wave",
        f"rows writing under archive/: {', '.join(archive_writes)[:260]} — "
        "block_subagent_archive_write refuses these to every dispatched executor. Fix: "
        "mark the row `execution_mode: operator` so the EM applies it, or move the write "
        "out of archive/. A value tagged `writes_under` names a PREFIX, so the guard's "
        "file-shaped carve-outs cannot be checked against it — concretize into `writes:` "
        "if the row only ever writes carve-out-shaped names.",
    )


def _writes_shape_refused_at_emit(
    rows: List[Any], repo_root: Optional[Path]
) -> Optional[Dict[str, Any]]:
    """A live row whose ``writes:`` entry is glob- or directory-shaped
    certifies here and is refused later, at ``dispatch.emit`` time:
    ``inventory_mint.py``'s ``_refuse_if_glob``/``_refuse_if_directory_shaped``
    raise on exactly these two shapes when minting a spine, and
    ``pathspec.py``'s ``DirectoryShapedWriteError`` (via ``_declared_paths``)
    raises the trailing-separator case again when an already-authored spine
    reaches emit. A bare path naming an existing directory passes emit and
    halts at the emitted workflow's claimability preflight instead, after an
    executor is spent (claude-klabauter#45 class B): its scoped-commit
    pathspec would stage every unrelated dirty file beneath it. Every one of
    these refusals fires AFTER this bar has already stamped
    ``mise_prepped_*`` — moving the check here catches it before the stamp,
    not after (example-retrieval-repo, 2026-09-07: 12 certified rows carried a
    directory-shaped write; DR-*-terminal-test-phase-refuse-vs-omit.md
    carried a bare glob).

    Glob detection reuses ``inventory_mint._GLOB_CHARS`` (``*``, ``?``,
    ``[``) rather than a second guess at the character set. Directory-shape
    detection reuses ``inventory_mint._refuse_if_directory_shaped``'s own
    two-spelling rule (trailing ``/``/``\\``, OR an existing directory on
    disk at ``repo_root`` — never ``pathspec.py``'s narrower trailing-
    separator-only check, which is deliberately NOT an on-disk test because
    spine derivation there must not depend on worktree state; THIS bar runs
    at authoring/certification time, when reading the worktree is exactly
    the point).

    Only rows ``read_spine`` already scheduled (open disposition, not
    deferred) reach here — a closed row is never dispatched, so nothing it
    declares is ever resolved by a driver.
    """
    from coordinator_core.ops.dispatch_emit.inventory_mint import _GLOB_CHARS
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

    root = repo_root or Path.cwd()
    # Two kinds, not one: a TRAILING SEPARATOR (`/` or `\`) is `pathspec.py`'s own
    # `DirectoryShapedWriteError` shape and is refused earliest, at spine-derivation
    # time -- its own kind (`writes-directory-shaped`) names that specifically. A
    # glob or a BARE path that merely happens to name an existing directory on this
    # worktree is caught later, at the emitted workflow's claimability preflight
    # (claude-klabauter#45 class B) -- `writes-unreadable-at-emit`. Distinguished
    # because the fix line is the same for both but the failure surface differs,
    # and a caller keying on `kind` must be able to tell which check actually fired.
    directory_shaped: List[str] = []
    unreadable: List[str] = []
    for row in rows:
        if row.writes is UNDECLARED:
            continue
        for value in row.writes:
            text = str(value)
            if any(ch in text for ch in _GLOB_CHARS):
                unreadable.append(f"{row.id} ({text!r}, glob pathspec)")
                continue
            if text.endswith("/") or text.endswith("\\"):
                directory_shaped.append(f"{row.id} ({text!r}, directory-shaped)")
            elif (root / text).is_dir():
                unreadable.append(f"{row.id} ({text!r}, directory-shaped)")
    fix = (
        "Fix: name the files this row actually writes, or declare `writes_under: <dir>/` "
        "if the row chooses the filename at run time. Do not strip the trailing separator "
        "and leave a bare directory name — that clears this check and then strands the "
        "wave at the same directory-shaped write, one step later."
    )
    if directory_shaped:
        return _defect(
            "writes-directory-shaped",
            f"rows with a directory-shaped writes: entry (trailing separator): "
            f"{', '.join(directory_shaped)} — pathspec.py refuses this at spine-derivation "
            f"time. {fix}",
        )
    if unreadable:
        return _defect(
            "writes-unreadable-at-emit",
            f"rows with a glob or an existing-directory writes: entry: {', '.join(unreadable)} "
            "— the dispatch path refuses both shapes (inventory_mint.py at emit; an existing "
            f"directory at the emitted workflow's preflight). {fix}",
        )
    return None


def _unroutable_rows(waves: Sequence[Sequence[Any]]) -> "tuple[List[str], Optional[Exception]]":
    """The rows the emitter would refuse to route, judged by the emitter itself.

    ``emit._row_agent_type`` raises one of ``emit.ROW_ROUTING_ERRORS`` on a
    static spine fact. Calling it here means a plan the gate certifies is one
    the emitter will route (DoE-claude#75).
    """
    from coordinator_core.ops.dispatch_emit import emit

    ids: List[str] = []
    first: Optional[Exception] = None
    for wave in waves:
        for row in wave:
            try:
                emit._row_agent_type(row)
            except emit.ROW_ROUTING_ERRORS as exc:
                ids.append(row.id)
                first = first or exc
    return ids, first


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
    field: str,
    value: str,
    root_names: frozenset,
    siblings: Sequence[str],
    created_roots: frozenset = frozenset(),
    nested_names: Optional[Dict[str, tuple]] = None,
    gitignored_roots: frozenset = frozenset(),
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

    ``created_roots`` exempts a first segment this plan's own spine declares
    CREATING (``writes_under: <segment>/``, or a bare ``writes: <segment>``).
    Without it the leg fires hardest on exactly the plans whose job is to bring a
    new top-level directory into existence, and the only way through is an
    ``external_gate`` that would be a lie — no external party, no closure
    evidence, nothing to wait for. Measured by example-game-workbench-repo-b8: a
    workspace-skeleton plan refused 35 times across C1-C5 on ``ide/``, the
    directory it exists to create, whose author correctly declined to fabricate
    the gate. Ported from DoE c36c45dd0a, which owns the twin.

    This is the ONE exemption read off a declaration rather than off the value's
    own shape, and the distinction holds: ``writes_under: ide/`` is SPELLED, not
    claimed — an author who writes it falsely has under-declared their own
    writes, which is the defect this bar exists to catch, rather than asserted a
    private intention no reader can check. Deliberately narrow to the two
    spellings that name a DIRECTORY: reading the first segment off any ``writes:``
    path would exempt ``coordinator_core/ops/x.py`` too, since a path's first
    segment is always its own row's first segment, and writing deeper would buy
    an exemption. The laundering residual is identical IN KIND to the bare-segment
    one above.
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
    if _is_settings_home_path(normalized):
        return None
    first = normalized.split("/")[0]
    if first and first in created_roots:
        return None
    if first and first in gitignored_roots:
        # GITIGNORE-ROOT (bug 8b41ec70da55): a first segment the SCANNED repo's
        # own `.gitignore` declares untracked is a build/install artifact of
        # THIS repo (an uninstalled `node_modules/`, e.g.), not a nameless path
        # into a sibling's tree -- and the verdict must not depend on whether
        # that dependency happens to be installed in the checkout being
        # scanned. See `repo_gitignored_roots` for why this reads the tree's
        # own declaration rather than a directory listing.
        return None
    if first and first not in root_names:
        # STALE-PREFIX candidate (ported from DoE-claude ``mise-prep-gate.py``
        # ``_repo_nested_names`` at sha ``fbc7bf2bb9f58ef84a11254ef71f0c9391b220f6``):
        # a first segment absent from the root but present one level down is a
        # path written against a remembered layout, and its repair is to move
        # the prefix — not to declare an ``external_gate``, which is what a
        # bare "leaves the repo" verdict sends the author to do. The candidate
        # is named, never substituted: a value that does not resolve is
        # defective either way and only the author knows which repair it meant.
        parents = (nested_names or {}).get(first) or ()
        if parents:
            candidates = " or ".join(f"{parent}/{first}/" for parent in parents)
            return (
                f"first path segment {first!r} does not exist at this repo's root, but "
                f"{candidates} does — a stale path prefix, not a cross-repo write"
            )
        return f"first path segment {first!r} does not exist in this repo"
    return None


_SETTINGS_HOME_PREFIXES = (
    "~/.coordinator-claude-settings/",
    "$COORDINATOR_SETTINGS_HOME/",
    "${COORDINATOR_SETTINGS_HOME}/",
    "%COORDINATOR_SETTINGS_HOME%/",
    "$env:COORDINATOR_SETTINGS_HOME/",
)


def _is_settings_home_path(normalized: str) -> bool:
    """A path rooted at the machine-local settings home, which belongs to no repo.

    The ROOT-EXISTENCE leg catches a nameless path into another team's tree; the
    settings home is not one. Same bar as DoE-claude ``mise-prep-gate.py``
    (``ff446da1b``), so a plan the authoring gate passes is one this stamp passes.
    The discriminant is read off the value's own spelling, never an author flag.

    Negative-spec: NOT a general "outside the repo is fine" — every other path
    the leg reports still reports; the trailing separator keeps a sibling
    directory sharing the stem out.
    """
    lowered = normalized.strip().strip('"').strip("'")
    folded = lowered.casefold()
    return any(folded.startswith(p.casefold()) for p in _SETTINGS_HOME_PREFIXES)


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

    ``surface:`` rides alongside ``writes:``/``reads:``/``reads_at_head:``/
    ``consumes:`` because it is where the corpus actually names cross-repo
    work. A gate reading only the array fields would call a plan clean on the
    strength of the field its author did not use. ``reads_at_head``/
    ``consumes`` are read exactly as ``reads`` is here -- the schema refuses a
    row mixing ``reads`` with either, so a row never carries more than one of
    the three, and each is reported under its own field name (DoE parity,
    commit 92ca01682).
    """
    out: List[tuple] = []
    for key in ("writes", "reads", "reads_at_head", "consumes"):
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


def _created_roots(rows: List[Dict[str, Any]]) -> frozenset:
    """Top-level directory names this spine declares it CREATES.

    Only the two spellings that name a DIRECTORY rather than a file inside one:
    a `writes_under:` entry, and a bare single-segment `writes:` entry. A
    multi-segment path is not read — see `_path_leaves_repo`'s `created_roots`
    paragraph for why depth must buy nothing here.
    """
    created: set = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        under = row.get("writes_under")
        values = list(under) if isinstance(under, list) else []
        writes = row.get("writes")
        if isinstance(writes, list):
            values += writes
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = value.strip().replace("\\", "/").strip("/")
            if normalized and "/" not in normalized:
                created.add(normalized)
    return frozenset(created)


def _ungated_reads(row: Dict[str, Any], row_id: str) -> "tuple[dict, list]":
    """Validated ``external_reads_ungated`` entries for ``row``, as
    ``({(path, owner_repo_folded): entry}, findings)``.

    APM ruling (mirrors DoE-claude ``coordinator/bin/mise-prep-gate.py``): an
    entry clears a SIBLING-NAME/ROOT-EXISTENCE hit on ``reads:`` only, never
    ``writes:``/``surface:`` — three shapes are refused here rather than
    silently ignored, each its own message because each names a different
    repair: (1) a path this row WRITES, so the field cannot launder a
    cross-repo write as an examined read; (2) a path absent from this row's
    ``reads:``, a stale acknowledgment; (3) a blank ``reason`` — the schema
    types ``reason`` ``minLength: 1``, but this gate reads raw YAML, not a
    schema-validated document, so the check is restated here the same way
    ``_external_deps`` already restates ``requires:``'s enum against raw
    text.
    """
    entries = row.get("external_reads_ungated")
    entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    # DoE parity, commit 92ca01682: reads_at_head/consumes clear exactly as
    # reads does -- the schema refuses a row mixing reads with either, so a
    # row never carries more than one of the three.
    reads_set: set = set()
    for _reads_key in ("reads", "reads_at_head", "consumes"):
        _reads_value = row.get(_reads_key)
        if not isinstance(_reads_value, list):
            _reads_value = []
        reads_set.update(str(v).strip() for v in _reads_value if isinstance(v, str))
    writes = row.get("writes")
    if not isinstance(writes, list):
        writes = []
    writes_set = {str(v).strip() for v in writes if isinstance(v, str)}
    writes_under = row.get("writes_under")
    if not isinstance(writes_under, list):
        writes_under = []
    writes_under_prefixes = [str(v) for v in writes_under if isinstance(v, str)]

    index: Dict[tuple, Dict[str, Any]] = {}
    findings: List[str] = []
    for i, entry in enumerate(entries):
        path = str(entry.get("path") or "").strip()
        owner_repo = str(entry.get("owner_repo") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if not path:
            findings.append(f"{row_id}: external_reads_ungated[{i}] has no path")
            continue
        if path in writes_set or any(path.startswith(prefix) for prefix in writes_under_prefixes):
            findings.append(
                f"{row_id}: external_reads_ungated[{i}] names {path!r}, which this row WRITES — "
                "the field only acknowledges a read, it never clears a write; declare a write "
                "with external_gate instead"
            )
            continue
        if path not in reads_set:
            findings.append(
                f"{row_id}: external_reads_ungated[{i}] names {path!r}, which is not in this "
                "row's reads: — a stale acknowledgment"
            )
            continue
        if not reason:
            findings.append(
                f"{row_id}: external_reads_ungated[{i}] ({path!r}) has an empty reason"
            )
            continue
        index[(path, owner_repo.casefold())] = entry
    return index, findings


def _matched_sibling(value: str, siblings: Sequence[str]) -> Optional[str]:
    """The SIBLING-NAME match ``_path_leaves_repo`` would report for ``value``, or
    None. Restated rather than returned from ``_path_leaves_repo`` (whose contract
    is a bare reason string, not a structured match) so ``_ungated_reads``
    correlation can key on the same sibling identity that function's own
    ``names {sibling}`` reason names — same fold/separator rule, not a second
    independent guess at it.
    """
    stripped = value.strip()
    if not stripped:
        return None
    folded = stripped.casefold()
    for sibling in siblings:
        key = sibling.casefold()
        if folded == key or (folded.startswith(key) and not folded[len(key)].isalnum()):
            return sibling
    return None


def _gate_covers_reason(value: str, siblings: Sequence[str], gates: List[Dict[str, Any]]) -> bool:
    """Does ANY of ``gates`` actually cover the sibling ``value`` names?

    Finding 1 (code-reviewer, 2026-09-08-hoexec-close/mise-prep-gate.md): a row
    writing into ``claude-klabauter`` carrying an ``external_gate`` naming a
    DIFFERENT sibling (``example-retrieval-repo``) used to silence the undeclared-path
    defect on the strength of gate PRESENCE alone. Attaching an unrelated gate
    must be no better than attaching none. Correlated by ``owner_repo``,
    case-folded, against the SIBLING-NAME match — the same identity
    ``_matched_sibling`` already gives ``_ungated_reads``'s correlation, not a
    second independent guess at it.

    A ROOT-EXISTENCE hit (no sibling name — a nameless path into an
    unidentified tree) cannot be correlated this way, since there is no
    ``owner_repo`` to compare against; ANY gate still clears it there, matching
    this leg's own acknowledged blind spot on that shape.
    """
    sibling = _matched_sibling(value, siblings)
    if sibling is None:
        return bool(gates)
    target = sibling.casefold()
    return any(str(g.get("owner_repo") or "").strip().casefold() == target for g in gates)


def _external_deps(
    rows: List[Dict[str, Any]],
    root_names: frozenset,
    siblings: Sequence[str],
    nested_names: Optional[Dict[str, tuple]] = None,
    gitignored_roots: frozenset = frozenset(),
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
    created_roots = _created_roots(rows)
    undeclared: Dict[str, int] = {}
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
        ungated_index, ungated_findings = _ungated_reads(row, row_id)
        for finding in ungated_findings:
            undeclared[finding] = undeclared.get(finding, 0) + 1
        for field, value in _row_declared_paths(row):
            if field != "surface" and _path_is_unresolved_placeholder(value):
                placeholders.append(
                    f"{row_id}: {field} {value.strip()!r} is an unreplaced placeholder, "
                    "not a path (no external_gate clears it)"
                )
                continue
            reason = _path_leaves_repo(
                field, value, root_names, siblings, created_roots, nested_names, gitignored_roots
            )
            # external_reads_ungated clears a reads: hit only — never writes:/surface: —
            # per the APM ruling this field exists to serve. Keyed on (path,
            # case-folded owner_repo) against the matched sibling identity, mirroring
            # DoE's SIBLING-NAME correlation; a value with no sibling match (the
            # ROOT-EXISTENCE leg) is not cleared by this field, matching DoE's own
            # acknowledged blind spot there.
            if reason and field in ("reads", "reads_at_head", "consumes"):
                sibling = _matched_sibling(value, siblings)
                if sibling is not None:
                    key = (value.strip(), sibling.casefold())
                    if key in ungated_index:
                        reason = None
            if reason and not _gate_covers_reason(value, siblings, gates):
                # One line per DISTINCT fact, with a count. This leg reports a first
                # SEGMENT, so a row writing eleven files under one new directory
                # produced eleven byte-identical lines and one plan produced 35
                # (example-game-workbench-repo's idex-02). A defect restated once per value
                # reads as that many defects and buries the repair under them.
                line = f"{row_id}: {field} {reason}, no external_gate"
                undeclared[line] = undeclared.get(line, 0) + 1
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

    collapsed = [
        line if count == 1 else f"{line} (x{count})"
        for line, count in undeclared.items()
    ]
    defects = collapsed + missing_requires + bad_requires
    if placeholders:
        # Its own kind, not folded into external-dep-undeclared: the repair
        # differs — one replaces a stand-in with the path it stands for, the
        # other adds a gate — and a tally that names only the second sends the
        # author to the wrong fix.
        return _defect(
            "path-placeholder", "; ".join(placeholders + defects), withheld=withheld
        )
    if defects:
        detail = "; ".join(defects)
        if any("first path segment" in line for line in collapsed):
            # Name the spelling rather than the rule. A plan whose job is to create
            # the directory the refusal names has one correct repair and one lie
            # available, and an author told only "declare it" reaches for the lie.
            detail += (
                " — if this plan CREATES the top-level directory a refusal names, "
                "declare it where this check reads: `writes_under: <segment>/` on "
                "the row that creates it. Writing files deeper inside it does not "
                "declare that the tree gains a root"
            )
        return _defect("external-dep-undeclared", detail, withheld=withheld)
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


#: Matches ``prime_exit_criterion.derived_from`` values this rung will even
#: attempt to resolve — a sizing path under ``state/sizings/``. Anything else
#: (a goal KR, free prose, a foreign-repo sentinel) is out of this rung's
#: business and keeps today's verdict, per Design § Inheritance, gate side.
_SIZING_DERIVED_FROM_RE = re.compile(r"^state/sizings/.+\.ya?ml$")


def _read_sizing_exit_criterion(repo_root: Path, derived_from: str) -> Optional[Dict[str, Any]]:
    """The cited sizing's ``exit_criterion`` mapping, or ``None``.

    Deliberately does NOT import ``ops/deliverable_cascade`` (Design § Inheritance,
    gate side): that module pulls in ``ipc``, ``claim_state``, ``git_native`` and
    ``locked_write`` at module level, which would invert the roadmap→ops layering
    and add a cold-import cost to every ``gate_plan`` call. This helper is a local,
    guarded ``yaml.safe_load`` instead — no side effects, and any failure (missing
    file, path escaping ``repo_root``, unparsable YAML, non-mapping content) is a
    silent ``None``, because this rung is a no-op on anything it cannot resolve:
    never a DEFECT and never an exception.
    """
    if not _SIZING_DERIVED_FROM_RE.match(derived_from):
        return None
    try:
        root = repo_root.resolve()
        candidate = (root / derived_from).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    try:
        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    criterion = loaded.get("exit_criterion")
    return criterion if isinstance(criterion, dict) else None


def _falsifier_shape_defect(
    fm: Dict[str, Any], repo_root: Optional[Path]
) -> Optional[str]:
    """The falsifier's OWN authoring-time SHAPE defects -- misnesting and a
    non-sha-shaped `baseline_ref` -- read through the SAME predicates
    `close_out_and_stamp._evaluate_goal_falsifier_gate` judges them by
    (`_falsifier_misnested`, `_falsifier_block`, the same size/date gate),
    reused rather than restated so this bar and the terminal close-out gate
    cannot disagree about the same plan (gh-klabauter#71, F23b).

    Deliberately NARROWER than that gate: both defects here are checkable
    from the plan's OWN bytes alone -- a `falsifier:` key nested one level
    too shallow, or a `baseline_ref` that is not even sha-shaped, are wrong
    regardless of whether the plan has executed. `_verify_baseline_ref`'s
    own git-ancestry half (does the sha exist, does `HEAD` reach it) is
    NOT reused here -- this module is a ZERO-SPAWN bar by contract (see its
    own module docstring's Budget section), and a git spawn per corpus plan
    is exactly the cost that contract exists to keep off this path.
    `exit_criterion_met` absence and a non-`pass` verdict are not reused
    either -- both describe an OBSERVATION made after the work runs, which
    this pre-execution bar has nothing to observe yet.

    `None` on anything the goal gate itself would decline on (no
    `prime_exit_criterion`, grandfathered, S/XS, a taken exemption, a
    correctly-nested well-formed falsifier) -- never raises, never spawns."""
    if repo_root is None:
        return None
    try:
        # `close_out_and_stamp.falsifier_shape` -- NOT `close_out_and_stamp`
        # itself: that module's top-level imports pull in git commit
        # machinery, ceremony, and session plumbing, which is fine for a
        # terminal once-per-close-out gate but not for this per-plan,
        # ZERO-SPAWN corpus hot path. See `falsifier_shape.py`'s own module
        # docstring for the measured brightline regression that import
        # caused. `close_out_and_stamp.py` imports these SAME names back
        # (re-export) -- this is the one definition, not a second copy.
        from coordinator_core.execute_plan_assemble.falsifier_shape import (
            _BASELINE_REF_CROSS_REPO_RE,
            _DISPOSITION_REF_SHA_RE,
            _falsifier_block,
            _falsifier_exemption,
            _falsifier_misnested,
            _plan_created_on_or_after_grandfather,
            _plan_is_m_plus,
        )
    except Exception:
        return None
    prime = fm.get("prime_exit_criterion")
    if not isinstance(prime, dict):
        return None
    try:
        misnested = _falsifier_misnested(fm)
        falsifier = _falsifier_block(prime)
        effective = falsifier if falsifier is not None else (fm.get("falsifier") if misnested else None)
    except Exception:
        return None

    # Cheap, I/O-free candidates first (pure dict/regex reads): the common
    # case -- a correctly-nested, well-shaped falsifier, or none declared at
    # all -- exits here on every one of a corpus's plans without ever
    # touching disk. `_plan_is_m_plus` below reads a file, and this bar is a
    # ZERO-SPAWN, per-plan corpus hot path (module Budget section) -- paying
    # that read on every prime_exit-carrying plan, defect or not, is what
    # pushed the real-corpus worst case over the 500ms brightline the first
    # time this predicate ran unconditionally
    # (`test_every_real_plan_holds_the_brightline`, 2026-09-27). Deferred
    # until a candidate defect exists to gate.
    candidates: List[str] = []
    if misnested:
        candidates.append(
            "plan declares a top-level falsifier: key as a sibling of "
            "prime_exit_criterion instead of nesting it under "
            "prime_exit_criterion.falsifier"
        )
    if isinstance(effective, dict):
        baseline_ref = effective.get("baseline_ref")
        if isinstance(baseline_ref, str) and baseline_ref.strip():
            stripped = baseline_ref.strip()
            if not _DISPOSITION_REF_SHA_RE.match(stripped) and not _BASELINE_REF_CROSS_REPO_RE.match(
                stripped
            ):
                candidates.append(
                    "prime_exit_criterion.falsifier.baseline_ref is not a "
                    f"resolvable sha shape: {baseline_ref!r}"
                )
    if not candidates:
        return None

    # A candidate exists -- NOW pay for the size/date gate (one file read)
    # to decide whether it is actually in scope, same bounds the goal gate
    # itself uses: a taken exemption, an S/XS plan, or one created before
    # the grandfather date never refuses on these, regardless of what its
    # falsifier looks like.
    try:
        if _falsifier_exemption(prime) is not None:
            return None
        if not _plan_created_on_or_after_grandfather(fm):
            return None
        if not _plan_is_m_plus(fm, repo_root):
            return None
    except Exception:
        return None
    return "; ".join(candidates)


def _prime_exit(fm: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
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

    A second, additive rung (Design § Inheritance, gate side / C5): when
    ``derived_from`` resolves under ``repo_root`` to a ``state/sizings/*.yaml``
    record whose ``exit_criterion.accepted`` is non-null, the plan's statement
    must match it (modulo whitespace normalisation) or the verdict is
    ``prime-exit-diverges-from-sizing``. An unresolvable path, a foreign-repo
    path, or a sizing with no or unaccepted ``exit_criterion`` leaves the verdict
    exactly as it was before this rung existed — this rung never turns a PASS
    into an exception, and it is itself a no-op: never a DEFECT on its own account
    beyond the one divergence case above.
    """
    criterion = fm.get("prime_exit_criterion")
    if not isinstance(criterion, dict):
        return _defect(
            "prime-exit-absent",
            "no prime_exit_criterion (required at every size, not only M/L/XL)",
        )
    statement = criterion.get("statement")
    if not str(statement or "").strip():
        return _defect("prime-exit-empty", "prime_exit_criterion carries no statement")
    if is_placeholder(statement):
        return _defect(
            "prime-exit-placeholder",
            "prime_exit_criterion.statement is still a scaffold placeholder "
            "(replace the <REPLACE: ...> marker with the falsifiable sentence)",
        )
    derived_from = criterion.get("derived_from")
    if not str(derived_from or "").strip():
        return _defect(
            "prime-exit-underived",
            "prime_exit_criterion has no derived_from (a link, not a self-declaration)",
        )
    if is_placeholder(derived_from):
        return _defect(
            "prime-exit-placeholder",
            "prime_exit_criterion.derived_from is still a scaffold placeholder "
            "(replace it with the sizing object or goal KR it derives from)",
        )
    if repo_root is not None:
        sizing_exit = _read_sizing_exit_criterion(repo_root, str(derived_from).strip())
        if isinstance(sizing_exit, dict) and isinstance(sizing_exit.get("accepted"), dict):
            sizing_statement = str(sizing_exit.get("statement") or "")
            if " ".join(str(statement).split()) != " ".join(sizing_statement.split()):
                return _defect(
                    "prime-exit-diverges-from-sizing",
                    "prime_exit_criterion.statement differs from the accepted "
                    f"exit_criterion on {derived_from} (plans inherit an accepted "
                    "criterion; they do not re-author it)",
                )
    shape_defect = _falsifier_shape_defect(fm, repo_root)
    if shape_defect is not None:
        return _defect("prime-exit-falsifier-shape", shape_defect)
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
#:
#: Anchored to the field path's top-level segment (Review: coordinator:code-
#: reviewer) — not a substring match, which would also suppress an unrelated
#: field whose name merely happened to contain "mise_prepped".
_SCHEMA_STAMP_FIELD_PREFIX = "mise_prepped"


def _is_stamp_field_error(error: Mapping[str, Any]) -> bool:
    """True when every field an error names belongs to the mise-prep stamp quartet.

    THIS GATE MUST NOT REFUSE OVER THE STAMP IT IS ABOUT TO WRITE. The quartet
    is engine-written metadata, not plan content, so a half-written stamp has
    to stay repairable BY re-stamping -- otherwise the damage blocks its own
    repair path and the plan is bricked out of certification forever.

    The predicate this replaces could never fire (2026-09-18). It compared
    `field.split(".")[0]` against the bare prefix `"mise_prepped"`, but these
    fields are underscore-suffixed (`mise_prepped_by`, `mise_prepped_at`, ...)
    and carry no dot at all, so `split(".")[0]` returned the WHOLE name and the
    comparison was false for every stamp field that has ever existed -- dead
    code written for a dotted-path shape the schema does not use. On top of
    that, `_cf_mise_prepped_stamp_quartet` reports its `field` as a
    COMMA-JOINED list of the missing members (`", ".join(missing)`), which no
    single-name comparison could match either.

    Net effect while it was dead: a plan carrying a partial hand-written
    quartet was graded SCHEMA/DEFECT and `plan.stamp_prepped` refused it
    NOT-PREPPED, telling the author to hand-correct engine-owned fields -- the
    exact outcome the exemption exists to prevent.

    Prefix-matched over every comma-separated member so both shapes resolve,
    and deliberately ALL-of rather than ANY-of: an error naming a stamp field
    alongside a genuine plan-content field is a real defect and must survive.
    """
    raw = str(error.get("field") or "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return False
    return all(n.split(".")[0].startswith(_SCHEMA_STAMP_FIELD_PREFIX) for n in names)


def _schema(
    fm: Dict[str, Any], prime_exit: Dict[str, Any], parse_error: Optional[str] = None
) -> Dict[str, Any]:
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

    F24b: ``parse_error``, when given, means the PLAN's own frontmatter --
    not the schema -- failed to parse. Reported as the parse error itself and
    nothing else: a schema walk over ``plan_frontmatter``'s fail-safe ``{}``
    would otherwise report every required field missing, which is a true
    but misleading restatement of "this YAML did not parse at all".
    """
    if parse_error is not None:
        return _defect("frontmatter-parse-error", parse_error)
    try:
        from coordinator_core.frontmatter.schema_validate import validate_frontmatter

        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
    except Exception:
        return _pass("not checked: plan.schema.json is unreadable beside this engine")
    errors = [e for e in errors if not _is_stamp_field_error(e)]
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

    A YAML parse failure and a genuinely EMPTY/absent frontmatter block both
    collapse to ``{}`` here on purpose — every existing predicate over the
    returned mapping (``_census``, ``_prime_exit``, ...) needs nothing more
    than "no fields to read". A caller that needs to tell the two apart (F24b,
    gh-klabauter#71: a YAML parse error was reported as "title/created/author/
    status: required field missing", which sent an author hunting for four
    fields that were never the problem) reads ``frontmatter_parse_error``
    instead, which is the ONLY place that distinction is checked.
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


def frontmatter_parse_error(text: str) -> Optional[str]:
    """The frontmatter's own YAML parse error, with the parser's line/column,
    or ``None`` when it parses (or there is no frontmatter block at all --
    that is a SPINE/absent-block concern, never a parse error).

    F24b (gh-klabauter#71): wherever this bar reports a defect derived from
    an UNPARSEABLE frontmatter, it must say so as a parse error, not as
    "required field X missing" -- the schema walk over ``plan_frontmatter``'s
    fail-safe ``{}`` cannot tell a plan that never declared ``title:`` from
    one whose YAML a stray tab or an unclosed quote broke, and reported both
    the same way. This reads the same bytes a SECOND time only to recover
    that ONE distinction; ``plan_frontmatter`` itself stays the single
    parse-or-empty reader every other predicate already depends on.
    """
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        yaml.safe_load(split.fm_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            location = f"line {mark.line + 1}, column {mark.column + 1}"
        else:
            location = "unknown location"
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return f"YAML parse error at {location}: {problem}"
    return None


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
    repo_root: Optional[Path] = None,
    nested_names: Optional[Dict[str, tuple]] = None,
    gitignored_roots: frozenset = frozenset(),
) -> Dict[str, Any]:
    """The whole bar over one plan. Returns a report; writes nothing.

    ``text`` is the plan's bytes when the caller already holds them — the write
    op holds them under a lock and must gate the exact document it is about to
    stamp, or the recorded sha certifies a body the bar never saw. Omitted, the
    file is read here.

    ``repo_root`` is forwarded to SPINE's directory-existence rung
    (``_writes_shape_refused_at_emit``) — omitted, it falls back to the
    process cwd, matching ``inventory_mint.py``'s own convention for a
    caller with no worktree root at hand.
    """
    if text is None:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    fm = plan_frontmatter(text)
    parse_error = frontmatter_parse_error(text)
    try:
        prime_exit = _prime_exit(fm, repo_root)
        classes = {
            "SPINE": _spine(plan_path, text, repo_root),
            "CENSUS": _census(fm),
            "EXTERNAL_DEPS": _external_deps(
                raw_spine_rows(text), root_names, siblings, nested_names, gitignored_roots
            ),
            "PRIME_EXIT": prime_exit,
            "SCHEMA": _schema(fm, prime_exit, parse_error),
        }
    except Exception as exc:  # noqa: BLE001 - defense in depth, see ENGINE_ERROR
        # A predicate raising anything OTHER than its own documented exception
        # (SpineReadError et al, already turned into a DEFECT above) is a
        # version-skewed engine, not an authoring gap -- reported as its own
        # status ("ERROR") so the verdict below routes it to ENGINE_ERROR
        # rather than NOT_PREPPED, the same distinction the CLI batch loop
        # already makes at `prep_gate_cli.py :: _engine_error_report`.
        kind = type(exc).__name__
        detail = f"{kind}: {exc}".strip().splitlines()[0][:300]
        classes = {
            "SPINE": {"status": "ERROR", "kind": "engine-error", "detail": detail, "withheld": []}
        }
    if any(v["status"] == "ERROR" for v in classes.values()):
        verdict = ENGINE_ERROR
    elif any(v["status"] == "REFUSE" for v in classes.values()):
        verdict = REFUSED
    elif any(v["status"] == "DEFECT" for v in classes.values()):
        verdict = NOT_PREPPED
    else:
        verdict = PREPPED
    withheld = sorted({r for v in classes.values() for r in v["withheld"]})
    status = str(fm.get("status") or "").strip()
    terminal = status in _terminal_statuses()
    return {
        "path": plan_path.as_posix(),
        "verdict": verdict,
        "withheld_rows": withheld,
        "classes": classes,
        "terminal": terminal,
        "message": refusal_message(plan_path, verdict, classes, withheld, terminal=terminal, status=status),
    }


def _terminal_statuses() -> frozenset:
    """Plan statuses meaning "already ran" -- prep is a pre-execution property.

    Imported, never hand-listed: the plans this bar reports as owing nothing
    must be exactly the ones the archive op moves out of `docs/plans/`, or the
    two readers disagree about which plans are still live.
    """
    from coordinator_core.lifecycle_constants import PLAN_ARCHIVABLE_STATUS

    return frozenset(PLAN_ARCHIVABLE_STATUS)


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


def _only_schema_defect(classes: Dict[str, Any]) -> bool:
    """True when SCHEMA is the only class not passing.

    The discriminator for which repair line to print. Scoped to SCHEMA ALONE
    on purpose: a plan that also misses a census or a prime_exit_criterion has
    derivable work the converter really can do, and routing it away from the
    converter over a co-occurring shape error would cost more than it saves.
    """
    failing = [k for k, v in classes.items() if v["status"] != "PASS"]
    return failing == ["SCHEMA"]


#: `EXTERNAL_DEPS`'s two declaration paths, the repair menu `_authoring_fix_lines`
#: offers for it. Kept as one place so the CLI's per-class refusal detail and this
#: catalogue cannot name a field the other has renamed out from under it.
def _authoring_fix_lines(failing_classes: "Sequence[str] | set") -> List[str]:
    """Repair lines for a SET of failing class names -- a catalogue, not a
    per-report message. Used where a caller wants the standing repair menu for
    a class (e.g. to check its wording stays current) independent of any one
    plan's report.

    `EXTERNAL_DEPS` names both live declaration paths: `external_gate` for a
    row whose write/read genuinely leaves the repo, `external_reads_ungated`
    (APM ruling) for a row that only EXAMINES a sibling path and never lands
    into it, and `writes_under: [<segment>/]` for a row that CREATES a new
    top-level directory rather than depending on one.
    """
    lines: List[str] = []
    if "EXTERNAL_DEPS" in failing_classes:
        lines.append(
            "  fix: declare `external_gate` on the row for a path that genuinely leaves "
            "this repo, or `external_reads_ungated` (path/owner_repo/reason) for a row "
            "that only reads a sibling path without landing into it; a row CREATING a "
            "new top-level directory declares `writes_under: [<segment>/]` instead of a "
            "gate"
        )
    if "CENSUS" in failing_classes:
        lines.append(
            "  fix: declare `census: []` if this plan rests on no counted premise, or "
            "one entry per counted claim with question/command/result all filled in"
        )
    if "PRIME_EXIT" in failing_classes:
        lines.append(
            "  fix: declare `prime_exit_criterion.statement` and `.derived_from` "
            "(a sizing object or goal KR, never a self-declaration)"
        )
    return lines


def refusal_message(
    plan_path: Path,
    verdict: str,
    classes: Dict[str, Any],
    withheld: Sequence[str],
    *,
    terminal: bool = False,
    status: str = "",
) -> str:
    """ONE message enumerating every missing declaration at once.

    A bar that reports its failures one at a time makes an author iterate through
    four round trips to learn one thing, each trip re-reading a plan that has not
    changed. Register: one fact per line, the terse alternative where one exists,
    and no override key — there is no way to pass this bar except by declaring
    what it names.

    Prep is a pre-execution property, and `terminal` (a plan whose `status:` is
    already archivable, see `_terminal_statuses`) says so instead of a fix line:
    reported 2026-09-11 by example-store-repo-fb — four agents, ~900k tokens, authoring
    census rows and exit criteria for 28 plans that had already shipped, because
    every reader took the NOT-PREPPED count as a work queue. The verdict itself
    stays unchanged on purpose — the whole-corpus denominator measures bar
    ADOPTION — only the refusal stops reading as work owed.
    """
    name = plan_path.name
    if verdict == ENGINE_ERROR:
        lines = [f"mise-prep: {ENGINE_ERROR} — {name}"]
        for key, value in classes.items():
            if value["status"] != "ERROR":
                continue
            lines.append(f"  {key:<14} {value['detail']}")
        lines.append("  route: PM/engineering — an engine defect, not an authoring gap.")
        return "\n".join(lines)
    if verdict == PREPPED:
        tail = f" ({len(withheld)} row(s) withheld: {', '.join(withheld)})" if withheld else ""
        return f"mise-prep: PREPPED — {name}{tail}"
    lines = [f"mise-prep: {verdict} — {name}"]
    for key in CLASS_ORDER:
        value = classes.get(key)
        if value is None or value["status"] == "PASS":
            continue
        lines.append(f"  {key:<14} {value['detail']}")
    if verdict == REFUSED:
        lines.append("  route: PM, not the plan author.")
    elif terminal:
        lines.append(
            f"  status: {status} — this plan has already run; nothing is owed here "
            "(prep is a pre-execution property)."
        )
    elif any(
        v["status"] == "DEFECT" and v["kind"] == "prime-exit-underived" for v in classes.values()
    ):
        # No admissible value exists for THIS plan: `derived_from`'s pattern admits
        # only a sizing object, a blitz-trail artifact, or a goal KR, and a
        # pre-sizing-regime plan has none of the three. mise-prep-upgrade DOES
        # derive this field when a sizing object exists, so pointing the author at
        # it here would have them run it, see "0 would change", and reach for the
        # nearest goal KR next -- an answer that does not actually support the
        # statement. Named here rather than left for the author to discover.
        lines.append(
            "  fix: mise-prep-upgrade would report 0 would change for this field — it "
            "derives derived_from only from an existing sizing object. Do not point it "
            "at the nearest goal KR unless that KR genuinely derives this statement; "
            "raise a schema question about what a pre-sizing-regime plan should cite "
            "instead"
        )
    elif _only_schema_defect(classes):
        # The converter DERIVES missing declarations from the plan's own body.
        # It cannot repair a value that is present and the wrong SHAPE, so for
        # a schema-only refusal it writes nothing and reports `0 would be
        # written` — a repair line pointing at a no-op, which costs the author
        # the run it takes to discover that. (example-retrieval-repo, 2026-09-11: ran
        # `--upgrade` across the whole refused set and got exactly that.)
        lines.append(
            "  fix: correct the named field(s) in the plan's frontmatter by hand — "
            "mise-prep-upgrade derives missing declarations and cannot repair a "
            "value that is present and the wrong shape. An engine-written field "
            "(execution_authorized_*) has a producer and is repaired there instead, "
            "never by hand here"
        )
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
    root_names = repo_root_names(worktree_root)
    return evaluate_plan(
        plan_path,
        text=text,
        root_names=root_names,
        siblings=fleet_siblings(worktree_root),
        repo_root=worktree_root,
        nested_names=repo_nested_names(worktree_root, root_names),
        gitignored_roots=repo_gitignored_roots(worktree_root),
    )


class CorpusInputs:
    """The per-corpus facts every predicate reads, resolved ONCE per repo root
    rather than once per plan — the same reasoning `gate_plan` already gives for
    computing them per call. A caller gating many plans in the same repo root
    (a directory walk, a corpus tally) derives this once and reuses it, instead
    of re-``iterdir``-ing the repo root once per plan.
    """

    __slots__ = ("repo_root", "root_names", "siblings", "nested_names", "gitignored_roots")

    def __init__(
        self,
        repo_root: Path,
        root_names: frozenset,
        siblings: tuple,
        nested_names: Dict[str, tuple],
        gitignored_roots: frozenset = frozenset(),
    ):
        self.repo_root = repo_root
        self.root_names = root_names
        self.siblings = siblings
        self.nested_names = nested_names
        self.gitignored_roots = gitignored_roots


def corpus_inputs(repo_root: Path) -> CorpusInputs:
    """``CorpusInputs`` for ``repo_root`` — the read-once twin of ``gate_plan``'s
    own per-call derivation, for a caller gating more than one plan standing in
    the same repo.
    """
    root_names = repo_root_names(repo_root)
    return CorpusInputs(
        repo_root=repo_root,
        root_names=root_names,
        siblings=fleet_siblings(repo_root),
        nested_names=repo_nested_names(repo_root, root_names),
        gitignored_roots=repo_gitignored_roots(repo_root),
    )


def gate_plan_with_corpus(plan_path: Path, corpus: CorpusInputs, *, text: Optional[str] = None) -> Dict[str, Any]:
    """``evaluate_plan`` against an already-resolved ``CorpusInputs`` — the
    plan-first, corpus-second calling shape a directory walk wants, alongside
    ``gate_plan``'s repo-root-first, per-call shape.
    """
    return evaluate_plan(
        plan_path,
        text=text,
        root_names=corpus.root_names,
        gitignored_roots=corpus.gitignored_roots,
        siblings=corpus.siblings,
        repo_root=corpus.repo_root,
        nested_names=corpus.nested_names,
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
