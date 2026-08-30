"""
coordinator_core.baton_assemble -- the `baton-assemble` computed-skill engine.

Purpose: computes the SHARED id-inheritance/lineage cascade that `handoff/SKILL.md`
and `spinoff/SKILL.md` both hand-walk today into ONE `kind`-flagged decision object
per the frozen contract. `kind` selects the parent-discovery order and the
companion-id field(s) a baton carries -- everything else (directive naming,
judgment-point surfacing, the envelope shape) is shared.

kind=handoff: parent-discovery order is plan -> predecessor -> mint. Companion id:
    `predecessor_id` (C2 add-not-swap ID-companion for the `predecessor:` path field).
kind=spinoff: parent-discovery order is stub -> plan -> mint. Companion ids:
    `origin_handoff_id` + `origin_session` / `origin_plan_id` / `origin_goal_id`
    (the ratified spinoff-provenance-ancestry shape,
    cross-repo/inbox/2026-07-07-spinoff-provenance-claude-klabauter-ratified.md).

COMPOSES the Tier-B contract (DR-092) -- does NOT copy pickup_assemble's field
shapes. The 8-key envelope and judgment-point constructors are imported from
`coordinator_core.contract.decision_object` (envelope.py / judgment.py); only the
per-domain routing (which directives/judgment-points a `kind` produces) lives here.

directives[] name EXISTING atomic CLIs/ops rather than reimplementing them:
    coordinator-doc-new (scaffold), lint-frontmatter.py (frontmatter lint),
    coordinator_core.ops.dirty_tree_gate (dirty-tree-gate),
    coordinator_core.ops.extract_scope_paths (+ apply_base.scoped_commit),
    coordinator/bin/session-claim-cli, coordinator_core.ops.handoff_archive_transition.

kind=handoff's d6 (2026-07-27, computed-skills-b4 plan C1 -- the push-side
succession writer): fires ONLY when this brief's own `lineage["predecessor"]`
is not None -- i.e. a continuation, never a fork -- and composes
`handoff.archive_transition` mode="supersede" (apply.py's
`_dispatch_handoff_supersede_predecessor`) to stamp the PREDECESSOR
`continued` + `continued_into:<this successor>` and archive it, in the SAME
transaction as this successor's own mint. Fixes the pathology where minting a
continuation baton left its predecessor non-terminal forever (76/91 DoE
batons). The discriminator is the plain `kind == "handoff" and
lineage["predecessor"] is not None` predicate -- NOT the
`j-continuation-vs-fork` judgment point below, which stays untouched
SKILL-prose residue for a different (upstream) question.

kind=handoff's d3 slot deliberately does NOT name
`coordinator_core.ops.handoff_phase_stamp` ("handoff.stamp_phase") --
that op is real and registered (mirrors handoff.stamp's shape; see its own
module docstring), but it is VESTIGIAL in this specific calling context: d1
(coordinator-doc-new) already scaffolds every new session-handoff with
`handoff_phase: continuation` stamped unconditionally, and this brief's
lineage resolution has no execution-phase/governing_plan input to ever ask the
op to stamp anything else -- a re-stamp of "continuation" onto a handoff
that already carries "continuation" is always the op's own idempotent
no-op. Emitting it here was dead weight that (via a SEPARATE bug --
`baton_assemble.apply._invoke_op_in_process` calling
`coordinator_core.ipc.get_op_handler` with no import trigger, so the
registry was empty for it in a fresh process) surfaced as a hard abort
("unrecognized op 'handoff.stamp_phase'") rather than a harmless no-op.
2026-07-25 break-class fix: removed the directive; the op remains fully
registered/dispatchable for its real caller (an execution-authorization
workflow supplying `phase="execution"` + `governing_plan`, per
docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md), which
is not this module.
d1's `--out` (2026-07-27, bug backlog
`2026-07-27-baton-assemble-handoff-brief-computes-a-fe36a5dea88e.yaml`): is a
COMPUTED fresh `state/handoffs/<date>-<slug>.md` path (`_compute_fresh_output_path`,
threaded via `lineage["output_path"]`), NEVER `artifact_path` echoed
verbatim -- kind-agnostic, covering BOTH kind=handoff and kind=spinoff.
`artifact_path` is the caller-supplied INPUT lineage source in both kinds
(the plan being handed off, in the sanctioned plan->execute
execution-handoff trigger, or the predecessor handoff this session opened
with, for kind=handoff; the origin handoff/stub/plan this spinoff forks
from, for kind=spinoff) -- `resolve_lineage` only ever READS it. Echoing an
EXISTING input into d1's `--out` destroyed it the moment d1 fired
(`coordinator-doc-new`'s `--out` write is an unconditional overwrite, no
existence check): the reproduced live break passed a just-PM-authorized
plan, carrying `execution_authorized_*` stamps, as `artifact_path`, and d1
came back set to scaffold a blank handoff directly over it.
`_assert_no_directive_writes_over_input` is a GENERAL, existence-gated
backstop over the whole `directives[]` table (not hand-pinned to d1, not
hand-pinned to kind=handoff) that fails loud on any future `--out=<input>`
collision against an artifact that already exists on disk -- gating on
existence is what lets both kinds' bare-slug mint convention (where
`output_path` is legitimately identical to `artifact_path`, since there is
no pre-existing file at that path to destroy) coexist with the qualified-
existing-input case without special-casing caller shape. Scoped to the
`--out=` flag shape specifically so it does not false-positive on d2's
`--file <path>` lint target (which, since the 2026-08-03 fix, names d1's
COMPUTED output rather than the input -- see `_build_directives`'s d2 block)
or d6's own `successor_path`
argument (bare positional, not `--out=`). d6's `successor_path` itself was
ALSO fixed 2026-07-27, in a separate follow-up, to thread
`lineage["output_path"]` rather than the INPUT `artifact_path` -- see
`_build_directives`'s d6 block for the full corruption-shape rationale.

IDEMPOTENT REPLAY (2026-07-29 break-class fix): `directives[].already_satisfied`
is DERIVED FROM DISK here, not hardcoded False, so re-running the identical
`apply` invocation after a partial abort is the resume path -- no
`--continue`/`--resume` flag, no persisted run-state file (that would be a second
source of truth for facts the artifacts already carry). Two pieces, both in this
module: `_resume_recorded_successor_path` pins `lineage["output_path"]` to the
successor a prior attempt already recorded on the predecessor, and
`_build_directives`'s d1 block marks the scaffold satisfied when that path is
already a file. Everything else in the envelope converges through predicates that
ALREADY EXIST and stays `already_satisfied: False` on purpose -- d2's lint is
read-only, d5's `release-artifact` is holder-identity-checked and no-ops to
success, and d6's supersede converges through `_supersede_continued`'s OWN
byte-identical no-op branch. Deriving a second "has this landed?" predicate
beside any of those is the named anti-pattern here; see each directive's own
comment for its single definition.

PRE-d6 ABORT (2026-07-30, closing the residue row the paragraph above left open).
`_resume_recorded_successor_path` reads PREDECESSOR-side evidence, which only
exists once d6 has run; an abort at d2/d4/d5 leaves none. Two changes close it:
`_compensate_d1_scaffold` (apply.py) now asks whether the survivor carries
operator content -- re-rendering `coordinator-doc-new`'s own template and
comparing bytes -- instead of asking whether `--title` was supplied, which was a
proxy for the wrong fact and preserved untouched scaffolds; and for the residue
that legitimately survives (real operator content),
`_adopt_prior_attempt_scaffold_path` identifies it via its OWN `predecessor:`
pointer and pins `lineage["output_path"]` to it, so the re-run re-uses that file
rather than minting beside it. That read of successor-side evidence is admitted
by DR-242 Amendment A1 (`docs/decisions/DR-242-successor-named-child-is-not-
evidence-of-succ.md` § 6, PROPOSED) for exactly ONE decision -- which path d1
writes -- and grants no succession conclusion: d6's own gate re-derives
`claimed_or_shipped` independently and is unchanged.

BARE-SLUG REPLAY (2026-08-25 break-class fix, bug backlog
`2026-08-25-spinoff-brief-then-apply-mints-two-batons-and-adopts-the-stub-as-
origin.yaml`). The IDEMPOTENT REPLAY machinery above reaches convergence through
PREDECESSOR-side evidence, which kind=spinoff never writes (`predecessor: none`
by design) -- so re-running an identical `apply spinoff <slug>` minted a SECOND
pickup_ready baton beside the first, and, because `resolve_lineage` read
whatever occupied the mint path BEFORE asking whether the caller had supplied a
bare slug at all, stamped that abandoned first attempt onto the survivor as its
own `origin_handoff`. Both halves close on one ordering rule: a bare-slug
`artifact_path` is the OUTPUT, never a lineage input, whoever occupies it.
`_adopt_prior_attempt_mint_path` then re-uses this run's own prior attempt as
`output_path` (d1's existing existence predicate makes it `already_satisfied`
from there), and `_assert_no_directive_writes_over_input` skips satisfied
directives, which by definition write nothing.

kind=spinoff's artifact-authoring directive DEFAULTS to DISPATCHING the existing
live claude-klabauter op `handoff.author_fork` (coordinator_core/ops/handoff_author_fork.py,
registered op name "handoff.author_fork") rather than reimplementing spinoff
authoring inline -- this is PENDING the claude-klabauter-em seam decision (dispatch /
supersede / coexist, C0); this module does NOT assert supersession of
`author_fork`, it only names it as the directive's dispatch target.

Judgment residue is NEVER auto-fired here: the Step 0 self-honesty gate, the
PM-authorization gate, continuation-vs-fork, and the dirty-tree case-c decision
stay SKILL-prose judgment calls (collapsed onto this module's callers in chunks
C4/C5) -- this module surfaces each as a `judgment_points[]` entry built via
`build_untrusted_gate_judgment_point` (no `recommendation` parameter exists on
that constructor -- structurally impossible to attach a verdict here).

CONSUMES B0's shared resolver: `coordinator_core.resolution.facade.
resolve_operator_config()` for settings_home/claude_klabauter_root/doe_root -- this module
does NOT define its own local `_settings_home()`.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md,
chunk C1

Negative-spec:
    - Do NOT add a mutating code path here -- every mutation is a `directives[]`
      entry naming an existing atomic CLI/op; this module only reads disk/git
      state and constructs the decision object.
    - Do NOT hand-roll a parallel 8-key envelope or judgment-point dict literal
      -- route every construction through `decision_object.envelope.build_envelope`
      / `.judgment.build_judgment_point` / `.build_untrusted_gate_judgment_point`.
    - Do NOT define a second satisfaction predicate for a directive whose
      dispatch target already owns one (d5's claim-holder check, d6's
      `_supersede_continued` idempotency branch) -- `already_satisfied` exists
      to skip a handler that would DESTROY something, not to reimplement the
      convergence a composed op already performs.
    - Do NOT rest `already_satisfied` (or `output_path` resumption) on a
      successor's own `predecessor:`/`origin_handoff:` field -- DR-242: a
      successor-named child is not evidence of succession.
    - Do NOT assert that `handoff.author_fork` is superseded by this module --
      C0's claude-klabauter-em seam decision is still open; the spinoff directive merely
      names the op as its dispatch target.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from coordinator_core import dag
from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.contract.decision_object.envelope import build_envelope
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)
from coordinator_core.contract.residue_segments import (
    SegmentLoadError,
    load_segments,
    select_segments,
)
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.ceremony.completion_entry import _slug_from_title as _title_slug
from coordinator_core.ops.deliverable_carry import (
    DivergentDeliverableIdError,
    resolve_deliverable_and_initiative,
)
from coordinator_core.ops.dirty_tree_gate import parse_porcelain_paths
from coordinator_core.ops.mint_deliverable_id import mint as _mint_deliverable_id
from coordinator_core.sizing_disposition import real_id
from coordinator_core.ops.session_commits import resolve_session_commits
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session_baton.store import merge_baton
from coordinator_core.ops.read_frontmatter_field import (
    read_frontmatter_field as _read_frontmatter_field,
)
from coordinator_core.pickup_assemble import compute_repo_identity_gate  # C3: foreign-repo gate
from coordinator_core.resolution.facade import resolve_operator_config
from coordinator_core.resolve_coordinator_clone import (
    ResolveCoordinatorCloneError,
    resolve_content_root,
)
from coordinator_core.session.claimed_plan import resolve_claimed_plan_path
from coordinator_core.session.touch_record import project_live_claims
from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exit-code contract (locally scoped to this CLI -- see contract's own
# § Exit-code contract; not inherited from pickup_assemble's).
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

KINDS = ("handoff", "spinoff")

_NO_CONSOLE = no_console_creationflags()


class BriefResult(NamedTuple):
    decision_object: dict[str, Any]
    exit_code: int


class TransportFailure(Exception):
    """Raised for a repo-root resolution failure -- the trampoline's own
    transport failure, mirroring pickup_assemble's `_TransportFailure`."""


# ---------------------------------------------------------------------------
# Small git/filesystem helpers -- no shell, no bash, subprocess-argv only.
# ---------------------------------------------------------------------------


def resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Resolve the enclosing git worktree root for `start` (default cwd).

    NEGATIVE-SPEC — the `show_toplevel` hop is load-bearing, not a formality.
    `cwd = start or Path.cwd()` is the exact shape audited in
    `state/audits/2026-08-26-session-hub-writers-path-resolution.md` as the
    one that reaches a repo no caller named; this instance is safe ONLY
    because `show_toplevel` re-anchors `cwd` to the worktree it is actually
    inside via an in-process upward filesystem walk -- WALKS ONLY, never
    spawns git (see `git/repo_root.py::show_toplevel`). Building a path by
    joining onto `cwd` directly -- or "simplifying" this to return `cwd`
    when the walk is cheap to skip -- reintroduces that defect silently,
    and nothing warns. A third shape reintroduces it just as silently:
    re-anchoring on `Path(__file__).resolve().parents[n]` instead of `cwd`
    still returns *a* repo root, just the wrong one -- the engine's own
    rather than the caller's -- and is the one live defect the audit found
    (`bash_guards/dispatch_checks.py::_bt_python3_invocation_cache_path`).
    """
    cwd = start or Path.cwd()
    top = show_toplevel(str(cwd))
    return Path(top) if top else None


def _read_frontmatter(path: Path) -> str:
    if not path.is_file():
        return ""
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    return split.fm_text if split else ""


_FM_BOUNDED_READ_CHUNK = 4096


def _read_frontmatter_bounded(path: Path) -> str:
    """Same return contract as `_read_frontmatter` (frontmatter text or
    `""`), but reads `path` in growing `_FM_BOUNDED_READ_CHUNK`-byte
    increments instead of the whole file, stopping as soon as
    `split_frontmatter` finds a closing `---` in what has been read so far.

    `_scan_deliverable_collision` (2026-08-21 budget fix, C13) is the sole
    caller: it opens every non-excluded file under `state/handoffs/` (170+
    on this box, ~10KB average body, some 38KB+) purely to read a ~10-line
    frontmatter block it then discards -- `_read_frontmatter`'s
    `path.read_text()` pulls the entire body across that walk for no reason.
    Frontmatter lives at the top of the file by construction, so bounding
    the read to what is needed to reach the closing delimiter (typically
    well under one chunk) cuts the walk's bytes-read by the body/frontmatter
    ratio without changing which candidates match -- same parser
    (`split_frontmatter`), same frontmatter text out, just less of the file
    pulled off disk to get it.

    A malformed or delimiter-less file falls through to reading the whole
    file (the loop keeps growing the buffer until `fh.read()` returns
    empty), matching `_read_frontmatter`'s existing `None`-splits-to-`""`
    behaviour for that case -- no new failure mode, only a bounded read on
    the common (frontmatter-bearing) path.
    """
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read(_FM_BOUNDED_READ_CHUNK)
            while True:
                split = split_frontmatter(text)
                if split is not None:
                    return split.fm_text
                more = fh.read(_FM_BOUNDED_READ_CHUNK)
                if not more:
                    return ""
                text += more
    except OSError:
        return ""


def _resolve_qualified_path_or_raise(artifact_path: str, root: Path, kind: str = "") -> Path:
    """Resolve a QUALIFIED (non-bare-slug) `artifact_path` that is absent at
    its named on-disk location to the swept-archive copy of the same file,
    or fail loud when no copy exists anywhere this repo's boot sweep would
    have moved it to.

    2026-07-28 break-class fix: before this function existed, a caller-named
    `artifact_path` that did not exist on disk -- the reproduced live case is
    the predecessor handoff this session was opened with, already swept to
    `archive/handoffs/` by the boot sweep -- resolved SILENTLY:
    `_read_frontmatter` returns `""` for a missing path, indistinguishable
    from the bare-slug mint convention's legitimate "nothing here yet" case
    (see `resolve_lineage`'s `was_bare_slug` guard, which is what routes a
    caller here in the first place -- this function is never reached for the
    mint shape). That silent empty-frontmatter read starved `resolve_lineage`'s
    `own_handoff_id` discriminator of the predecessor's `handoff_id`, which
    cascaded into d6 (`handoff.supersede_predecessor`) never being emitted --
    exactly the stranding defect d6 exists to close (see module docstring).

    Reuses `coordinator_core.ops.resolve_swept_baton._find_first_match` --
    the SAME archive-dir list (`cross-repo/archive/`, `archive/handoffs/`,
    `archive/completed/`) and `rglob` recursion `/pickup`'s own
    `baton.resolve_swept_in_archive` op already searches -- rather than
    re-deriving a second copy of that walk (see `artifact_basename.py`'s own
    module docstring naming `_find_first_match` as the one shared shape any
    new resolver should call, not copy).

    Raises `ValueError` naming the path and the archive dirs searched when
    the file resolves nowhere. Callers reach this function ONLY for a
    qualified path that is already confirmed missing at its live location
    (see `resolve_lineage`), so "resolves nowhere" here always means a
    genuinely wrong/stale/deleted caller-supplied path -- never the
    bare-slug mint convention, which never calls this function at all.
    """
    from coordinator_core.ops.resolve_swept_baton import _ARCHIVE_SUBDIRS, _find_first_match

    match = _find_first_match(root, Path(artifact_path).name)
    if match is None:
        searched = ", ".join(_ARCHIVE_SUBDIRS)
        # design-as-offers (project CLAUDE.md): for kind="handoff" the cheapest
        # correct move is not "guess a better path" but "supply no path at all"
        # -- `_resolve_held_handoff_for_session` reads the predecessor off the
        # durable claim ledger, which is authoritative where a hand-typed path
        # is a guess. Leading with the better alternative rather than the
        # violation; the prior wording named only the guess-again path, so a
        # caller who had just guessed wrong was steered into guessing again.
        offer = (
            " -- omit the artifact-path entirely and it self-resolves from this "
            "session's held handoff claim, or pass the correct predecessor path"
            if kind == "handoff"
            else " -- pass the correct predecessor path"
        )
        raise ValueError(
            f"baton_assemble: artifact-path {artifact_path!r} does not exist at its "
            f"named location under {root} and no swept copy was found searching "
            f"{searched}{offer}, or confirm the artifact was deleted rather than "
            "archived."
        )
    return match


def _fm_field(fm: str, key: str) -> Optional[str]:
    return read_fm_field_unquoted(fm, key)


# ---------------------------------------------------------------------------
# id-inheritance / lineage cascade -- the shared extraction target both
# handoff and spinoff hand-walk today (SKILL.md's D1 carry-not-remint rule).
# ---------------------------------------------------------------------------


def _repo_rel_handoff_path(basename: str) -> str:
    """`state/handoffs/<basename>` as a POSIX-separated repo-relative path --
    the ONE constructor for every such path this module computes.

    2026-08-07 break-class fix (Windows-first-class). These paths do not stay
    internal: `lineage["output_path"]` becomes d1's `--out`, d2's lint target,
    d6's `exclude`, and -- durably -- the PREDECESSOR's `continued_into:`
    frontmatter, the succession edge d6 exists to write. `str(Path("state") /
    "handoffs" / x)` renders `state\\handoffs\\x.md` on Windows, so a baton
    minted here authored a backslash-separated edge into a tracked file that
    every consumer (and every peer platform in a fleet whose `~/.claude` is
    synced with a Mac) resolves as posix. Every OTHER path this module hands
    back is already `.as_posix()`-normalized (`_resolve_qualified_path_or_raise`,
    the predecessor/additional-predecessor resolution, `_fm_path_value`) -- this
    was the sole native-separator holdout, and it was the one that reached
    frontmatter.

    Negative-spec: takes a BASENAME, never a path fragment -- a caller with a
    directory component of its own does not belong here.
    """
    return (Path("state") / "handoffs" / basename).as_posix()


def _normalize_artifact_path(artifact_path: str) -> str:
    """Normalizes a bare slug -- no path separator, no `.md` extension --
    into `state/handoffs/<YYYY-MM-DD>-<slug>.md`. Anything that already
    LOOKS like a path (contains `/` or `\\`, or already ends `.md`) passes
    through UNCHANGED -- existing callers pass fully-qualified paths and
    must not regress.

    Fixes a reproduced live break: `baton-assemble apply spinoff
    windows-host-validation-review-assemble-seam` scaffolded an
    extensionless file named literally `windows-host-validation-review-
    assemble-seam` at the repo ROOT and committed it there, because
    `_build_directives`'s `--out`/lint-target/claim-plan args are a pure
    passthrough of this CLI's second positional arg with no directory/date/
    extension synthesis anywhere in the module. A bare slug IS the
    documented calling convention (`spinoff/SKILL.md`'s argument-hint is
    `"<slug> [optional one-line title]"`), so the caller is not wrong -- the
    engine was silently assuming a fully-qualified path.

    Applied here, at the single point `lineage["artifact_path"]` is
    established -- kind-agnostic, since this runs before the
    `if kind == "handoff": ... else:` branch below, so every `kind` in
    `KINDS` is normalized uniformly rather than special-cased for spinoff.
    `brief()` also threads the result back into its own `artifact.path` (not
    just `lineage["artifact_path"]`), so every consumer of this value --
    d1's `--out`, the path d2 lints, d3/d5's args, and the envelope's own
    top-level `artifact.path` -- sees the SAME resolved string. Normalizing
    at only one of those call sites would desync it from the others (see
    `_build_directives`'s own comment on why `--out` exists at all).
    """
    if not artifact_path:
        return artifact_path
    if "/" in artifact_path or "\\" in artifact_path or artifact_path.endswith(".md"):
        return artifact_path
    date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return _repo_rel_handoff_path(f"{date_str}-{artifact_path}.md")


def _resolve_additional_predecessor_paths(
    paths: Optional[list[str]], root: Path, kind: str
) -> list[str]:
    """Resolve every fan-in `additional_predecessor_paths` entry to a stable,
    directly-openable, repo-relative path -- the SAME normalize -> is_file ->
    archive-aware-fallback -> relative_to treatment `resolve_lineage` already
    applies to its primary `predecessor` rung, extracted here (sedge-01,
    `succession-edge-cardinality` roadmap, R2) so it can be called ONCE, ahead
    of the `kind == "handoff"` deliverable-id cascade, instead of resolving
    twice per path (once for the cascade's own divergence check, once for
    `lineage["additional_predecessors"]`).

    A path missing at its named live location is routed through
    `_resolve_qualified_path_or_raise` -- fail-loud, unchanged by this
    extraction: an unreadable/archived additional-predecessor path already
    failed loud here before the hoist, and still does.
    """
    resolved: list[str] = []
    for _extra_path in paths or []:
        _extra_normalized = _normalize_artifact_path(_extra_path)
        _extra_fm_path = Path(_extra_normalized)
        if not _extra_fm_path.is_absolute():
            _extra_fm_path = root / _extra_normalized
        if not _extra_fm_path.is_file():
            # Missing at its named live location -- archive-aware fail-loud
            # resolution, identical to the primary predecessor.
            _extra_fm_path = _resolve_qualified_path_or_raise(_extra_normalized, root, kind)
        try:
            resolved.append(_extra_fm_path.relative_to(root).as_posix())
        except ValueError:
            resolved.append(str(_extra_fm_path))
    return resolved


def _compute_fresh_output_path(
    artifact_path: str, root: Optional[Path] = None, title: Optional[str] = None
) -> str:
    """Computes a NEW `state/handoffs/<date>-<slug>.md` path for d1's scaffold
    target -- deliberately DIFFERENT from `artifact_path` itself. Kind-agnostic:
    used for BOTH kind="handoff" (the plan being handed off, or the predecessor
    handoff this session opened with) and kind="spinoff" (the origin
    handoff/stub/plan this spinoff forks from) whenever `artifact_path` is a
    qualified/existing path rather than the bare-slug mint shape.

    `artifact_path` is the caller-supplied INPUT lineage source in both
    kinds -- `resolve_lineage` only ever READS it (deliverable_id/initiative/
    predecessor for handoff; origin_handoff_id/origin_session/etc. for
    spinoff). Nothing in this module's contract requires d1 to write BACK to
    that same path, and `coordinator-doc-new`'s `--out` write is an
    unconditional overwrite (`open(out_path, "w", ...)`, no existence check)
    -- echoing an existing input into `--out` silently destroys it the
    moment d1 fires.

    Fixes the reproduced live break: `baton-assemble brief handoff
    docs/plans/2026-07-26-priority-ledger.md` (the plan->execute case) came
    back with d1's `--out` set to that SAME plan path -- firing d1 verbatim
    would have scaffolded a blank handoff over a just-PM-authorized plan
    carrying `execution_authorized_*` stamps. See bug backlog
    `2026-07-27-baton-assemble-handoff-brief-computes-a-fe36a5dea88e.yaml`.

    Slug derivation: `artifact_path`'s basename, extension stripped, with
    any leading `YYYY-MM-DD-` date prefix ALSO stripped (plans, predecessor
    handoffs, and origin handoffs are all date-prefixed by convention) --
    then RE-DATED to today, so a new artifact written today never collides
    with the artifact it derives from even when both happen to share a slug.

    Same-day-chain collision (2026-07-27 follow-up, break-class regression
    THIS re-dating introduced): when `artifact_path` is a handoff already
    dated TODAY -- routine in this fleet, where multiple handoffs per day
    on the same chain are the common case, not an edge case -- stripping
    and re-adding today's date is a no-op, so the plain `<date>-<slug>.md`
    candidate collides with the input itself.
    `_assert_no_directive_writes_over_input` (correctly) then refuses the
    whole brief rather than silently overwriting it -- see
    `brief handoff state/handoffs/2026-07-27-priority-ledger-execute.md`
    for the reproduction. The FIX belongs here, in the derivation, not in
    weakening that guard: when `root` is supplied and the plain candidate
    already exists on disk, this function disambiguates by inserting an
    `HHMMSS` timestamp -- `state/handoffs/<date>_<HHMMSS>_<slug>.md` --
    the SAME `<date>_<HHMMSS>_...` shape already used fleet-wide for
    same-day handoff chains (see `handoff_author_fork._fork_handoff_filename`
    and the many `state/handoffs/<date>_<HHMMSS>_*.md` files already on
    disk), rather than inventing a new scheme. If even THAT collides
    (pathological: two disambiguations in the same second), a numeric
    `-2`, `-3`, ... suffix is appended and re-checked until a free path is
    found -- deterministic, no uuid, never silently overwrites a
    DIFFERENT existing handoff either.

    `root` is optional (defaults to `None`, skipping the existence check
    entirely) because `_build_directives`'s own defensive fallback call
    has no `root` in scope and some direct unit tests construct a
    `lineage` dict by hand with no `root`-bearing caller above them --
    those callers get the pre-collision-check candidate unchanged, which
    is correct for their scope (they are not exercising the collision
    path). Every REAL caller (`resolve_lineage`) has `root` and passes it.

    Deliberately idempotent on an ALREADY-today-dated `state/handoffs/`
    path when that path does NOT YET EXIST on disk (the bare-slug
    normalization case, `_normalize_artifact_path`, which stamps a bare
    slug with TODAY's date before this function ever sees it, producing a
    FRESH mint target with nothing on disk to collide with): stripping
    today's date prefix and re-adding it is a no-op, so a bare-slug
    `brief handoff|spinoff <slug>` call keeps producing the SAME single
    path for both d1's `--out` and d2's lint target -- no behaviour change
    for that existing, already-tested calling convention. The distinction
    from the same-day-chain collision above is existence, not date shape:
    a not-yet-existing today-dated candidate is a mint target; an
    existing one is a live input this function must route around.

    Negative-spec: does NOT reuse `artifact_path` unmodified when doing so
    would collide with an EXISTING file on disk -- that is precisely the
    regression this function's collision branch exists to close. Still may
    coincide with `artifact_path` for a genuinely fresh (not-yet-existing)
    mint target -- that coincidence is intentional idempotency, not a bug.

    SLUG DERIVATION ORDER (2026-08-10 PM ruling, naming derivation), most
    specific first, consulted ONLY when `artifact_path` is EMPTY -- the
    standalone shape, `brief`'s `standalone_no_predecessor_reason` case,
    which has no plan/predecessor to derive a slug from via `stem` below at
    all:

      1. `artifact_path`'s own basename (the plan/predecessor a non-empty
         `artifact_path` names) -- handled by the `stem` branch below, not
         this list; this list only ever runs when that branch is empty.
      2. THIS SESSION's own `state/sizings/*.yaml` sizing object, via
         `_resolve_session_sizing_slug` -- NEW. Sizing is the EM's first
         move on a fresh ask (`coordinator:sizing` routes before plan/
         shape/dispatch), so a session's own sizing object frequently
         exists before any plan or handoff does, and the "genuinely
         sourceless standalone" case this fallback used to assume was
         usually not sourceless at all -- see that function's own
         docstring for the authorship-not-adjacency discriminator and the
         multiple-sizings-per-session tiebreak.
      3. `title` (the caller-supplied `--title`, already threaded to d1's
         own `--title=` flag) -- the ONLY OTHER caller-supplied naming
         signal available here (2026-08-04 break-class fix: `Path("").stem`
         is `""`, and the old unconditional `f"{date_str}-{slug}.md"`
         candidate came back as a dangling `state/handoffs/<date>-.md`).
         Slugified via `coordinator_core.ops.ceremony.
         completion_entry._slug_from_title` -- the one PORT of
         `coordinator-doc-new`'s own `_slug_from_title` that lives in an
         importable `coordinator_core` module (`coordinator-doc-new` itself
         has no `.py` suffix and is not a package, so it cannot be imported
         directly; several other `coordinator_core` modules carry their OWN
         hand-ported copy rather than importing this one, apparently
         because each was written before this copy existed as a reachable
         import target -- this call site reuses the existing importable
         port instead of adding a fourth copy). Same
         lower/collapse-non-alnum/strip-dashes/40-char-cap algorithm, so a
         standalone handoff's filename slug matches the house style
         already visible on disk (e.g. `state/sizings/2026-08-03-git-
         commit-agent-structurally-denied-by-.yaml`'s
         dangling-hyphen-on-truncation behaviour).
      4. `_mint_last_resort_slug()` -- a `secrets.token_hex(4)`-based
         `untitled-<shortid>` slug (2026-08-10 PM ruling, REPLACING the
         prior literal `"untitled"`): deterministic-enough (collision-free
         by construction, no disambiguation retry needed) and never a
         dangling `<date>-.md`, for the genuine "nothing at all to go on"
         case -- a bare `baton-assemble apply handoff` with no title, no
         session sizing, and standalone (no predecessor) is still a
         request this function must produce SOME fresh, collision-checked
         path for. See that function's own docstring for why the prior
         literal `"untitled"` was a defect (two same-day standalone
         handoffs collided on one path) and not merely inelegant.

    Tier 2 (session sizing) needs `root` to run `git log`; when `root` is
    `None` (the same "no root in scope" callers named in this function's
    own `root` paragraph below), it is skipped and derivation falls straight
    through to tier 3/4 -- unchanged behaviour for those callers, not a
    regression, since none of them exercised the standalone-empty-
    `artifact_path` shape this whole ordered list only ever applies to.
    """
    stem = Path(artifact_path).stem
    if stem:
        slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
    else:
        sizing_slug = _resolve_session_sizing_slug(root) if root is not None else None
        if sizing_slug:
            slug = sizing_slug
        elif title:
            slug = _title_slug(title) or _mint_last_resort_slug()
        else:
            slug = _mint_last_resort_slug()
    date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    candidate = _repo_rel_handoff_path(f"{date_str}-{slug}.md")
    if root is None:
        return candidate

    # 2026-07-29 follow-up (successor-derivation archive-collision fix, see
    # bug-report evidence at DoE-claude state/handoffs/
    # 2026-07-29_175200_confinement-band-split-plan-awaiting-review.md §
    # Session Ledger): `_exists` must check BOTH the live `state/handoffs/`
    # location AND every known archive location, not the live location
    # alone. Before this fix, a same-day chain whose PREDECESSOR was still
    # live collided (correctly) against that live file and disambiguated
    # via the `<date>_<HHMMSS>_<slug>.md` convention below -- but the exact
    # same predecessor, once swept to `archive/handoffs/` by a concurrent
    # session mid-flight (routine on this fleet, not exotic), vanished from
    # the live-only check, so a RE-BRIEF of the identical input silently
    # stopped disambiguating and handed `coordinator-doc-new` a plain
    # candidate that collided with the now-archived record --
    # `handoff_creation_guard.assert_no_archived_twin` then correctly
    # refused it, but only after the assembler had already computed an
    # unusable path. Reusing `_find_first_match`/`_ARCHIVE_SUBDIRS` (the
    # SAME archive-fallback enumeration `_resolve_qualified_path_or_raise`
    # above already calls, and NOT a hand-rolled single flat archive path)
    # makes the derivation see an identical collision regardless of
    # whether the predecessor is currently live or archived -- the fix
    # this docstring's own "Successor filename derivation must be stable"
    # requirement names.
    # 2026-08-13 break-class fix (hot-path-over-acquisition sweep, residual
    # #3): the prior fix here (2026-07-29, see the memoization test class
    # `TestArchiveScanIsMemoizedAndDegradesOnUnreadableSubdir`) closed the
    # up-to-3x-per-call redundancy by building ONE full basename index of
    # all three archive subtrees (1,682 files, 6.2ms) and reusing it across
    # the ladder's probes -- but the ladder only ever asks about 1-3 KNOWN
    # basenames, never "what's in there", so indexing every file to answer
    # a targeted membership question was itself the over-acquisition (a
    # ~1,681:1 read:need ratio). Fixed by asking the filesystem the direct
    # question -- `rglob(<the one basename this probe needs>)` per subdir --
    # instead of enumerating the subtree to build a lookup set. Each
    # `_exists` probe touches its OWN basename only, so the "no cross-call
    # cache" property the 2026-07-29 fix was careful about (a predecessor
    # archived by a concurrent session mid-ladder must still be visible)
    # holds automatically: there is no cache to go stale.
    import glob

    from coordinator_core.artifact_basename import md_fallback_candidates
    from coordinator_core.ops.resolve_swept_baton import _ARCHIVE_SUBDIRS

    def _archive_has_basename(basename: str) -> bool:
        # `Path.rglob()` treats its argument as a GLOB PATTERN, not a
        # literal filename -- a basename containing `*`/`?`/`[...]`
        # (slugified from a handoff/plan title; unlikely but not
        # impossible) would otherwise match files it does not name. The
        # same hazard, in the same sweep, was fixed the same way at
        # `ops/fleet/memo_send.py` (235b4710) and
        # `ops/ceremony/branch_resolution.py` (44ba757f); `glob.escape`
        # keeps the search targeted (no full-subtree enumeration) while
        # restoring the literal-match semantics the prior
        # `candidate in _archive_basenames` frozenset lookup guaranteed.
        for subdir in _ARCHIVE_SUBDIRS:
            archive_dir = root / subdir
            if not archive_dir.is_dir():
                continue
            try:
                for candidate in md_fallback_candidates(basename):
                    pattern = glob.escape(candidate)
                    if any(p.is_file() for p in archive_dir.rglob(pattern)):
                        return True
            except OSError:
                # Degrade to "treat as clear" for this subdir -- matches the
                # pre-diff `Path.exists()`-only posture, which never raised
                # on an unreadable path either. Logged, not silent.
                _LOG.warning(
                    "baton_assemble: skipping unreadable archive subdir %s "
                    "while probing for the successor-path basename %r",
                    archive_dir,
                    basename,
                )
                continue
        return False

    def _exists(rel: str) -> bool:
        fs_path = Path(rel)
        if not fs_path.is_absolute():
            fs_path = root / rel
        if fs_path.exists():
            return True
        return _archive_has_basename(fs_path.name)

    if not _exists(candidate):
        return candidate

    time_str = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S")
    candidate = _repo_rel_handoff_path(f"{date_str}_{time_str}_{slug}.md")
    if not _exists(candidate):
        return candidate

    counter = 2
    while True:
        candidate = _repo_rel_handoff_path(f"{date_str}_{time_str}_{slug}-{counter}.md")
        if not _exists(candidate):
            return candidate
        counter += 1


def _resume_recorded_successor_path(predecessor: str, root: Path) -> Optional[str]:
    """The successor path a PRIOR attempt of this same `apply` run already
    recorded on the predecessor, or `None` when no prior attempt got that far.
    Read-only; the entire input is the predecessor's OWN frontmatter.

    Why this exists (2026-07-29 break-class fix -- idempotent replay).
    `apply_base.execute_directives` has no rollback and no resume: a handler
    that raises mid-run returns `APPLY_EXIT_PARTIAL_MUTATION` with whatever
    already landed. The sanctioned resume path is therefore RE-RUNNING THE
    IDENTICAL COMMAND -- `brief()` recomputes from scratch every time, so a
    per-directive `already_satisfied` derived from disk turns the second run
    into the continuation, with no `--continue` flag for an operator to
    remember and no run-state file to become a second source of truth. Live
    on 2026-07-29 the absent resume left the operator hand-running d2/d5/d6
    one directive at a time -- the "relocated transcription" the north-star
    discharge test forbids.

    The one thing that made re-running NOT converge was `output_path`:
    `_compute_fresh_output_path` deliberately disambiguates AWAY from any
    existing file, so a second run minted a SECOND successor at a fresh
    `<date>_<HHMMSS>_<slug>.md` path and then handed d6 that new path as
    `continued_into`. `_supersede_continued` (ops/handoff_archive_transition.py)
    correctly refuses to overwrite one real succession edge with a different
    one, so d6 raised `superseded=False`, `_dispatch_handoff_supersede_
    predecessor` deleted the fresh scaffold, and the run aborted -- IDENTICALLY
    on every subsequent attempt. That is a permanent wedge, not a retryable
    failure: the predecessor's `continued_into` kept pointing at the abandoned
    attempt's successor and no re-run could ever converge.

    Resuming the RECORDED path instead makes the whole envelope converge
    through predicates that already exist rather than new ones: d1 sees its
    `--out` target already on disk (`already_satisfied`), and d6 hands
    `_supersede_continued` the value the predecessor already carries, hitting
    that function's OWN byte-identical no-op branch. No satisfaction predicate
    for the succession is defined here -- the single definition stays in the op
    that performs the write.

    DR-242 (`docs/decisions/DR-242-successor-named-child-is-not-evidence-of-
    succ.md`): the evidence read here is the predecessor's own claimed/shipped
    state plus the `deployment_state: continued` + `continued_into` pair that
    d6 ITSELF wrote -- never a successor merely naming this predecessor as its
    parent. `claimed_or_shipped_at_path` is COMPOSED (the same predicate the
    five production gates share), not re-derived. Resumption additionally
    cannot launder a DR-242 refusal: d6's own gate re-checks the predecessor
    independently, and this function returns `None` for any predecessor that
    does not pass the same check.

    Negative-spec:
      - Does NOT read the candidate successor's frontmatter at all -- a
        successor's `predecessor:` field is exactly the successor-named-child
        evidence DR-242 forbids, and admitting it here would launder it into
        d1's `already_satisfied`.
      - Does NOT resume a bare `handoff_id`-shaped `continued_into` value: d6
        always writes a path, and scaffolding d1's `--out` at a
        pseudo-path derived from an id would author a file nothing points at.
      - Does NOT resume a path outside this worktree, or a `..`-bearing one.
      - Does NOT re-serialize the recorded value it returns. Separator folding
        happens on a throwaway copy for the containment checks only; see the
        inline note at the return for the Windows wedge that re-serializing
        would reintroduce.
    """
    if not predecessor:
        return None
    pred_path = Path(predecessor)
    if not pred_path.is_absolute():
        pred_path = root / predecessor
    if not pred_path.is_file():
        return None

    from coordinator_core.archival import claimed_or_shipped_at_path

    if not claimed_or_shipped_at_path(str(pred_path)):
        return None

    fm = _read_frontmatter(pred_path)
    if _fm_field(fm, "deployment_state") != "continued":
        return None
    recorded = (_fm_field(fm, "continued_into") or "").strip()
    if recorded in ("", "none", "null", "~"):
        return None
    if not recorded.replace("\\", "/").endswith(".md"):
        return None

    # Containment is checked on a normalized copy; the RETURNED value is the
    # recorded string VERBATIM. That is load-bearing, not fussiness: d6 hands
    # this value straight back to `_supersede_continued`, whose idempotency
    # branch compares `continued_into` as a plain STRING. Re-serializing it
    # through `Path` would emit `state\handoffs\x.md` on Windows for a record
    # written as `state/handoffs/x.md` (or the reverse), turning the no-op
    # branch into that function's "conflicting succession edge" refusal -- i.e.
    # re-opening this exact wedge on the other platform, or on any repo whose
    # two ends were written from different ones.
    normalized = Path(recorded.replace("\\", "/"))
    if normalized.is_absolute():
        try:
            normalized = normalized.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return None
    elif ".." in normalized.parts:
        return None

    # Under `state/handoffs/` it is a legitimate scaffold target whether or not
    # the file survived the failed attempt (a re-mint at the recorded path is
    # what makes a stale `continued_into` stop being stale). Anywhere else --
    # in practice an already-archived successor -- it is only resumable when it
    # actually exists, since d1 must never scaffold into `archive/`.
    if normalized.as_posix().startswith("state/handoffs/") or (root / normalized).is_file():
        return recorded
    return None


def _resolve_current_session_id() -> Optional[str]:
    """Resolve THIS RUN's own harness session id, for gating
    `_adopt_prior_attempt_scaffold_path`'s authorship check below.

    Delegates to `ops.session_context.resolve_current_session_id`, the
    canonical chain (`COORDINATOR_SESSION_ID` > `CLAUDE_SESSION_ID` >
    `CLAUDE_CODE_SESSION_ID`, above them a per-request identity binding).
    This was formerly a local re-implementation of that env ladder, on the
    precedent that `coordinator/bin/coordinator-doc-new.py` duplicates the
    same helper because it runs from an arbitrary consumer repo's cwd where
    this package may not be importable. That precedent does not reach here:
    this function is inside the package, so it can import the resolver, and
    a copy that reads `os.environ` directly is not equivalent to one that
    does not.

    Negative-spec: do NOT restore the inline `os.environ` reads. This
    function is reachable inside a WARM-SERVED dispatch -- the registered op
    `handoff.correct_body` imports it at call time (see that module's
    own import-cycle note) and gates authorship on it. A warm server's
    environment names whoever SPAWNED it, so a raw env read there resolves a
    stranger; the caller's true identity arrives only as the per-request
    `ContextVar` that the canonical resolver reads. Cold callers are
    unaffected -- with nothing bound, the resolver walks the same three env
    tiers this code used to walk inline.

    `handoff_correct_body._resolve_session_id_with_source` is the paired
    copy: that op cross-checks the two against each other and REFUSES on a
    mismatch, so the two must never be migrated one at a time. They now
    agree by sharing a resolver instead of by two hand-synced ladders.

    Returns `None`, never the CLI's own `'em-unknown'` fallback sentinel,
    when nothing is set -- this copy exists only to GATE an equality check,
    and "no session id resolvable" must fail-safe to "no match," never
    coerce into a value that could coincidentally equal a candidate's own
    unset/placeholder field.
    """
    return resolve_current_session_id()


def _sizing_slug_from_path(path: Path) -> str:
    """`state/sizings/<date>-<slug>.yaml` -> `<slug>` -- the same
    date-prefix-strip `_compute_fresh_output_path` already applies to a
    plan/predecessor basename, reused here so a sizing-derived slug matches
    that same house shape rather than inventing a second stripping rule."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)


def _sizing_add_commits_by_relpath(
    root: Path, sizings_dir: Path
) -> dict[str, tuple[str, str]]:
    """Map every repo-relative `state/sizings/` path to `(author_iso,
    session_id_trailer)` of the commit that ADDED it, resolved in ONE
    `git log --diff-filter=A` walk of the whole directory.

    Replaces two git spawns PER sizing file (an add-commit lookup plus a
    trailer lookup on its sha) with one walk that carries both, because the
    directory is scanned in full on every untitled mint and grows without
    bound. Gate: `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`.

    Framing is `%x00`-delimited records with a `%x01` header/name-list
    separator, not newline-delimited fields: `%(trailers:...valueonly)` renders
    a MULTI-VALUED trailer as several lines, so a newline-framed parse could
    read a second trailer value as the next commit's record. The trailer sits
    LAST in the header for the same reason -- everything between the second
    `|` and the `%x01` is its value, however many lines that is, and a
    multi-valued trailer therefore compares unequal to a session id exactly as
    the per-file form's `stdout.strip() != session_id` did.

    Newest-first is git's own order, so the LAST record naming a path is that
    path's creation commit -- the same "oldest entry" rule the per-file form
    applied to its own output.

    Returns `{}` on any git failure, and omits any path with no add commit in
    this walk; the caller reads both as "unattributable" and falls through to
    its next derivation tier, never guessing.

    Negative-spec -- `--follow` is deliberately NOT used and cannot be: git
    rejects it for more than one pathspec, which is what makes a single walk
    possible at all. A sizing file RENAMED since it was added is therefore
    attributed to the rename commit rather than its pre-rename creation, and a
    session that renamed someone else's sizing could be credited with it.
    Accepted narrowly: this tier only picks a FILENAME slug, the docstring
    above states a wrong pick "never corrupts or overwrites anything", and
    renaming a `state/sizings/*.yaml` is not a shape this corpus exhibits.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--diff-filter=A",
                "--format=%x00%H|%aI|%(trailers:key=Session-Id,valueonly=true)%x01",
                "--name-only",
                "--",
                sizings_dir.as_posix() if sizings_dir.is_absolute() else str(sizings_dir),
            ],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}

    by_relpath: dict[str, tuple[str, str]] = {}
    for record in proc.stdout.split("\x00")[1:]:
        header, _, names_blob = record.partition("\x01")
        _sha, _, rest = header.partition("|")
        add_iso, _, trailer = rest.partition("|")
        trailer_sid = trailer.strip()
        for name in names_blob.splitlines():
            rel = name.strip()
            if rel:
                # Newest-first: a later assignment is an OLDER commit, so the
                # last one to land is the creation commit.
                by_relpath[rel] = (add_iso.strip(), trailer_sid)
    return by_relpath


def _resolve_session_sizing_slug(root: Path) -> Optional[str]:
    """The slug of the `state/sizings/*.yaml` sizing object THIS SESSION
    authored, or `None` when none is attributable -- the PM-ordered
    derivation tier between "predecessor/plan" and "caller-supplied title"
    (2026-08-10 PM amendment to the untitled-mint fix): a session that
    started from a fresh ask routes through the sizing lobby FIRST
    (`coordinator:sizing`), so its own sizing object frequently exists
    before any plan or handoff does -- the "standalone, nothing to go on"
    case the pre-amendment fallback assumed was sourceless usually is not.

    AUTHORSHIP, not adjacency (the discriminator the PM's amendment names
    explicitly, by analogy to the handoff-predecessor "don't pick the
    newest handoff" trap): "most recent file in `state/sizings/`" is not
    ownership on a fleet where concurrent sessions author sizings
    constantly. A sizing-object carries no `session_id`/`authoring_session`
    field of its own (see `sizing-object.schema.json`'s own 1.5.0 x-bump-
    note: "`system.created_by_session` ... was CONSIDERED AND DECLINED ...
    recoverable from the commit's own `Session-Id:` trailer, and needs no
    schema slot") -- so authorship is read from THAT trailer on the commit
    that ADDED the sizing file, via `git log --diff-filter=A --follow`
    (the earliest such commit chronologically, in case a rename crosses
    this scan), never from the file's own content or a later editor's
    commit (the schema's own 1.10.0 note records a sizing being amended by
    a DIFFERENT session -- review-integrator, not the original author --
    which is exactly the adjacency trap this authorship check exists to
    avoid: an editor is not an author).

    MULTIPLE SIZINGS FROM ONE SESSION (PM-named, must be decided and
    documented, not silently picked): this function picks the MOST
    RECENTLY AUTHORED one (by the adding commit's own `%aI` author-date),
    not "refuse to guess" -- because a session that authored several
    sizings in one turn (the `/pickup a AND b AND c` multi-artifact shape
    has a sizing analogue) is overwhelmingly likely to be minting THIS
    baton for the LATEST thing it sized, not an earlier one from the same
    session. This mirrors `_adopt_prior_attempt_scaffold_path`'s own
    precedent of preferring a decidable single answer over an unconditional
    refusal wherever the ambiguity resolves cleanly -- unlike that
    function's EXACTLY-ONE-candidate refusal (which guards a DESTRUCTIVE
    write), a slug pick here is non-destructive: choosing wrong picks an
    imperfect filename, never corrupts or overwrites anything.

    Returns `None` (never raises) on a missing `state/sizings/` directory,
    an unresolvable current session id, a `git log` spawn failure, or zero
    attributable candidates -- falls through to the caller-supplied-title
    tier exactly like every other "nothing found" case in this cascade.

    Negative-spec:
      - Does NOT read a sizing's own frontmatter/YAML body for a
        session-shaped field -- no such field exists (see docstring above);
        the ONLY evidence source is the adding commit's `Session-Id:`
        trailer.
      - Does NOT treat a LATER (edit) commit on the file as authorship --
        only the file's own creation (`--diff-filter=A`) commit is
        consulted.
      - Does NOT fall back to "newest file by mtime" when the git-trailer
        read is unavailable -- an unresolvable authorship check returns
        `None`, never a guess from filesystem adjacency.
    """
    session_id = _resolve_current_session_id()
    if not session_id:
        return None
    sizings_dir = root / "state" / "sizings"
    if not sizings_dir.is_dir():
        return None

    on_disk = sorted(sizings_dir.glob("*.yaml"))
    if not on_disk:
        return None
    add_commits = _sizing_add_commits_by_relpath(root, sizings_dir)

    candidates: list[tuple[str, Path]] = []
    for path in on_disk:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        add = add_commits.get(rel)
        if add is None:
            continue
        add_iso, trailer_sid = add
        if trailer_sid != session_id:
            continue
        candidates.append((add_iso, path))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return _sizing_slug_from_path(candidates[-1][1])


def _mint_last_resort_slug() -> str:
    """The terminal fallback slug when NOTHING upstream (plan, predecessor,
    this session's own sizing object, caller-supplied title) resolves --
    `secrets.token_hex(4)` (8 hex chars), the same shortid-nonce shape
    `coordinator_core.dispatch.provision` and `coordinator_core.subagent_
    sandbox.provision_report` already mint with for an analogous
    "guaranteed non-colliding, no ambient identity to derive from" case.

    2026-08-10 PM ruling, replacing the literal string `"untitled"`: two
    standalone handoffs minted on the same date used to collide on that one
    literal slug and fight over one `state/handoffs/<date>-untitled.md`
    path -- `_compute_fresh_output_path`'s own `<date>_<HHMMSS>_..`/`-N`
    disambiguation ladder papers over same-SECOND collisions but not the
    underlying defect: a mint-time name that carries zero information about
    what was minted. A per-call shortid is non-colliding by construction
    (no disambiguation retry needed) and, unlike `"untitled"`, never reads
    as a title an operator forgot to fill in."""
    return f"untitled-{secrets.token_hex(4)}"


_DIRTY_TREE_EVIDENCE_PATH_CAP = 10


def _normalize_repo_relative_path(path: str) -> str:
    """Normalize a repo-relative path to a common forward-slash form.

    Windows carve-out (this is the load-bearing line, not incidental): both
    inputs this function normalizes -- `.git/coordinator-sessions/<id>/
    touched.txt` lines and `git status --porcelain` paths -- are documented
    elsewhere as already forward-slash-normalized (`track_touched_files.
    _normalize_path` replaces `os.sep`; porcelain itself always emits `/`
    even on Windows). This function is the belt-and-suspenders second layer:
    a naive `set(touched) & set(dirty)` with even ONE side carrying a stray
    backslash silently produces an EMPTY intersection on Windows (never an
    error), which is exactly the "residue: 66, mine: 0" false-negative this
    whole change exists to prevent. Applied uniformly to BOTH sides before
    the intersection, never to only one.
    """
    return path.strip().replace("\\", "/")


def _compute_dirty_tree_attribution(root: Path) -> dict[str, Any]:
    """Disk-derived case-c attribution probe: partitions the current dirty
    set into `mine` (this session's own edits, per its live touch-record
    claims -- `coordinator_core.session.touch_record.project_live_claims`,
    the C3 read seam) and a `residue` count (everything else -- may be a
    sibling session's, may be unrelated cruft; this probe does not attempt
    to attribute residue, only to STOP over-claiming it as "mine").

    Returns one of two shapes:
      - `{"degraded": False, "mine": [<repo-relative paths>, ...], "residue_count": <int>}`
      - `{"degraded": True, "evidence": "<why the probe could not run>"}`

    Degrades to `degraded=True` -- never to `mine=[]` -- on any of: no
    resolvable session id, a degraded touch-record projection (unreadable
    family member or a malformed line -- see `project_live_claims`'s own
    Failure posture), or a failing/erroring `git status` call.
    `_build_judgment_points` falls back to TODAY's unconditional emission on
    `degraded=True`; collapsing a probe FAILURE into `mine=[]` would
    silently resolve `d1` with no ask at all, which is the exact failure
    mode this module must never introduce (a failure to compute must never
    silently resolve `d1`)."""
    session_id = _resolve_current_session_id()
    if not session_id:
        return {
            "degraded": True,
            "evidence": (
                "dirty-tree attribution probe: no resolvable session id "
                "(COORDINATOR_SESSION_ID/CLAUDE_SESSION_ID/CLAUDE_CODE_SESSION_ID "
                "all unset) -- falling back to an unconditional ask."
            ),
        }

    try:
        common_dir = git_common_dir(root)
    except RuntimeError as exc:
        return {
            "degraded": True,
            "evidence": (
                f"dirty-tree attribution probe: git_common_dir({root}) raised "
                f"{exc!r} -- falling back to an unconditional ask."
            ),
        }

    sink_path = common_dir / "coordinator-sessions" / session_id / "touch-record.jsonl"
    projection = project_live_claims(sink_path, cwd=str(root))
    if projection.degraded:
        return {
            "degraded": True,
            "evidence": (
                f"dirty-tree attribution probe: touch-record projection for "
                f"{sink_path} degraded ({'; '.join(projection.degrade_reasons)}) "
                "-- falling back to an unconditional ask."
            ),
        }
    touched = {
        _normalize_repo_relative_path(path) for path in projection.claims
    }

    try:
        # `--untracked-files=all` (not the bare `--porcelain` default) --
        # without it, an entirely-new directory collapses to one `?? dir/`
        # porcelain line rather than one line per file inside it, which would
        # silently make every file under a brand-new directory (e.g. a fresh
        # `state/handoffs/<x>.md`) invisible to the `mine`/`touched.txt`
        # intersection below.
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "--no-optional-locks",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError as exc:
        return {
            "degraded": True,
            "evidence": (
                f"dirty-tree attribution probe: git status --porcelain raised "
                f"{exc!r} -- falling back to an unconditional ask."
            ),
        }
    if result.returncode != 0:
        return {
            "degraded": True,
            "evidence": (
                "dirty-tree attribution probe: git status --porcelain exited "
                f"{result.returncode} -- falling back to an unconditional ask."
            ),
        }

    dirty = {
        _normalize_repo_relative_path(path)
        for _xy, path in parse_porcelain_paths(result.stdout)
        if path.strip()
    }

    mine = sorted(dirty & touched)
    residue_count = len(dirty - touched)
    return {"degraded": False, "mine": mine, "residue_count": residue_count}


def _dirty_tree_case_c_evidence(attribution: dict[str, Any]) -> str:
    """Bounded evidence string for `j-dirty-tree-case-c` -- caps the listed
    path count at `_DIRTY_TREE_EVIDENCE_PATH_CAP` (plus an "and N more" tail)
    so a 66-path dirty tree does not blow up the brief, and always states
    the residue COUNT (visible, never a second question, never gating)."""
    mine = attribution["mine"]
    residue_count = attribution["residue_count"]
    shown = mine[:_DIRTY_TREE_EVIDENCE_PATH_CAP]
    listing = ", ".join(shown)
    remainder = len(mine) - len(shown)
    if remainder > 0:
        listing += f", and {remainder} more"
    return (
        f"coordinator_core.ops.dirty_tree_gate's porcelain read found {len(mine)} "
        f"dirty path(s) matching this session's own touch record: {listing}. "
        f"{residue_count} additional dirty path(s) are NOT in this session's "
        "touch record (unattributed residue -- not this ask; see "
        "coordinator_core.ops.dirty_tree_gate for that classification)."
    )


def _adopt_prior_attempt_scaffold_path(
    predecessor: str,
    predecessor_id: Optional[str],
    root: Path,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """The path of THE SOLE LIVE `state/handoffs/*.md` FILE NAMING THIS
    PREDECESSOR AS ITS OWN `predecessor:` AND CARRYING THIS RUN'S OWN
    `authoring_session` -- adopted as d1's `--out` so a re-run RE-USES it
    instead of minting a second successor beside it. `None` when no such
    file can be identified. Read-only.

    What this establishes, precisely -- and why the authorship check exists
    (Finding 1, review-integrator 2026-07-30; closed 2026-07-30). The
    identification below used to be exactly what its name said: "the sole
    live child naming this predecessor." That was NOT, and could not be,
    "this run's own prior attempt" -- nothing checked which session or run
    authored the candidate. On a shared branch (this repo's own operating
    model -- see project CLAUDE.md's "Don't stash on a shared dirty tree" /
    "Live agent invalidates your grep" class of cautions), an unrelated
    session's legitimate, in-progress continuation attempt that happened to
    be the ONLY live child currently naming this predecessor satisfied every
    other condition below and was adopted as if it were this run's own
    residue -- and adoption is not idle: `coordinator-doc-new`'s `--out` is
    an unconditional `open(out_path, "w")` (no existence check), so
    misfiring here silently truncates a live peer's in-progress handoff back
    to a pristine scaffold for the entire span between that peer's d1 and
    d6. The harm is that overwrite, not merely an orphaned file.

    The fix: adoption now additionally requires the surviving candidate's
    own `authoring_session` field to be present and equal to THIS run's own
    session id (`_resolve_current_session_id` above). A handoff scaffolded
    before this change carries no `authoring_session` at all and is
    therefore NEVER adopted by this predicate -- that is intentional and
    fail-safe: absence falls through to the pre-existing fresh-mint
    behaviour (a harmless second file, not a silent overwrite), exactly the
    direction every other refusal in Identification below already fails.

    Why this exists (2026-07-30 -- the residue row 76ee96ee left open).
    `_resume_recorded_successor_path` above resumes off PREDECESSOR-side
    evidence: the `deployment_state: continued` + `continued_into` pair d6
    itself wrote. That evidence only exists once d6 has RUN. An abort BEFORE d6
    -- d2, d4 or d5 raising -- is the common case precisely because d6 is
    emitted last, and it leaves the predecessor completely untouched. If d1's
    compensator then also declines to delete the scaffold (because the operator
    edited it, so `_is_pristine_generator_scaffold` correctly protects it),
    there is no predecessor-side fact left. The re-run minted a fresh successor
    beside the survivor and ORPHANED it -- converging to a correct succession
    graph with one abandoned file per aborted attempt.

    The one disk fact that identifies the survivor is its OWN `predecessor:`
    pointer -- successor-side evidence. DR-242 Amendment A1
    (`docs/decisions/DR-242-successor-named-child-is-not-evidence-of-succ.md`,
    § 6, PROPOSED) admits it for exactly this ONE decision and no other: WHICH
    PATH d1 WRITES. That is not a succession question. Getting it wrong picks a
    filename badly (an orphan); it cannot conclude that anything was
    superseded, claimed, or archived, and it cannot flip a status or emit a
    stamp. d6's own gate is untouched and still reads predecessor-side evidence
    only -- it re-derives `claimed_or_shipped` independently, so an adoption
    here cannot launder a DR-242 refusal into a succession.

    Precedence: this runs ONLY when `_resume_recorded_successor_path` returned
    nothing AND the predecessor carries no `continued_into` at all. That
    ordering is what keeps the two evidence classes from competing --
    predecessor-side evidence always wins, and a predecessor that HAS a
    succession edge is never a candidate for this path.

    Identification (every condition required; any doubt returns `None`):
      - The predecessor must be a live file under `state/handoffs/`. An
        archived predecessor means d6 already ran and archived it, which is
        `_resume_recorded_successor_path`'s case, not this one.
      - The predecessor must carry NO `continued_into` and must not be at
        `deployment_state: continued`.
      - `claimed_or_shipped_at_path` must pass -- COMPOSED, the same predicate
        the five production gates share, never re-derived. Not defence in depth
        for its own sake: d6 refuses a never-claimed predecessor outright, so
        adopting a path for a run that cannot complete grants the carve-out
        reach it has no use for.
      - A candidate is a live `state/handoffs/*.md` file, other than the
        predecessor itself, whose own `predecessor:` field resolves to the SAME
        file as `predecessor`, AND whose `predecessor_id` matches the
        predecessor's own `handoff_id` when both carry one.
      - A candidate that has itself been continued (its own `continued_into`
        set, or `deployment_state: continued`) is not a prior attempt's
        scaffold -- it is a completed link in a longer chain.
      - EXACTLY ONE candidate must survive after ALL of the above -- including
        the authorship check immediately below. Zero means nothing to adopt;
        TWO OR MORE means the predecessor legitimately has several named
        children and this function cannot tell which one (if any) is this
        run's own prior attempt -- adopting either would orphan the other, so
        it declines and the pre-existing fresh-mint behaviour stands.
      - The candidate's own `authoring_session` field must be PRESENT and must
        equal `_resolve_current_session_id()` -- THIS run's own session id,
        resolved from this process's own environment, never a value passed in
        by the caller. Absent field, absent/unresolvable env on this run, or a
        mismatch all return `None` alike; there is no partial-credit branch.
        This is the authorship fact `coordinator/bin/coordinator-doc-new.py`'s
        `_scaffold_handoff` now stamps at scaffold time (see that function's
        docstring) -- machine-written, not operator-typed prose, and
        therefore safe to gate on, unlike the `PLACEHOLDER` string the OTHER
        scaffolders (spinoff/goal-seed/roadmap-seed) still emit for a human to
        fill in later.

    Return shape: `str(Path("state") / "handoffs" / <name>)` -- built the same
    way `_compute_fresh_output_path` builds its own candidates, so an adopted
    path and a freshly-minted one are the same string shape on every platform
    (backslash-separated on Windows, as that function already emits). Note the
    contrast with `_resume_recorded_successor_path`, which must return a
    RECORDED string verbatim because `_supersede_continued` compares it against
    what is already on disk: there is no recorded value here to match, so d6
    writes this one fresh, and constructing it via `Path` reintroduces no
    cross-platform wedge.

    Negative-spec:
      - Does NOT adopt a candidate whose `authoring_session` is absent, even
        when it is otherwise the sole live child naming this predecessor and
        everything else about it looks like this run's own residue -- a
        handoff scaffolded before this change, or by any scaffolder that
        still emits a placeholder rather than a machine-stamped value, is
        indistinguishable from an unrelated session's file and is never
        adopted. This is fail-safe to fresh-mint, not a gap: see the harm
        this predicate exists to prevent, above.
      - Does NOT read a candidate's `predecessor:` field for ANY purpose other
        than choosing d1's `--out` path. It concludes nothing about succession,
        supersession, claim, archival, or a gate verdict -- see DR-242
        Amendment A1's anti-loophole teeth.
      - Does NOT fire when the predecessor carries predecessor-side evidence;
        that is `_resume_recorded_successor_path`'s single definition.
      - Does NOT adopt a candidate outside the live `state/handoffs/` tree, and
        never an `archive/` path -- d1 must never scaffold into the archive.
      - Does NOT resolve ambiguity by picking the newest/first candidate. An
        ordering heuristic over a set this function cannot distinguish is a
        guess dressed as a rule.
      - Does NOT write, stamp, or repair anything, on the predecessor or the
        candidate.
    """
    if not predecessor:
        return None
    pred_path = Path(predecessor)
    if not pred_path.is_absolute():
        pred_path = root / predecessor
    if not pred_path.is_file():
        return None
    live_dir = root / "state" / "handoffs"
    try:
        pred_resolved = pred_path.resolve()
        if pred_resolved.parent != live_dir.resolve():
            return None
    except OSError:
        return None

    pred_fm = _read_frontmatter(pred_path)
    if _fm_field(pred_fm, "deployment_state") == "continued":
        return None
    if (_fm_field(pred_fm, "continued_into") or "").strip() not in ("", "none", "null", "~"):
        return None

    from coordinator_core.archival import claimed_or_shipped_at_path

    if not claimed_or_shipped_at_path(str(pred_path)):
        return None

    current_session = session_id or _resolve_current_session_id()
    if not current_session:
        return None

    pred_handoff_id = _fm_field(pred_fm, "handoff_id") or predecessor_id or ""

    candidates: list[str] = []
    for candidate in sorted(live_dir.glob("*.md")):
        try:
            if candidate.resolve() == pred_resolved:
                continue
        except OSError:
            continue
        # A single unreadable or non-UTF8 peer handoff must not be able to
        # take down the whole adopt-path scan (and therefore the whole
        # `/handoff` cascade, which cannot start if this raises) -- matches
        # `_scan_deliverable_collision`'s own identical guard over the same
        # `_read_frontmatter` call, the established pattern for this class of
        # candidate loop.
        try:
            fm = _read_frontmatter(candidate)
        except (OSError, UnicodeDecodeError):
            continue
        named = (_fm_field(fm, "predecessor") or "").strip()
        if named in ("", "none", "null", "~"):
            continue
        named_path = Path(named.replace("\\", "/"))
        if not named_path.is_absolute():
            named_path = root / named_path
        try:
            if named_path.resolve() != pred_resolved:
                continue
        except OSError:
            continue
        candidate_pred_id = (_fm_field(fm, "predecessor_id") or "").strip()
        if candidate_pred_id and pred_handoff_id and candidate_pred_id != pred_handoff_id:
            continue
        if _fm_field(fm, "deployment_state") == "continued":
            continue
        if (_fm_field(fm, "continued_into") or "").strip() not in ("", "none", "null", "~"):
            continue
        candidate_session = (_fm_field(fm, "authoring_session") or "").strip()
        if not candidate_session or candidate_session != current_session:
            continue
        candidates.append(candidate.name)

    if len(candidates) != 1:
        return None
    return _repo_rel_handoff_path(candidates[0])


def _adopt_prior_attempt_mint_path(
    mint_path: str, root: Path, kind: str, session_id: Optional[str] = None
) -> Optional[str]:
    """Non-None when the bare-slug MINT TARGET `mint_path` is already occupied
    by THIS run's own prior attempt -- the path is then re-used as
    `lineage["output_path"]` (making d1 `already_satisfied`, since d1's own
    predicate is "does `--out` exist on disk") instead of
    `_compute_fresh_output_path` disambiguating away from it and minting a
    SECOND baton beside the first.

    The kind=spinoff sibling of `_adopt_prior_attempt_scaffold_path`, which
    cannot serve here: that function identifies a prior attempt by the
    candidate's own `predecessor:` pointer, and every spinoff is
    `predecessor: none` by design (schema_validate.py Rule A3a-3). The
    evidence available on this path is stronger and needs no scan -- the bare
    slug determines the mint path exactly, so there is a single candidate to
    accept or reject, never a set to rank.

    Fixes the reproduced live break (bug backlog
    `2026-08-25-spinoff-brief-then-apply-mints-two-batons-and-adopts-the-stub-
    as-origin.yaml`): `apply spinoff <slug>` was idempotent only for
    kind=handoff, which reaches convergence through the predecessor-side
    `continued_into` record (`_resume_recorded_successor_path`) a spinoff never
    writes. Re-running the identical spinoff command -- the ordinary response to
    a first run whose output the operator could not read -- minted a second
    pickup_ready baton for one topic.

    Adopted only when EVERY condition holds; there is no partial-credit branch,
    and a decline falls back to the pre-existing fresh-mint behaviour (a second
    file, but never a corrupted one):
      - `mint_path` names an existing file directly under live
        `state/handoffs/` -- never `archive/`, which d1 must not write into.
      - Its `kind:` equals the `kind` this run is assembling. A `handoff` and a
        `spinoff` briefed from the same slug are different artifacts that
        happen to collide on a name; adopting across kinds would author one
        over the other.
      - Its `authoring_session` is PRESENT and equals `_resolve_current_session_id()`
        -- THIS process's own session, resolved from this run's environment,
        never a caller-supplied value. The same machine-stamped authorship fact
        `_adopt_prior_attempt_scaffold_path` gates on, and for the same reason:
        a peer session's same-slug baton is not this run's residue.
      - It has not been continued (`deployment_state: continued`, or a set
        `continued_into`) -- a completed link in a longer chain is not a prior
        attempt's output.

    Negative-spec:
      - Does NOT read the adopted file for LINEAGE. `resolve_lineage` holds `fm`
        empty on this path: the file is this run's own output, so nothing in it
        is an origin, a predecessor, or a progenitor. That separation is the
        whole point -- adopting the path while reading the frontmatter would
        re-create the `origin_handoff`-names-the-stub half of the defect.
      - Does NOT write, stamp, delete, or repair anything.
      - Does NOT fire for a qualified caller-supplied `artifact_path`; only the
        bare-slug mint convention reaches here (`resolve_lineage`'s
        `was_bare_slug` gate), because only there is `artifact_path` the OUTPUT
        rather than an input.
    """
    if not mint_path:
        return None
    candidate = Path(mint_path)
    if not candidate.is_absolute():
        candidate = root / mint_path
    if not candidate.is_file():
        return None
    try:
        if candidate.resolve().parent != (root / "state" / "handoffs").resolve():
            return None
    except OSError:
        return None
    try:
        fm = _read_frontmatter(candidate)
    except (OSError, UnicodeDecodeError):
        return None
    if (_fm_field(fm, "kind") or "").strip() != kind:
        return None
    if _fm_field(fm, "deployment_state") == "continued":
        return None
    if (_fm_field(fm, "continued_into") or "").strip() not in ("", "none", "null", "~"):
        return None
    current_session = session_id or _resolve_current_session_id()
    if not current_session:
        return None
    if (_fm_field(fm, "authoring_session") or "").strip() != current_session:
        return None
    return _repo_rel_handoff_path(candidate.name)


def _assert_no_directive_writes_over_input(
    directives: list[dict[str, Any]], input_path: str, root: Path
) -> None:
    """Backstop invariant, covering the directive table GENERALLY (every
    directive, not just d1): no computed directive may carry a `--out=`
    write-target equal to a caller-supplied INPUT `artifact_path` that
    ALREADY EXISTS ON DISK. Firing such a directive would silently
    overwrite/destroy the input artifact the moment it dispatches
    (`coordinator-doc-new`'s `--out` write is an unconditional overwrite --
    see `_compute_fresh_output_path`'s own docstring). Fails loud with
    `ValueError` naming the colliding directive and path, rather than
    letting the caller discover the collision only after firing it.

    Existence-gated deliberately: the bare-slug mint convention
    (`_normalize_artifact_path` turning a bare slug into a fresh
    `state/handoffs/<date>-<slug>.md` target) legitimately produces an
    `artifact_path` that IS ALSO the intended `--out` -- there is no
    existing file at that path to destroy, so `input_path == --out` there
    is by design, not a collision. Gating on existence is what lets this
    guard stay general across both calling shapes without special-casing
    kind or caller convention.

    Scoped to the `--out=<value>` single-token flag shape specifically
    (the ONE write-target convention any directive in this module's table
    uses today, via d1) -- not a bare positional-arg match. A bare
    positional equal to `input_path` is legitimate and common for
    READ-only directives (d2's `--file <path>` lint target, d5's
    `claim-plan` args) and for directives outside this module's current
    scope (d6's `handoff.supersede_predecessor` composes an existing op
    whose own write-vs-input semantics are a concurrent investigation's
    concern, not this guard's) -- matching bare positionals would false-
    positive on both. A future directive that threads its own output path
    should adopt the same `--out=` convention to inherit this protection.
    """
    if not input_path:
        return
    input_fs_path = Path(input_path)
    if not input_fs_path.is_absolute():
        input_fs_path = root / input_path
    if not input_fs_path.is_file():
        return  # nothing existing to destroy -- a fresh-mint target, not a live input
    collision_flag = f"--out={input_path}"
    for directive in directives:
        if directive.get("already_satisfied"):
            # A satisfied directive is SKIPPED, never dispatched
            # (`contract.apply_base.execute_directives` -- "skips
            # `already_satisfied` directives without dispatching their
            # handler"), so it cannot overwrite anything and this guard has
            # nothing to protect against. Load-bearing since the bare-slug
            # replay adopt (`_adopt_prior_attempt_mint_path`): there
            # `output_path` is deliberately the existing mint-path file and d1
            # is satisfied BECAUSE it exists -- without this narrowing the
            # backstop would refuse the whole brief for the one shape whose
            # entire purpose is to not write.
            continue
        for arg in directive.get("args") or []:
            if arg == collision_flag:
                raise ValueError(
                    f"baton_assemble: directive {directive.get('id')!r} "
                    f"({directive.get('cli')!r}) would write its output to "
                    f"{input_path!r} -- the SAME path as an EXISTING input "
                    "artifact. Refusing to emit a directive that would "
                    "overwrite the input artifact; this is the fail-loud "
                    "backstop, not a computed output-path bug -- check the "
                    "caller's output-path derivation."
                )


def _tracking_read_frontmatter_field(
    plan_file: Optional[str], predecessor: Optional[str]
) -> tuple[Callable[[str, str], str], list[str]]:
    """Wrap `read_frontmatter_field` so `resolve_deliverable_and_initiative`'s
    OWN plan -> predecessor discovery order (`coordinator_core.ops.
    deliverable_carry`) can be OBSERVED, not re-implemented -- AC3's "exactly
    one implementation of this cascade" bars a second copy of the plan ->
    predecessor branching, but `discovery` still needs to name which tier
    supplied the id. Records which of `plan_file` / `predecessor` produced
    the first truthy `deliverable_id` read, in call order -- the SAME order
    the wrapped cascade queries in, so the recorded tier can never disagree
    with which path the cascade actually took.

    Returns `(tracked_read_fn, tier_list)` -- `tier_list` is empty until the
    cascade fires a truthy `deliverable_id` read, then holds exactly one of
    `"plan"` / `"artifact"` (the DoE-claude-cascade's own vocabulary, ported
    verbatim rather than reusing "predecessor" -- see module docstring at
    `resolve_lineage`). `resolve_lineage` itself relabels an `"artifact"` hit
    to a THIRD value, `"plan-input"`, when the hit's `_predecessor_file` is
    the plan->execute trigger's plan (not a real predecessor handoff) -- see
    that function's `is_plan_input` block. This function never emits
    `"plan-input"` itself; the vocabulary is `"plan"` / `"artifact"` /
    `"plan-input"` only once the caller's relabel is applied.
    """
    tier: list[str] = []

    def _tracked(path: str, field: str) -> str:
        value = _read_frontmatter_field(path, field)
        if field == "deliverable_id" and value and not tier:
            if plan_file and path == plan_file:
                tier.append("plan")
            elif predecessor and path == predecessor:
                tier.append("artifact")
        return value

    return _tracked, tier


def _walk_deliverable_ancestor_set(
    lineage_source: Optional[Path],
    additional_predecessor_paths: Optional[list[str]],
    root: Path,
    *,
    max_depth: int = 64,
) -> set[Path]:
    """Resolve the full TRANSITIVE ancestor set of the artifact `resolve_
    lineage` is currently authoring, starting from `lineage_source` (the
    immediate predecessor/origin this run read its lineage from) and any
    fan-in `additional_predecessor_paths` the caller supplied.

    Governing principle (2026-08-05 PM ruling, widening the same-day
    roadmap-baton-kind-skip fix): the `deliverable_id` holder is not "the
    ancestor" -- it is whichever baton in the lineage chain is LIVE and MOST
    RECENT, i.e. the chain's tip. Every earlier artifact in that SAME chain
    still carrying the id is the designed carry (plan -> predecessor -> mint,
    repeated once per hop), not a collision. `roadmap-baton` is merely the
    special case where the ancestor happens to be the chain ROOT --
    `_scan_deliverable_collision`'s separate kind-based skip stays in place
    for that case (see its own docstring) because a roadmap-baton reached via
    a `plan:` pointer rather than a `predecessor:` edge has no ancestry edge
    for THIS walk to follow at all.

    Walks the `predecessor:` field (a repo-relative-or-absolute path string,
    per `handoff.schema.json`) hop by hop, resolving each against `root` with
    the same `Path.resolve()`-with-`OSError`-fallback discipline
    `_scan_deliverable_collision`'s own `excluded` set already uses. Also
    follows each visited artifact's OWN `additional_predecessors:` list
    (fan-in edges), so a merged chain's every branch is excluded, not just
    the primary line.

    `predecessor_id` is NOT an independent traversal edge -- `resolve_
    lineage`'s own docstring establishes it is read off "the SAME file the
    predecessor field names", i.e. it is a companion attribute of the
    `predecessor:` path hop, never a second, separately-reachable node. The
    path field alone is sufficient for correctness here; there is no id-keyed
    hop this walk would otherwise miss.

    `seen` (keyed on the same resolved-or-raw `Path` the exclusion set uses)
    makes this cycle-safe -- a malformed or self-referential `predecessor:`
    chain terminates instead of looping. `max_depth` is a second, independent
    backstop against a long-but-acyclic chain. Both bounds terminate the walk
    silently; a missing, unreadable, or frontmatter-less ancestor stops that
    one leg without raising (this function inherits `_scan_deliverable_
    collision`'s own never-raise invariant).

    Returns the resolved-or-raw `Path` set of every node visited (including
    `lineage_source` and each `additional_predecessor_paths` entry itself) --
    callers fold this into their own exclusion set alongside `exclude_path`.
    A sibling reached only via a DIFFERENT chain (same parent, but not on
    THIS artifact's own ancestor path) is never visited and therefore never
    excluded -- genuine independent-mint duplicates still collide.

    Ancestor refs (`predecessor` / `additional_predecessors`) are resolved
    archive-aware via `dag.resolve_target` (same fix shape as the sibling
    `_lineage_related_sessions` fix in `pickup_assemble`), not a bare
    `root / ref` join. A bare join is correct only while the referenced
    ancestor is still live under `state/handoffs/` -- once it archives to
    `archive/handoffs/YYYY-MM/`, the join resolves to a now-nonexistent
    path, `_read_frontmatter` returns "", and the walk silently `continue`s
    past it: the archived node itself is already in `seen` (added before
    the read) so it stays correctly excluded, but everything BEYOND it
    (its own `predecessor`/`additional_predecessors`) is lost, truncating
    the exclusion set one hop early.
    """
    import yaml

    def _resolve_ancestor_ref(ref: str) -> Path:
        resolved = dag.resolve_target(
            ref,
            handoff_dir=str(root / "state" / "handoffs"),
            repo_root=str(root),
            include_history_tier=False,
        )
        if resolved:
            return Path(resolved)
        ref_path = Path(ref)
        if not ref_path.is_absolute():
            ref_path = root / ref_path
        return ref_path

    seen: set[Path] = set()
    frontier: list[Path] = []
    if lineage_source is not None:
        frontier.append(lineage_source)
    for _extra in additional_predecessor_paths or []:
        frontier.append(_resolve_ancestor_ref(_extra))

    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[Path] = []
        for candidate_path in frontier:
            try:
                resolved = candidate_path.resolve()
            except OSError:
                resolved = candidate_path
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                fm_text = _read_frontmatter(candidate_path)
            except (OSError, UnicodeDecodeError):
                continue
            if not fm_text:
                continue
            try:
                fm_dict = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(fm_dict, dict):
                continue
            pred = fm_dict.get("predecessor")
            if isinstance(pred, str) and pred.strip() and pred.strip().lower() != "none":
                next_frontier.append(_resolve_ancestor_ref(pred.strip()))
            extras = fm_dict.get("additional_predecessors")
            if isinstance(extras, list):
                for extra in extras:
                    if isinstance(extra, str) and extra.strip():
                        next_frontier.append(_resolve_ancestor_ref(extra.strip()))
        frontier = next_frontier
    return seen


def _addr_suffix(value: str) -> str:
    """Shared `", send-message-address=..."` shape for the `reachable` and
    `own_session` branches below -- structural, not convention-maintained
    (Review: code-reviewer -- P3, near-duplicate suffix construction)."""
    return f", send-message-address={value!r}"


def _resolve_claimed_by_address_suffix(claimed_by: Optional[str]) -> str:
    """Best-effort `" (send-message-address=...)"` suffix for a collision
    warning's `claimed_by` UUID, or `""` on any resolution failure.

    A thin formatter over `reachability.resolve_advisory_address` (the
    shared bare-string resolution core, factored out so `pickup_assemble`'s
    `send_message_address` field does not re-derive the same
    `ResolveResult.outcome` mapping) -- this function owns only the
    `" (send-message-address=...)"` wrapping for THIS warning's message
    shape.

    Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-
    registry.md` § 3, wiring the resolver into `baton-assemble`'s
    deliverable-collision warning -- the strongest single-surface candidate
    named there. Degrades to the current UUID-only message on every failure
    path (empty `claimed_by`, an import error, or the resolver itself
    raising) -- resolution is advisory only and must never raise or block
    this warning (per that section's own directive).

    Negative-spec: the `coordinator_core.session.reachability` import is
    deliberately LOCAL to this function body, not hoisted to module scope --
    an import-time failure in that module (e.g. a future circular import)
    then degrades identically to a runtime resolution failure, through the
    same `except Exception: return ""` below, rather than aborting this
    module's own import (Review: code-reviewer -- P3, local import
    rationale)."""
    if not claimed_by:
        return ""
    try:
        from coordinator_core.session import reachability

        value = reachability.resolve_advisory_address(claimed_by)
    except Exception:
        return ""

    if not value:
        return ""
    return _addr_suffix(value)


def _scan_deliverable_collision(
    deliverable_id: Optional[str],
    exclude_path: Path,
    root: Path,
    lineage_source: Optional[Path] = None,
    additional_predecessor_paths: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Warn-only identity-collision scan (2026-08-05 duplicate-baton-
    deliverable-id-warn plan, C1): does a NON-TERMINAL handoff already exist
    under `state/handoffs/` carrying the SAME `deliverable_id` this baton is
    about to be authored under? Two sessions each running their own
    plan -> predecessor -> mint cascade never see each other -- this is the
    one place that reads across the whole live set before a second baton for
    one deliverable gets written.

    `state/handoffs/` ONLY -- never `archive/handoffs/` (Anti-scope). An
    archived baton is terminal by construction; scanning it would flag every
    completed deliverable's own successor as a false-positive collision
    forever.

    Non-terminal is read off `HANDOFF_TERMINAL_DEPLOYMENT`
    (`coordinator_core.lifecycle_constants`) -- the canonical four-member set
    (`shipped`, `abandoned`, `continued`, `closed`). Do NOT swap in either
    three-member `_TERMINAL_DEPLOYMENT_STATES` copy elsewhere in the tree:
    both omit `abandoned`, which would warn on an already-dead baton.

    `exclude_path` is `lineage["output_path"]` resolved against `root` -- the
    file THIS run is about to scaffold (or, on a resumed/adopted run, the
    live file it is about to rewrite). Comparing candidates against it is
    what keeps a re-run from reporting a collision against itself (AC5).

    Governing principle (2026-08-05 PM ruling): the `deliverable_id` HOLDER
    is not "the ancestor" in some fixed, one-hop sense -- it is whichever
    baton in the lineage chain is LIVE and MOST RECENT, i.e. the chain's
    TIP. Every earlier, still-non-terminal artifact in that SAME chain
    carrying the same id is the designed carry (plan -> predecessor -> mint,
    repeated once per hop), never a collision. `lineage_source` (the
    artifact this run read its `deliverable_id` FROM) is excluded together
    with its FULL transitive ancestor set -- see `_walk_deliverable_ancestor_
    set`, which this function delegates the walk to -- not merely
    `lineage_source` itself. A one-hop-only exclusion missed a grandparent
    (or deeper) still carrying the id: a `kind: spinoff` ancestor with a
    `kind: session-handoff` child (`dlv-pickup-skill-code-driven-branch-
    result-acd867`) and a 3-deep `session-handoff` chain
    (`dlv-claude-klabauter-oss-release-engine-mirr-a0952e`) both reproduce
    this live in `state/handoffs/` as of 2026-08-05, and both have NO
    `roadmap-baton` anywhere in the chain -- the kind-based skip below does
    not touch either.

    Excluding the whole ancestor chain costs no real detection. A genuine
    duplicate is two batons that resolved the same id INDEPENDENTLY -- via a
    shared `plan:` pointer, each session running its own cascade, or two
    SIBLINGS minted off one common predecessor -- so the competing baton is
    never on the path this run's own lineage was read up through. A sibling
    (same parent, different branch) is NOT an ancestor and still collides;
    see `_walk_deliverable_ancestor_set`'s own docstring for why the walk
    cannot accidentally swallow it.

    A candidate whose `kind` canonicalizes (via `coordinator_core.frontmatter.
    baton_class.canonical_kind` -- the same resolver
    `_resolved_predecessor_canonical_kind` in this file already uses) to
    `roadmap-baton` is skipped regardless of `deployment_state` and
    regardless of ancestry (2026-08-05 example-market-data-repo-em cross-repo
    memo). This is the DOCTRINE-MANDATED special case of the governing
    principle above, kept as an independent check rather than folded into
    the ancestor walk: a roadmap-baton stub is the chain ROOT and OWNS the
    identity (`roadmap-planning/SKILL.md` § D1) but is reached via a `plan:`
    pointer, not a `predecessor:` edge -- the ancestor walk has no edge to
    follow to it at all, so without this separate kind-based skip a
    roadmap-baton root would still warn even though it is exactly the chain
    tip's own root. DoE-claude `coordinator/docs/wiki/coordinator-
    tripwires.md:1454` mandates this exclusion directly: "Any backfill,
    reconciler, or convergence pass over `deliverable_id` MUST exclude
    `kind: roadmap-baton` records."

    Returns `None` on no hit. An unreadable or frontmatter-less candidate is
    skipped, not fatal (AC6) -- this scan never raises.
    """
    from coordinator_core.frontmatter.baton_class import canonical_kind

    if not deliverable_id:
        return None
    handoffs_dir = root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return None
    excluded: set[Path] = set()
    if exclude_path is not None:
        try:
            excluded.add(exclude_path.resolve())
        except OSError:
            excluded.add(exclude_path)
    excluded |= _walk_deliverable_ancestor_set(lineage_source, additional_predecessor_paths, root)
    for candidate in sorted(handoffs_dir.glob("*.md")):
        try:
            if candidate.resolve() in excluded:
                continue
        except OSError:
            continue
        try:
            fm = _read_frontmatter_bounded(candidate)
        except (OSError, UnicodeDecodeError):
            continue
        if not fm:
            continue
        if _fm_field(fm, "deliverable_id") != deliverable_id:
            continue
        deployment_state = _fm_field(fm, "deployment_state")
        if deployment_state in HANDOFF_TERMINAL_DEPLOYMENT:
            continue
        if canonical_kind(_fm_field(fm, "kind")) == "roadmap-baton":
            continue
        try:
            candidate_rel = candidate.relative_to(root).as_posix()
        except ValueError:
            candidate_rel = str(candidate)
        return {
            "path": candidate_rel,
            "status": _fm_field(fm, "status"),
            "deployment_state": deployment_state,
            "claimed_by": _fm_field(fm, "claimed_by"),
        }
    return None


def _predecessor_is_plan_input(fm: str, artifact_path: str, root: Path) -> bool:
    """Is ``artifact_path`` the plan->execute trigger's own PLAN, arriving on
    the predecessor axis rather than as a real handoff record?

    Predicate: ``artifact_path`` does not carry its own `handoff_id` or
    `kind` (i.e. is not itself a handoff record -- `_fm_field(fm, "handoff_
    id")` / `_fm_field(fm, "kind")`, the SAME discriminator `resolve_lineage`
    computes as `is_own_handoff_record` for its own, differently-scoped,
    purposes), AND `fm` carries a `plan_id`, AND `artifact_path` resolves to
    a path under `docs/plans/` relative to `root` (the secondary confirmatory
    check -- a `plan_id`-shaped field alone, off a path that does not live
    under `docs/plans/`, is not enough).

    The single definition `resolve_lineage` consults at BOTH the pre-cascade
    call site (arming `resolve_deliverable_and_initiative`'s
    `predecessor_is_plan_input` refusal, DR-207 DD#1 AC4-widening) and the
    post-cascade `discovery == "plan-input"` relabel -- a second copy here is
    exactly the drift AC4 exists to prevent: the guard could then disagree
    with the label about what a plan input IS.

    ``fm`` and ``artifact_path`` must be the SAME values `resolve_lineage`
    already holds (frontmatter already read, `artifact_path` already
    normalized/resolved) -- this function does no path resolution of its
    own beyond the `docs/plans/` containment check, mirroring every other
    predicate in this module's caller-resolves/this-reads discipline.

    Null-reader split, INTENTIONAL (2026-08-13 review, finding 1): this
    predicate's `plan_id` presence check goes through `_fm_field`
    (`read_fm_field_unquoted`), which does NOT fold a literal YAML
    `plan_id: null` to `None` -- it returns the two-character string
    `"null"`. That is a DIFFERENT null-handling contract from the sibling
    reader `read_frontmatter_field` used for the `deliverable_id` rungs in
    the same cascade, which DOES fold literal `null` to `""`. Do NOT
    "fix" this by normalizing `plan_id: null` to absent/`None` -- the two
    failure modes are asymmetric: treating `plan_id: null` as PRESENT
    (this function's current behaviour) over-arms the guard, costing a
    loud refusal the caller can fix in one edit; treating it as ABSENT
    would under-arm the guard for a plan-shaped artifact whose author
    wrote `plan_id: null` instead of omitting the field, reopening the
    exact silent-mint hole this predicate exists to close. More-inclusive
    is the deliberately-chosen safe direction here.

    Negative-spec:
        - Do NOT swap `_fm_field` for `read_frontmatter_field` (or
          otherwise fold literal `null` to absent) on the `plan_id`
          check above -- see the null-reader-split paragraph.
    """
    if _fm_field(fm, "handoff_id") or _fm_field(fm, "kind"):
        return False
    if _fm_field(fm, "plan_id") is None:
        return False
    _plan_candidate = Path(artifact_path) if artifact_path else None
    if _plan_candidate is not None:
        if not _plan_candidate.is_absolute():
            _plan_candidate = root / _plan_candidate
        try:
            _plan_rel_parts = _plan_candidate.resolve().relative_to(root.resolve()).parts
        except (OSError, ValueError):
            # Resolved containment failed -- may be a genuinely
            # out-of-root path, OR a `docs/plans/*.md` entry that is
            # itself a SYMLINK resolving outside `root` (2026-08-13
            # review, finding 2). `.resolve()` follows symlinks, so the
            # resolved-path check alone silently falls through to "not a
            # plan input" for that shape -- the same silent-mint failure
            # mode this predicate exists to close, reopened for a
            # symlinked plan file. Fall back to the UN-resolved path's
            # own containment before concluding "not a plan input";
            # resolved containment stays the primary test.
            try:
                _plan_rel_parts = _plan_candidate.relative_to(root).parts
            except ValueError:
                _plan_rel_parts = ()
    else:
        _plan_rel_parts = ()
    return (
        len(_plan_rel_parts) >= 2
        and _plan_rel_parts[0] == "docs"
        and _plan_rel_parts[1] == "plans"
    )


def _deliverable_id_for(rel_or_abs_path: Optional[str], root: Path) -> Optional[str]:
    """Read `deliverable_id` off `rel_or_abs_path` relative to `root`.

    Hoisted module-level (C2, staff-eng review finding 6): `_build_fan_in_
    cardinality_judgment_point` and `_build_divergent_deliverable_id_
    judgment_point` each carried an identical local of this shape; one
    definition keeps the union `resolve_lineage` computes and the
    narration those two judgment points build reading the same disk fact
    by construction. Delegates to `_read_frontmatter_field`, which never
    raises (returns `""` on an unreadable/missing file) -- so an
    unreadable predecessor leg is skipped here, matching `_scan_
    deliverable_collision`'s own guard over the same read, without a
    local try/except.
    """
    if not rel_or_abs_path:
        return None
    candidate = Path(rel_or_abs_path)
    full_path = candidate if candidate.is_absolute() else root / candidate
    value = _read_frontmatter_field(str(full_path), "deliverable_id")
    return value or None


def resolve_lineage(
    kind: str,
    artifact_path: str,
    root: Path,
    additional_predecessor_paths: Optional[list[str]] = None,
    title: Optional[str] = None,
    explicit_deliverable_id: Optional[str] = None,
    *,
    excise_rung: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve the `kind`-selected parent-discovery order into a lineage dict.

    `session_id`, when supplied, is the caller's ALREADY-RESOLVED id, passed
    through to the two prior-attempt adoption predicates below in place of
    their ambient environment read. `apply()` binds its resolved id into
    `apply_base.session_identity`'s per-var ContextVars, which
    `core.resolve_session_id` does not read -- so an explicit `--session-id`
    was invisible to those authorship gates, which then compared a candidate's
    `authoring_session` against whatever id happened to be in THIS PROCESS's
    environment. A caller with no id of its own keeps the ambient read.

    handoff: plan -> predecessor -> mint. Companion id `predecessor_id` is
        read off the SAME file the `predecessor` field names (never a
        different candidate -- C2 add-not-swap discipline). `discovery` for
        `kind == "handoff"`: `"plan"` (claimed-plan tier) / `"artifact"`
        (predecessor carry) / `"plan-input"` (predecessor carry relabeled
        when the predecessor IS the plan->execute trigger's own plan, not a
        real predecessor) / `"mint"`. `explicit_deliverable_id` is NOT
        consulted here -- the handoff cascade's own explicit tier lives
        entirely inside `coordinator-doc-new` (its `_mint_deliverable_id`
        carry-vs-mint precedence), reached only when that CLI is invoked
        directly with `--deliverable-id`, never through this function.
    spinoff (2026-08-05 PM ruling): a spinoff does NOT inherit a
        `deliverable_id` from its progenitor -- the artifact it forks from
        (`artifact_path`) -- by default. Before this ruling, ANY hit on
        `artifact_path`'s own `deliverable_id` frontmatter field was
        silently carried onto the fork, labeled `"stub"` (a
        handoff-schema-shaped hit, i.e. `artifact_path` carries a `kind`
        field) or `"plan"` (a kind-less hit, e.g. a `docs/plans/*.md`
        document, which never carries `kind`). That silent carry is the
        defect this ruling closes: a live spinoff inheriting an id with
        `predecessor: none` and no origin edge produces an
        un-diagnosable chain shape downstream (the collision scan's
        ancestry walk cannot tell an inherited id from an independent
        mint). The default path now MINTS a fresh id regardless of
        whether the progenitor's own `deliverable_id` field resolves,
        reported as `discovery == "mint"` -- the `"stub"`/`"plan"`
        tier labels above are retired for the inherit path they used to
        name (see `_walk_deliverable_ancestor_set`'s own docstring, which
        narrates the downstream symptom this closes). The explicit
        opt-in this ruling deliberately preserves is `--deliverable-id`
        supplied straight to `coordinator-doc-new`, unrelated to this
        function's own `explicit_deliverable_id` parameter below.
        `explicit_deliverable_id`, when the caller of THIS function
        supplies one (added for exactly this ruling; `brief()`'s own
        `explicit_deliverable_id` parameter is the caller that threads an
        EM-supplied `--deliverable-id` through from `main()`'s CLI parser,
        spinoff-only -- `brief()` raises loud rather than routing it here
        for `kind == "handoff"`, see `brief()`'s own docstring), is
        carried through UNCHANGED and reports `discovery == "explicit"`
        -- never re-minted, mirroring `_mint_deliverable_id`'s own
        carry-not-remint contract. `initiative` is UNTOUCHED by this
        ruling and keeps inheriting from `artifact_path`'s frontmatter
        exactly as before -- the PM ruling named `deliverable_id` only.
        Companion ids `origin_handoff_id` / `origin_session` /
        `origin_plan_id` / `origin_goal_id` are read off `artifact_path`
        (the parent stub/plan/handoff this spinoff forks from) when
        present -- unaffected by this ruling, which is `deliverable_id`-only.

    A `deliverable_id`/`initiative` resolved from an upstream artifact is
    CARRIED, never re-minted (D1) -- `discovery` in the returned dict names
    which tier of the order actually supplied it, or `"mint"` when none did.
    (`kind == "spinoff"`'s progenitor-carry tier is retired by the 2026-08-05
    ruling above; `kind == "handoff"`'s cascade is unaffected.)

    `artifact_path` is normalized (see `_normalize_artifact_path`) BEFORE
    anything else in this function reads it -- the frontmatter lookup below,
    the stored `lineage["artifact_path"]`, and (for `kind == "handoff"`)
    the predecessor-relative-path resolution all see the post-normalization
    value.

    `additional_predecessor_paths` (2026-07-29, N-predecessor succession
    fix): `kind="handoff"`-only fan-in edges beyond the primary predecessor
    -- populated only when `_resolve_held_handoff_for_session` self-resolved
    MORE than one held claim for this session (a session may legitimately
    hold two batons; before this fix that shape hard-failed as "ambiguous").
    Each entry gets the SAME archive-aware resolution the primary predecessor
    gets (`_resolve_qualified_path_or_raise`) -- a stale/archived additional
    predecessor resolves through the archive fallback exactly as the primary
    does, never silently dropped. Stored under `lineage["additional_
    predecessors"]`, the schema's OWN field name (`handoff.schema.json`) --
    no new field is introduced. Ignored entirely for `kind="spinoff"`, which
    has no predecessor axis at all.

    `title`, when supplied, is threaded straight through to
    `_compute_fresh_output_path` -- it is only ever CONSULTED there, and only
    when `artifact_path` is empty (the standalone-handoff mint case, which has
    no lineage source to derive a slug from); every other case's slug still
    derives from `artifact_path` alone, unchanged.

    `predecessor_ordering_degraded` (AC-6, folded candidate 7, 2026-08-13):
    `bool`, set ONLY inside the `kind == "handoff"` branch that self-resolves
    via `_resolve_held_handoff_for_session` (the `is_plan_input` ledger
    read) -- `True` when that resolver's composite ordering key could not
    fully distinguish two or more of the session's held claims (a set-level
    signal; see that function's own docstring for what it does and does not
    say about the primary pick), `False` when it ran and found a clean
    order. For every OTHER `kind`/branch -- including `kind == "spinoff"`
    entirely -- this key is ABSENT from the returned dict, not `False`; a
    caller that needs the value regardless of branch must use `.get()`.

    `excise_rung` (2026-08-14, break-glass excise reaching the cascade):
    `kind == "handoff"`-only. `None` (default) leaves the cascade untouched.
    `"plan_file"` drops `_plan_file` before the `resolve_deliverable_and_
    initiative` call -- used when the caller supplied an explicit
    `artifact_path` (the OPERATOR-NAMED rung wins). `"predecessor_file"`
    drops `_predecessor_file` instead -- used when `artifact_path` was
    empty and the predecessor was self-resolved from the claim ledger (the
    AUTO-DISCOVERED rung is the one cut). The caller (`brief`) computes
    which value to pass; this function only applies it.
    """
    # `was_bare_slug` distinguishes the bare-slug mint convention (fresh
    # output target -- legitimately absent on disk, nothing to archive-
    # search for) from a genuinely qualified caller-supplied path
    # (predecessor handoff / plan) that is missing -- `_normalize_artifact_path`
    # is a no-op on anything already qualified and only CHANGES a bare slug
    # (adds the date prefix + state/handoffs/ directory), so a changed value
    # here is exactly the bare-slug case; reusing that comparison keeps this
    # discriminator from drifting out of sync with the normalizer itself.
    _normalized = _normalize_artifact_path(artifact_path)
    was_bare_slug = bool(artifact_path) and _normalized != artifact_path
    artifact_path = _normalized
    # Absolute on-disk path `fm` was actually read from -- `None` when there
    # is nothing to read (bare-slug fresh-mint target, or no `artifact_path`
    # at all). Tracked separately from `artifact_path` itself (which the
    # branches below may rewrite to a repo-relative posix string) because the
    # `kind == "handoff"` deliverable-id cascade below needs a stable,
    # directly-openable path to hand `resolve_deliverable_and_initiative` as
    # its `predecessor` argument.
    _artifact_frontmatter_abs_path: Optional[Path] = None
    # Non-None only on a bare-slug REPLAY -- see `_adopt_prior_attempt_mint_path`.
    _adopted_mint_path: Optional[str] = None
    if artifact_path:
        _artifact_fm_path = Path(artifact_path)
        if not _artifact_fm_path.is_absolute():
            _artifact_fm_path = root / artifact_path
        if was_bare_slug:
            # MINT TARGET, never a lineage input -- whether or not a file is
            # already sitting there. `was_bare_slug` is tested BEFORE the
            # `is_file()` read (2026-08-25 break-class fix, bug backlog
            # `2026-08-25-spinoff-brief-then-apply-mints-two-batons-and-adopts-
            # the-stub-as-origin.yaml`): the previous ordering read whatever
            # occupied the mint path as `fm` and, for kind=spinoff, stamped its
            # `handoff_id` onto the new baton as `origin_handoff_id` (see the
            # `kind == "spinoff"` branch's `origin_own_handoff_id` gate). The
            # file occupying a mint path is by construction NOT an origin: it is
            # this run's OWN prior attempt, or an unrelated same-slug artifact.
            # Reproduced live: two `apply spinoff <slug>` calls 8s apart (the
            # first's stdout swallowed by a `Select-Object -First 120` pipe, so
            # the operator could not see it had succeeded) left two pickup_ready
            # batons for one topic, the survivor naming the abandoned first as
            # its own `origin_handoff` -- provenance pointing at a file a
            # tidy-up deletes.
            #
            # `fm` stays empty, so kind=spinoff falls through to
            # `handoff.author_fork`'s own claim-ledger self-resolution (the
            # session's ACTUAL held baton) exactly as it does on a first run.
            fm = ""
            _adopted_mint_path = _adopt_prior_attempt_mint_path(
                artifact_path, root, kind, session_id
            )
        elif _artifact_fm_path.is_file():
            fm = _read_frontmatter(_artifact_fm_path)
            _artifact_frontmatter_abs_path = _artifact_fm_path
        else:
            # Qualified path, missing at its named live location -- archive-
            # aware fail-loud resolution (2026-07-28 break-class fix). See
            # `_resolve_qualified_path_or_raise`'s own docstring.
            _resolved_fm_path = _resolve_qualified_path_or_raise(artifact_path, root, kind)
            try:
                artifact_path = _resolved_fm_path.relative_to(root).as_posix()
            except ValueError:
                artifact_path = str(_resolved_fm_path)
            fm = _read_frontmatter(_resolved_fm_path)
            _artifact_frontmatter_abs_path = _resolved_fm_path
    else:
        fm = ""

    # Fan-in resolution, HOISTED above the `kind == "handoff"` cascade call
    # (sedge-01, `succession-edge-cardinality` roadmap, R2): resolved ONCE
    # here, from `additional_predecessor_paths` as this function received it
    # -- BEFORE the `is_plan_input` branch below may extend that same local
    # with ledger-sourced extras (see `resolve_deliverable_and_initiative`'s
    # own "Known uncovered leg" docstring note: those ledger-sourced extras
    # are discovered only AFTER the cascade call and are not covered by this
    # widening). Reused verbatim at `lineage["additional_predecessors"]`
    # below -- no second walk of the same paths. Ignored for kind="spinoff",
    # which has no predecessor axis (see this function's own docstring).
    resolved_additional_predecessors: list[str] = (
        _resolve_additional_predecessor_paths(additional_predecessor_paths, root, kind)
        if kind == "handoff"
        else []
    )

    if kind == "handoff":
        # DR-207 DD#1 cascade: claimed plan -> predecessor artifact -> mint.
        # `resolve_claimed_plan_path` (C1a) is the session->plan link;
        # `resolve_deliverable_and_initiative` (C1b, relocated from
        # `coordinator/bin/handoff-deliverable-carry.py`) is the ONE
        # cascade implementation -- this call site observes which tier it
        # took via `_tracking_read_frontmatter_field` rather than
        # re-branching the plan/predecessor order itself (AC3).
        _claimed_plan_rel = resolve_claimed_plan_path(cwd=root)
        _plan_file = str(root / _claimed_plan_rel) if _claimed_plan_rel else None
        _predecessor_file = (
            str(_artifact_frontmatter_abs_path) if _artifact_frontmatter_abs_path else None
        )
        # Break-glass excise, reaching the cascade (2026-08-14): applied HERE,
        # before `resolve_deliverable_and_initiative` runs, so a divergent
        # rung is gone by the time it would otherwise raise
        # `DivergentDeliverableIdError` -- not after, when the raise has
        # already propagated. Cuts exactly ONE rung, per `excise_rung`'s own
        # docstring above; the other rung (and the carry it supplies) is
        # untouched.
        #
        # Inertness guard (2026-08-14, review finding P2): `excise_rung` on
        # its own does not know whether the two rungs actually diverge --
        # when both `_plan_file` and `_predecessor_file` are present and
        # agree on `deliverable_id`, there is nothing to rescue, and cutting
        # a rung anyway would silently swap which artifact
        # `resolve_deliverable_and_initiative`'s `initiative` fallback reads
        # from (it is resolved independently of `deliverable_id`, plan-file
        # first). Read directly here, BEFORE either rung is nulled -- the
        # cascade's own `_tracked` read (`_tracking_read_frontmatter_field`)
        # fires too late, after this cut has already happened. When either
        # rung is absent there is no agreement to detect (only one source
        # exists), so the cut proceeds as before.
        # Scope note: this guard concerns the two CARRYING rungs only.
        # Fan-in legs are excluded from the divergence check outright --
        # see `_cascade_additional_predecessors` below -- so no excise
        # decision here has anything to say about them.
        _rungs_diverge_on_deliverable_id = True
        if excise_rung in ("plan_file", "predecessor_file") and _plan_file and _predecessor_file:

            _rungs_diverge_on_deliverable_id = _read_frontmatter_field(
                _plan_file, "deliverable_id"
            ) != _read_frontmatter_field(_predecessor_file, "deliverable_id")
        if excise_rung == "plan_file" and _rungs_diverge_on_deliverable_id:
            _plan_file = None
        elif excise_rung == "predecessor_file" and _rungs_diverge_on_deliverable_id:
            _predecessor_file = None
        # Dropped-join guard, plan-input axis (2026-08-13): computed HERE,
        # BEFORE the cascade call, from `fm`/`artifact_path` this function
        # already holds -- the SAME predicate the `is_plan_input` relabel
        # below re-derives from the identical inputs, via the one shared
        # helper (`_predecessor_is_plan_input`), never a second copy. A plan
        # handed in as `_predecessor_file` (the plan->execute trigger's own
        # plan, never touching `_plan_file`) must arm the same refusal a
        # claimed plan already arms -- see `resolve_deliverable_and_
        # initiative`'s module docstring, "Dropped-join refusal, two arms".
        _predecessor_plan_input = _predecessor_is_plan_input(fm, artifact_path, root)
        _tracked_read, _discovery_tier = _tracking_read_frontmatter_field(
            _plan_file, _predecessor_file
        )
        # `DroppedDeliverableJoinError` (raised when an active claimed plan
        # names no deliverable_id and the predecessor fallback also yields
        # nothing, OR when `_predecessor_file` is itself a plan input that
        # names no deliverable_id) is NOT caught here -- it must propagate to
        # the caller rather than be swallowed into a silent mint-from-slug
        # (AC4).
        # Fan-in legs never reach the divergence check (2026-08-14). A leg
        # cannot become the carried id -- `resolve_deliverable_and_
        # initiative` pins the winner as `plan_dlvr_id or
        # predecessor_dlvr_id` -- so a leg whose id differs raises a question
        # with no answer: there is no winner to pick, only a drop. Feeding
        # them in made the ordinary N->1 fan-in (Resolution 1: "correct and
        # stays") fail outright, which also made `j-fan-in-cardinality`
        # unreachable in exactly the case it exists to narrate.
        #
        # The signal is not lost, it is relocated to where it is actionable:
        # `j-fan-in-cardinality` names every additional predecessor's
        # deliverable_id and states that it is dropped, not carried. That is
        # the whole content of a fan-in "divergence".
        #
        # Negative-spec: this is NOT a widening of the no-auto-pick refusal.
        # Divergence between the two CARRYING rungs still raises and still
        # routes to `j-divergent-deliverable-id` -- see the sibling
        # `excise_rung` block above. Only rungs that were never candidates
        # are excluded.
        _cascade_additional_predecessors: list[str] = []

        deliverable_id, initiative = resolve_deliverable_and_initiative(
            _tracked_read,
            _mint_deliverable_id,
            _plan_file,
            _predecessor_file,
            additional_predecessors=_cascade_additional_predecessors,
            predecessor_is_plan_input=_predecessor_plan_input,
        )
        discovery = _discovery_tier[0] if _discovery_tier else "mint"
    else:
        # kind == "spinoff" (2026-08-05 PM ruling -- see this function's own
        # docstring for the full defect/fix narrative): a spinoff does NOT
        # inherit `deliverable_id` from `artifact_path` (the progenitor it
        # forks FROM) by default -- the read that used to feed the retired
        # `"stub"`/`"plan"` tiers below is gone. `initiative` is UNCHANGED --
        # the ruling named `deliverable_id` only, so it keeps reading off the
        # SAME `fm` (the progenitor's frontmatter) exactly as before this fix.
        #
        # `explicit_deliverable_id` is the ONE opt-in this ruling sanctions
        # for THIS function (see docstring): a caller who explicitly supplies
        # one gets it back unchanged, labeled `"explicit"`, never re-minted --
        # never read off `fm`. No caller in this repo passes it yet (see
        # docstring) -- this is the landing spot for a future `brief()` param
        # threading an EM-supplied `--deliverable-id` through, not a currently
        # reachable path from `main()`/`brief()`'s own signatures.
        initiative = _fm_field(fm, "initiative")
        if explicit_deliverable_id:
            deliverable_id = explicit_deliverable_id
            discovery = "explicit"
        else:
            deliverable_id = None
            discovery = "mint"

    lineage: dict[str, Any] = {
        "kind": kind,
        "artifact_path": artifact_path,
        "deliverable_id": deliverable_id,
        "initiative": initiative,
        "discovery": discovery,
        # `output_path` is the FRESH path d1 scaffolds the new artifact at --
        # deliberately never `artifact_path` itself when that names an
        # EXISTING input (the plan/predecessor/origin this brief reads
        # lineage from). See `_compute_fresh_output_path`'s own docstring
        # for the destructive-collision this closes. Kind-agnostic:
        # idempotent (== artifact_path) for the bare-slug mint convention
        # both kinds share, genuinely fresh for a qualified existing input.
        "output_path": _compute_fresh_output_path(artifact_path, root, title=title),
        # Non-None only on a REPLAY: the successor path a prior attempt of this
        # same run already recorded on the predecessor, which `output_path` is
        # then pinned to instead of a freshly-disambiguated one. See
        # `_resume_recorded_successor_path`. Always present (never merely
        # absent) so "this is a first run" and "this lineage dict does not say"
        # read differently -- the same reasoning as `apply()`'s own `commits`
        # field. kind=spinoff has no predecessor and therefore never resumes.
        "resumed_successor": None,
        # Non-None only when a prior attempt aborted BEFORE d6 and left a
        # scaffold `_compensate_d1_scaffold` declined to delete: the surviving
        # file's path, adopted as `output_path` so the re-run re-uses it instead
        # of minting beside it. Kept as its own field rather than folded into
        # `resumed_successor` because the two record DIFFERENT evidence classes
        # -- predecessor-side (`continued_into`, which d6 wrote) versus
        # successor-side (the survivor's own `predecessor:` pointer, admitted
        # for this one path decision by DR-242 Amendment A1) -- and an audit of
        # the carve-out's reach needs to be able to tell them apart. Present-as-
        # None on every run, for the same reason as `resumed_successor`.
        "adopted_scaffold": None,
        # Non-None only when the BARE-SLUG mint path was already occupied by
        # this run's own prior attempt: that path, adopted as `output_path` so
        # the re-run converges onto the existing baton (d1 goes
        # `already_satisfied`) instead of minting a second one beside it. Its
        # own field rather than folded into `adopted_scaffold` for the same
        # reason that one is not folded into `resumed_successor`: a DIFFERENT
        # evidence class (the mint path is deterministic from the slug; nothing
        # about a predecessor edge is consulted or concluded). Present-as-None
        # on every run. See `_adopt_prior_attempt_mint_path`.
        "adopted_mint_path": None,
    }

    if _adopted_mint_path:
        # Applied BEFORE the kind branches, so kind=handoff's own
        # predecessor-side resumption (`_resume_recorded_successor_path`, and
        # then `_adopt_prior_attempt_scaffold_path`) still overrides it where it
        # has evidence -- that ordering keeps the stronger, predecessor-side
        # evidence class first, unchanged by this fix.
        lineage["output_path"] = _adopted_mint_path
        lineage["adopted_mint_path"] = _adopted_mint_path

    if kind == "handoff":
        # Discriminator: does `artifact_path` carry its OWN `handoff_id` --
        # i.e. is it itself a real handoff record (the file this session was
        # opened with, per `coordinator/CLAUDE.md § Handoff Lineage` /
        # `handoff/SKILL.md § Predecessor identification`: "the predecessor
        # is whatever handoff this session was opened with -- period"), or
        # is it a non-handoff lineage source (the plan->execute trigger's
        # PLAN input, which carries no `handoff_id` of its own)?
        #
        # 2026-07-27 break-class fix (bug backlog entry for this session's
        # reproduction): when `artifact_path` IS itself a handoff, it must
        # become the new successor's predecessor DIRECTLY -- reading a
        # `predecessor:` field OFF of it instead walks one generation too
        # far (artifact_path's own PARENT), which is exactly what stranded
        # the true parent as claimed-but-never-continued while `d6`
        # superseded the grandparent instead. `schemas/handoff.schema.json`'s
        # own `predecessor_id` field description confirms the intended
        # semantics: "the predecessor's handoff_id, resolved ... from the
        # SAME predecessor the [predecessor] path field names" -- describing
        # a value being STAMPED ONTO the new successor, not walked from an
        # existing artifact's own frontmatter.
        #
        # A plan carries no `handoff_id` (it is not a handoff record), so it
        # falls to the second branch and contributes its own lineage pointer
        # instead -- `predecessor_handoff:` (the plan-authoring field name;
        # see e.g. this repo's own
        # `docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md`
        # frontmatter) with a bare `predecessor:` fallback for a hand-
        # authored/legacy artifact that used the handoff field name instead.
        own_handoff_id = _fm_field(fm, "handoff_id")
        # 2026-08-06 break-class fix: `handoff_id` is an OPTIONAL-omit field
        # on d1's own scaffold (`coordinator-doc-new` only stamps it when a
        # caller threads one through -- see its own `if handoff_id:` guard),
        # so a genuine, freshly-claimed handoff record routinely has NO
        # `handoff_id` at all -- not just a legacy/hand-authored edge case,
        # as Finding 5's original residual-risk note (below) assumed. Live
        # reproduction this fix closes: a consumed handoff whose own
        # `predecessor:` field was already set (continuation phase) fell to
        # the `else` branch below purely because `handoff_id` was absent,
        # walking one generation too far to ITS predecessor (the grandparent)
        # -- exactly the bug the 2026-07-27 fix above already closed for the
        # handoff_id-present case, reopened for the equally-common
        # handoff_id-absent one. `kind:` is a second, independent signal that
        # `artifact_path` is a real handoff record: every handoff schema
        # value (`session-handoff`, `roadmap-baton`, ...) stamps `kind:`,
        # while a plan (this branch's other legitimate input, gated by
        # `plan_id` + `docs/plans/` below) never carries the field at all --
        # confirmed by this module's own `TestHandoffInputBecomesItsOwnPredecessor.
        # test_no_own_handoff_id_falls_back_to_predecessor_handoff_field` fixture,
        # which sets neither `handoff_id` nor `kind` and still expects the
        # `else`-branch field-walk. `is_own_handoff_record` widens the
        # discriminator from "carries handoff_id" to "carries handoff_id OR
        # kind" without touching `predecessor_id` (still sourced from
        # `own_handoff_id` alone, `None` when absent -- schema-optional stays
        # schema-optional).
        is_own_handoff_record = bool(own_handoff_id) or bool(_fm_field(fm, "kind"))
        # 2026-07-27 review note (Finding 5, residual risk -- deliberately
        # NOT fail-loud): this discriminator trusts `handoff_id`'s presence
        # alone, not `artifact_path`'s location. A legacy/hand-authored/
        # corrupted handoff record missing `handoff_id` (a schema violation
        # not otherwise validated/rejected here) silently falls to the
        # `else` branch below and reads its OWN `predecessor:` field --
        # reproducing, for that one input shape, the grandparent-walking bug
        # this discriminator exists to fix, with no error or warning.
        # A path-location heuristic ("is artifact_path under a handoffs/
        # directory?") was considered and rejected: this module's own test
        # suite (see `TestKindParametrizedCascade::
        # test_handoff_kind_resolves_predecessor_order_and_predecessor_id_
        # companion`) deliberately seeds a real on-disk file under
        # `state/handoffs/` that carries no `handoff_id` and asserts the
        # `else`-branch predecessor-field walk as the CORRECT behavior for
        # it (modeling "the non-handoff (plan->execute) lineage-source
        # tier") -- so directory location does not reliably distinguish a
        # real handoff record from another lineage-source shape in this
        # codebase's own model, and a location-based fail-loud gate would
        # make that legitimate case an error. Every handoff minted via
        # `coordinator-doc-new` carries `handoff_id` UNLESS scaffolded with no
        # --title (placeholder-title guard, 2026-08-05: a title-less scaffold
        # refuses to mint handoff_id rather than bake a durable id from
        # placeholder text — see _is_placeholder_title in coordinator-doc-new),
        # which widens this residual risk beyond legacy/hand-authored inputs
        # to any freshly-scaffolded, still-untitled handoff. Still narrow: the
        # gap closes the moment the title lands and the record is re-minted,
        # and it is limited to this window, not closed for good.
        # C4 (2026-08-02, roadmap-baton-supersession-hazard plan): plan-ness
        # discriminator, checked AHEAD of the `else` field-walk below and
        # AFTER the `own_handoff_id` check above (never disturbing either).
        # Every plan carries `plan_id` (this function already reads it for
        # `origin_plan_id` in the spinoff branch below); no handoff record
        # does. `docs/plans/` path containment is a SECONDARY confirmatory
        # check -- an artifact that merely carries a `plan_id`-shaped field
        # but does not live under `docs/plans/` falls through UNCHANGED to
        # the existing field-walk rather than being routed to the ledger on
        # a weaker signal alone.
        #
        # THE DEFECT this closes: DoE's `plan.schema.json` defines
        # `predecessor_handoff` as the handoff that SPAWNED this plan
        # (provenance) -- this module used to consume it as the handoff to
        # TERMINATE (a different relation). The actual supersession target
        # for a plan input is the session's OWN durable handoff-claims
        # ledger entry (`_resolve_held_handoff_for_session`, the SAME
        # authoritative source `brief()` already reads for its empty-
        # `artifact_path` self-resolution case) -- reading it here routes
        # the plan case through that same ledger rather than opting out of
        # it merely because `artifact_path` was supplied non-empty.
        # `predecessor_handoff` itself is still CARRIED on `lineage` for
        # lineage-carry purposes; it is simply not a termination target.
        # Re-derived via the single shared helper -- see the pre-cascade
        # call site above (and `_predecessor_is_plan_input`'s own
        # docstring) for the "one definition, two call sites" contract;
        # re-derived here, from the identical `fm`/`artifact_path`, only
        # because `is_own_handoff_record` (computed above, for OTHER
        # purposes this block needs) was not yet available earlier.
        is_plan_input = _predecessor_is_plan_input(fm, artifact_path, root)

        # C4 (2026-08-03, deliverable-id-carry-plan-handoff-agree plan, AC7):
        # `_tracking_read_frontmatter_field` above tags a hit against
        # `_predecessor_file` as `"artifact"` regardless of WHAT that file is
        # -- for the plan->execute trigger, `_predecessor_file` IS
        # `artifact_path`, i.e. the plan itself, so a plan-carried
        # `deliverable_id` came back labeled `"artifact"`: accurate about the
        # mechanism (the predecessor rung), misleading about the meaning (a
        # reader cannot tell this apart from a coincidental predecessor
        # carry). Relabeled to `"plan-input"` -- deliberately NOT `"plan"`,
        # which `_tracking_read_frontmatter_field` already reserves for the
        # CLAIMED-plan tier (`_plan_file`, a different file); overloading it
        # would make `discovery == "plan"` ambiguous between two tiers this
        # module's own test suite pins as distinct. Only fires when the
        # `"artifact"` tier is what actually produced the id (never
        # overwrites `"plan"` or `"mint"`), so it can never disagree with
        # which path the cascade took.
        if is_plan_input and lineage["discovery"] == "artifact":
            lineage["discovery"] = "plan-input"

        lineage["plan_ledger_no_claim"] = None
        lineage["predecessor_handoff"] = None
        # Ledger-sourced additional-predecessor paths discovered by the
        # `is_plan_input` branch below (`_resolve_held_handoff_for_session`'s
        # second return value) -- captured into this OUTER local, separate
        # from `resolved_additional_predecessors` (already resolved above,
        # ahead of the cascade call), rather than merged into it. See the
        # `lineage["additional_predecessors"]` assignment below for why: the
        # two sets are resolved once each, never re-walked, then concatenated
        # there (sedge-01 R2/R3, EM-clarified 2026-08-11 -- the field itself
        # must NOT lose these entries, only the widened cascade's own
        # divergence check is blind to them, since they are discovered only
        # after that check already ran).
        _ledger_extra_paths: list[str] = []
        # Default: only the `is_plan_input` branch below (the sole call site
        # that self-resolves via `_resolve_held_handoff_for_session`) ever has
        # a degradation fact to report -- every other branch's `predecessor`
        # comes straight off a frontmatter field, with no composite-key
        # ordering involved at all, so `False` (not `None`) is the accurate
        # "nothing degraded, because nothing was ordered" answer, not a gap.
        lineage["predecessor_ordering_degraded"] = False
        if is_own_handoff_record:
            predecessor = artifact_path
            predecessor_id = own_handoff_id
        elif is_plan_input:
            _provenance_predecessor_handoff = _fm_field(fm, "predecessor_handoff") or _fm_field(
                fm, "predecessor"
            )
            lineage["predecessor_handoff"] = (
                _provenance_predecessor_handoff
                if _provenance_predecessor_handoff not in ("none", None, "")
                else None
            )
            # Leg 1 (this chunk, C6): the plan's OWN declared
            # `predecessor_handoff`/`predecessor` field wins outright when it
            # resolves to a file on disk -- an author-asserted edge is
            # descent evidence on its own (same reasoning
            # `deliverable_carry.py :: resolve_explicit_predecessor_
            # deliverable_id`'s docstring gives for treating an explicit
            # `predecessor_path` as stronger than a session-held claim).
            # Before this fix the ledger-held handoff below was consulted
            # UNCONDITIONALLY, discarding this declaration even when it
            # named a real file -- the defect this plan's problem statement
            # names.
            _declared_predecessor_path: Optional[Path] = None
            if lineage["predecessor_handoff"]:
                _declared_candidate = Path(lineage["predecessor_handoff"])
                if not _declared_candidate.is_absolute():
                    _declared_candidate = root / lineage["predecessor_handoff"]
                if _declared_candidate.is_file():
                    _declared_predecessor_path = _declared_candidate
            if _declared_predecessor_path is not None:
                predecessor_path = _declared_predecessor_path
                try:
                    predecessor = predecessor_path.relative_to(root).as_posix()
                except ValueError:
                    predecessor = str(predecessor_path)
                predecessor_fm = _read_frontmatter(predecessor_path)
                predecessor_id = _fm_field(predecessor_fm, "handoff_id")
                lineage["predecessor_ordering_degraded"] = False
            else:
                try:
                    predecessor, _ledger_additional, _ledger_degraded = (
                        _resolve_held_handoff_for_session(root)
                    )
                except ValueError as exc:
                    predecessor = None
                    predecessor_id = None
                    lineage["plan_ledger_no_claim"] = str(exc)
                    lineage["predecessor_ordering_degraded"] = False
                else:
                    predecessor_path = Path(predecessor)
                    if not predecessor_path.is_absolute():
                        predecessor_path = root / predecessor
                    if not predecessor_path.is_file():
                        # Review: coordinatorcode-reviewer-c2d43fc7 Finding 1 --
                        # the ledger's returned basename may already have moved to
                        # `archive/handoffs/` (`_resolve_held_handoff_for_session`'s
                        # own reason for existing); route it through the same
                        # archive-aware fail-loud resolution
                        # `_resolve_additional_predecessor_paths` already applies
                        # to its own entries, rather than letting
                        # `_read_frontmatter`'s missing-path silence starve
                        # `predecessor_id` and defeat
                        # `_resolved_predecessor_canonical_kind`'s own roadmap-baton
                        # gate for an archived predecessor.
                        predecessor_path = _resolve_qualified_path_or_raise(
                            predecessor, root, kind
                        )
                    predecessor_fm = _read_frontmatter(predecessor_path)
                    # Leg 2 (C6): a ledger-held handoff is only accepted as
                    # this plan's predecessor when it ACTUALLY shares lineage
                    # with the plan -- its own `governing_plan` names this
                    # plan (`artifact_path`), or it carries the same
                    # `deliverable_id` this plan resolved above. Holding a
                    # claim is bookkeeping, not an attestation (DR-294,
                    # extended here to the predecessor edge the same way C4
                    # extends it to sizing objects) -- an unrelated baton the
                    # session merely happens to be holding must not be
                    # wired in as descent.
                    _held_governing_plan = _fm_field(predecessor_fm, "governing_plan")
                    _held_deliverable_id = _fm_field(predecessor_fm, "deliverable_id")
                    _shares_governing_plan = bool(_held_governing_plan) and (
                        _repo_relative_posix(_held_governing_plan, root)
                        == _repo_relative_posix(artifact_path, root)
                    )
                    _shares_deliverable_id = bool(deliverable_id) and (
                        _held_deliverable_id == deliverable_id
                    )
                    if _shares_governing_plan or _shares_deliverable_id:
                        lineage["predecessor_ordering_degraded"] = _ledger_degraded
                        _ledger_extra_paths = list(_ledger_additional)
                        # Reflect the resolved (possibly archived) location back
                        # onto `predecessor` itself -- it feeds `lineage["predecessor"]`
                        # a few lines below, which is in turn what
                        # `_resolved_predecessor_canonical_kind` (C3's roadmap-baton
                        # gate) and `predecessor_is_live` resolve against. Leaving
                        # `predecessor` as the stale ledger basename here would fix
                        # `predecessor_id` while still handing C3's gate a path that
                        # fails its own `is_file()` check.
                        try:
                            predecessor = predecessor_path.relative_to(root).as_posix()
                        except ValueError:
                            predecessor = str(predecessor_path)
                        predecessor_id = _fm_field(predecessor_fm, "handoff_id")
                    else:
                        # Leg 3: the held claim shares no lineage with this
                        # plan -- an absent predecessor is the correct value
                        # for a plan descending from a sizing object, not a
                        # gap to fill by guessing which unrelated baton the
                        # session happens to hold.
                        predecessor = None
                        predecessor_id = None
                        lineage["predecessor_ordering_degraded"] = False
        else:
            predecessor = _fm_field(fm, "predecessor_handoff") or _fm_field(fm, "predecessor")
            predecessor_id = None
            if predecessor and predecessor not in ("none", ""):
                predecessor_path = Path(predecessor)
                if not predecessor_path.is_absolute():
                    predecessor_path = root / predecessor
                predecessor_fm = _read_frontmatter(predecessor_path)
                predecessor_id = _fm_field(predecessor_fm, "handoff_id")
        lineage["predecessor"] = predecessor if predecessor not in ("none", None, "") else None
        lineage["predecessor_id"] = predecessor_id

        # R5 (2026-08-21, rebuild-the-three-ceremony-assemblers plan C5): the
        # baton's own plan path, stamped at mint and carried across the
        # successor hop -- mirrors deliverable_id's carry-not-remint shape
        # (this function's own "plan -> predecessor -> mint" order), but
        # stores the PATH, not an id, matching `state/sizings/*.yaml`'s
        # literal `plan:` field shape (the in-tree pattern this chunk copies,
        # per its own dispatch brief -- do NOT invent a new mechanism).
        #
        # Precedence: a FRESH plan rung (the plan->execute trigger itself
        # arriving as `artifact_path`, or this session's own currently-
        # claimed plan) always wins over a carried value -- an active plan
        # claim is never masked by a stale carry. Absent either fresh rung, a
        # continuation baton (`artifact_path` IS the predecessor's own
        # handoff record, `is_own_handoff_record`) carries the predecessor's
        # own `governing_plan` frontmatter field forward unchanged -- `fm` is
        # already that predecessor's frontmatter at this point in the
        # function, so this is a read, not a second file open. That is the
        # successor-hop carry this chunk's dispatch brief names as load-
        # bearing (RULED, not a judgment point).
        #
        # Absent plan is the COMMON case and must be cheap and truthful: no
        # search, no warning-on-absence-at-mint -- `None` when none of the
        # three rungs supplies a value.
        _governing_plan_raw: Optional[str] = None
        if is_plan_input:
            _governing_plan_raw = artifact_path or None
        elif _plan_file:
            _governing_plan_raw = _plan_file
        elif is_own_handoff_record:
            _carried_governing_plan = _fm_field(fm, "governing_plan")
            _governing_plan_raw = (
                _carried_governing_plan
                if _carried_governing_plan not in (None, "", "none")
                else None
            )
        lineage["governing_plan"] = (
            _repo_relative_posix(_governing_plan_raw, root) if _governing_plan_raw else None
        )
        # `predecessor_is_live` (2026-07-28, archive-aware resolution follow-
        # up): whether `lineage["predecessor"]` names a file under THIS
        # worktree's live `state/handoffs/` -- computed here (root-relative,
        # absolute-or-relative-safe) rather than as a string-prefix check on
        # `lineage["predecessor"]` downstream, because that field legitimately
        # carries either an absolute path (the common case: `artifact_path`
        # was supplied absolute) or a repo-relative one (a frontmatter-field
        # value) -- a bare `.startswith("state/handoffs/")` string check
        # would silently mis-classify the absolute-path shape as "not live".
        # `_build_directives`'s d6 gate reads this flag rather than re-
        # deriving the containment check itself, matching the module's own
        # style rule of one definition per predicate.
        if lineage["predecessor"]:
            _pred_path = Path(lineage["predecessor"])
            if not _pred_path.is_absolute():
                _pred_path = root / lineage["predecessor"]
            try:
                _pred_path.relative_to(root / "state" / "handoffs")
                # 2026-08-06 break-class fix: containment alone (does the
                # STRING sit under `state/handoffs/`?) says nothing about
                # whether a file actually lives there -- `lineage["predecessor"]`
                # can carry a `state/handoffs/...` string read straight off a
                # frontmatter field (the `else`-branch field-walk above never
                # runs it through `_resolve_qualified_path_or_raise`), which
                # is exactly the shape an already-archived, terminal
                # predecessor produces: its basename still SPELLS a live
                # path, it just does not exist there anymore. `.is_file()`
                # is the same existence check `_resolve_qualified_path_or_raise`
                # already gates its archive fallback on -- reused here rather
                # than re-deriving a second existence predicate.
                lineage["predecessor_is_live"] = _pred_path.is_file()
            except ValueError:
                lineage["predecessor_is_live"] = False
        else:
            lineage["predecessor_is_live"] = False

        # Fan-in predecessors (2026-07-29, N-predecessor succession fix --
        # see this function's own docstring). Two DISJOINT sets, each
        # resolved exactly once (sedge-01 R2's no-double-walk intent, EM-
        # clarified 2026-08-11): `resolved_additional_predecessors` was
        # already computed above, ahead of the `kind == "handoff"` cascade
        # call, from the paths this function received as its own argument;
        # `_ledger_extra_paths` is resolved HERE, for the first time, from
        # the ledger-sourced extras `is_plan_input` discovered above (only
        # discoverable after that point -- `_resolve_held_handoff_for_session`
        # runs inside the branch). Neither set overlaps the other, so
        # concatenating them re-walks nothing. This field is therefore
        # restored to its pre-widening content in full -- the widened
        # cascade's own divergence check is the ONLY thing blind to
        # `_ledger_extra_paths` (it ran before these paths were known; see
        # `resolve_deliverable_and_initiative`'s "Known uncovered leg"
        # docstring note), not this field. `None` when empty, not `[]` --
        # matches this module's existing convention for optional array-valued
        # lineage fields (see `origin_goal_id` below), so "no additional
        # predecessors" and "this lineage dict does not populate the field"
        # read the same way.
        lineage["additional_predecessors"] = (
            resolved_additional_predecessors
            + _resolve_additional_predecessor_paths(_ledger_extra_paths, root, kind)
        ) or None

        # `deliverable_ids` / `plan_ids` (C2): ordered, deduplicated union of
        # the primary rung's already-resolved id followed by each additional
        # predecessor's OWN id, read off ITS frontmatter via the hoisted
        # `_deliverable_id_for` (unreadable/frontmatter-less legs read back
        # `None` and are skipped, matching `_scan_deliverable_collision`'s
        # own guard -- see that helper's docstring). Order is the fan-in
        # order this function just finalized above (primary, then
        # `lineage["additional_predecessors"]` verbatim: caller-argv order
        # followed by ledger-discovery order) -- NOT sorted, NOT earliest-
        # claimed order; both segments are order-preserving through
        # `_resolve_additional_predecessor_paths` (its archive-aware
        # fallback rewrites a path in place, never reorders), so deriving
        # off the finalized `additional_predecessors` list is sound. THIS
        # FUNCTION OWNS the 2+ threshold (staff-eng review finding 8) --
        # `None`, never `[]`, when fewer than 2 distinct ids result, the
        # same optional-array convention `additional_predecessors` itself
        # follows.
        _extra_paths_for_union = lineage["additional_predecessors"] or []

        def _ordered_unique(values: list[Optional[str]]) -> Optional[list[str]]:
            # `real_id`, not bare truthiness: `origin_plan_id` survives
            # frontmatter parsing as the STRING "null" on 80 live batons, and
            # "null" is truthy. Without this filter a fan-in whose one
            # predecessor carries a real plan id and whose other carries the
            # null sentinel emits `plan_ids: ["null", "pln-real-..."]` -- a
            # lineage carrier holding a non-id that `sizing_disposition.
            # cited_plan_fks` reads as a citation. No record has hit that yet
            # (plan_ids is 0/772), which is exactly why it is cheap to close now.
            seen: set[str] = set()
            ordered: list[str] = []
            for value in values:
                real = real_id(value)
                if real and real not in seen:
                    seen.add(real)
                    ordered.append(real)
            return ordered if len(ordered) >= 2 else None

        def _field_for(rel_or_abs_path: Optional[str], field: str) -> Optional[str]:
            if not rel_or_abs_path:
                return None
            candidate = Path(rel_or_abs_path)
            full_path = candidate if candidate.is_absolute() else root / candidate
            return _read_frontmatter_field(str(full_path), field) or None

        lineage["deliverable_ids"] = _ordered_unique(
            [lineage.get("deliverable_id")]
            + [_deliverable_id_for(_extra, root) for _extra in _extra_paths_for_union]
        )
        lineage["plan_ids"] = _ordered_unique(
            [_field_for(lineage.get("predecessor"), "origin_plan_id")]
            + [_field_for(_extra, "origin_plan_id") for _extra in _extra_paths_for_union]
        )

        # `carried_items` fan-in carry (sizing 2026-08-30-multi-baton-
        # pickup-mints-a-successor-bat, `injection_rule`): union over the
        # primary predecessor plus every fan-in leg, ROWS ONLY -- never
        # prose. Identity is `carry_id`: the same id appearing on two priors
        # de-duplicates to ONE row (first-seen order: primary, then fan-in
        # legs in their finalized order), while two DIFFERENT ids with
        # identical description text stay two separate rows. Rows whose
        # disposition is `"carried"` or `"blocked"` are injected;
        # `closed`/`spun_off` are not. `closed` terminated in place and
        # `spun_off` names its own successor baton in `disposition_detail`,
        # so both leave a reachable trace once this prior archives --
        # `blocked` leaves NONE: it is an item still open on an external
        # dependency, and the schema requires it to "remain visible and
        # explicit rather than silently re-carried". Dropping it at the hop
        # makes that dependency invisible the moment the prior archives,
        # which is the one failure the disposition exists to prevent. It is
        # injected with its disposition UNCHANGED -- visibility, never a
        # re-open; the successor re-declares its state like any other row.
        # Not the same axis as handoff_carry_gate._TERMINAL_DISPOSITIONS
        # (that set = "requires a disposition_detail") -- `blocked`'s
        # membership in that detail-required set does not imply it should
        # be excluded from this union's visibility axis.
        # Collision ruling (2026-08-30, review-integrator on this slice):
        # when a retained `carried` row and an incoming `blocked` row share
        # a `carry_id`, `blocked` wins and replaces the row IN PLACE (same
        # list position) -- `blocked` is strictly more informative (names
        # an external dependency, carries a mandatory disposition_detail)
        # and losing it loses information nothing downstream can
        # reconstruct, while losing a `carried` in favour of `blocked`
        # loses nothing. Asymmetric on purpose: a retained `blocked` is
        # NEVER downgraded by a later `carried`. `closed`/`spun_off` are
        # never injected here, so this is a two-value rule, not a
        # precedence table.
        # A leg with no readable `carried_items` (absent field, unparseable
        # frontmatter, or a shape `handoff_carry_gate.read_carried_items`
        # itself refuses) contributes nothing and is never fatal here --
        # d7's own gate already covers the PRIMARY predecessor's shape
        # before this ever runs; a fan-in leg's malformed carried_items is
        # the same "drop, don't crash" posture `_deliverable_id_for` already
        # takes on this exact leg list.
        _carried_items_paths = [lineage.get("predecessor")] + list(_extra_paths_for_union)
        _carried_items_seen: dict[str, int] = {}
        _carried_items_union: list[dict[str, Any]] = []
        for _ci_path in _carried_items_paths:
            if not _ci_path:
                continue
            _ci_candidate = Path(_ci_path)
            _ci_abs = _ci_candidate if _ci_candidate.is_absolute() else root / _ci_candidate
            if not _ci_abs.is_file():
                continue
            try:
                from coordinator_core.ops.handoff_carry_gate import (
                    read_carried_items as _read_carried_items,
                )

                _ci_items = _read_carried_items(str(_ci_abs))
            except Exception:
                continue
            for _ci_item in _ci_items:
                if not isinstance(_ci_item, dict):
                    continue
                if _ci_item.get("disposition") not in ("carried", "blocked"):
                    continue
                _ci_id = _ci_item.get("carry_id")
                if not _ci_id or not isinstance(_ci_id, str):
                    continue
                if _ci_id in _carried_items_seen:
                    _ci_existing_idx = _carried_items_seen[_ci_id]
                    _ci_existing = _carried_items_union[_ci_existing_idx]
                    if (
                        _ci_existing.get("disposition") == "carried"
                        and _ci_item.get("disposition") == "blocked"
                    ):
                        _carried_items_union[_ci_existing_idx] = _ci_item
                    continue
                _carried_items_seen[_ci_id] = len(_carried_items_union)
                _carried_items_union.append(_ci_item)
        lineage["carried_items"] = _carried_items_union or None

        # DR-388 (2026-08-30, PM ruling): a fan-in successor (2+
        # predecessors) mints a BRAND NEW deliverable_id by construction,
        # never one carried or reused from any rung -- deliberate, narrow
        # departure from DR-207 DD#1, scoped to N>1 only. Fired AFTER the
        # `deliverable_ids`/`plan_ids` unions above so those carriers still
        # name the priors' own real ids for traceability -- the fresh mint
        # is the successor's own identity, not a fourth entry unioned into
        # `deliverable_ids`. Single-predecessor carry-verbatim (DD#1) is
        # untouched: this fires only when a real fan-in leg is present.
        if lineage.get("additional_predecessors"):
            _fan_in_mint_slug = title or Path(lineage["output_path"]).stem
            lineage["deliverable_id"], _ = _mint_deliverable_id(slug=_fan_in_mint_slug)
            lineage["discovery"] = "fan-in-mint"

        # Replay resumption (2026-07-29): pin `output_path` to the successor a
        # prior attempt already recorded on THIS predecessor, rather than
        # letting `_compute_fresh_output_path` disambiguate away from it and
        # mint a second baton the predecessor's `continued_into` will then
        # refuse to be repointed at. Computed here, at the single point
        # `output_path` is established, so d1's `--out`, d6's
        # `successor_path`/`exclude`, and the envelope's own reported value all
        # see the SAME string -- the same one-definition rule the
        # `_normalize_artifact_path` threading already follows.
        if lineage["predecessor"]:
            _resumed = _resume_recorded_successor_path(lineage["predecessor"], root)
            if _resumed:
                lineage["output_path"] = _resumed
                lineage["resumed_successor"] = _resumed
            else:
                # Predecessor-side evidence is absent -- an abort BEFORE d6. The
                # surviving scaffold's OWN `predecessor:` pointer is the only
                # fact left, admitted for this ONE path decision by DR-242
                # Amendment A1. Strictly second in precedence, so the two
                # evidence classes never compete; see
                # `_adopt_prior_attempt_scaffold_path`.
                _adopted = _adopt_prior_attempt_scaffold_path(
                    lineage["predecessor"], lineage.get("predecessor_id"), root,
                    session_id,
                )
                if _adopted:
                    lineage["output_path"] = _adopted
                    lineage["adopted_scaffold"] = _adopted
    elif kind == "spinoff":
        # 2026-07-27 break-class fix (live /spinoff run self-stamped
        # origin_handoff onto its own freshly-minted file): the sanctioned
        # `/spinoff <slug>` calling convention (coordinator/skills/spinoff/
        # SKILL.md) passes the NEW artifact's own mint slug as
        # `artifact_path` -- there is no distinct existing origin file to
        # read in that shape, so `fm` is empty and `artifact_path` names
        # nothing but the about-to-be-scaffolded output. Unconditionally
        # echoing `artifact_path` here (the pre-fix line) stamped that
        # not-yet-real path onto the fork as its own origin. Gate on
        # `own_handoff_id` -- the SAME discriminator the kind=="handoff"
        # branch above already uses ("does this artifact carry ITS OWN
        # handoff_id, i.e. is it a real, existing handoff record") -- so
        # origin_handoff/origin_handoff_id are only ever sourced from
        # `artifact_path` when it names a genuine pre-existing origin
        # baton (the "stub" tier of the documented stub -> plan -> mint
        # discovery order). Otherwise both come out None here and the
        # caller (`_dispatch_handoff_author_fork`) passes that through
        # unset, letting `handoff.author_fork`'s own self-resolution
        # (`_resolve_origin_handoff`, session claimed_by/consumed_by
        # match) supply the SESSION'S ACTUAL HELD BATON -- exactly the
        # producer-note contract SKILL.md already documents ("derives
        # origin_handoff/origin_handoff_id by finding the baton that
        # session currently holds").
        origin_own_handoff_id = _fm_field(fm, "handoff_id")
        if origin_own_handoff_id:
            lineage["origin_handoff"] = artifact_path or None
            lineage["origin_handoff_id"] = origin_own_handoff_id
        else:
            lineage["origin_handoff"] = None
            lineage["origin_handoff_id"] = None
        lineage["origin_session"] = _fm_field(fm, "claimed_by") or _fm_field(fm, "consumed_by")
        lineage["origin_plan_id"] = _fm_field(fm, "plan_id")
        goal_raw = _fm_field(fm, "goal_id")
        lineage["origin_goal_id"] = [goal_raw] if goal_raw else None
        # R5 governing-plan stamp, spinoff kind: the progenitor's own governing_plan is
        # NOT carried onto a fork by default -- mirrors the 2026-08-05
        # deliverable_id PM ruling this branch's own docstring section
        # narrates (a fork mints fresh, it does not inherit). The one case
        # that DOES produce a value is `artifact_path` itself BEING the plan
        # document (the "stub -> plan -> mint" order's plan tier) -- i.e.
        # `fm` carries `plan_id` and is not itself a handoff record.
        if _fm_field(fm, "plan_id") and not origin_own_handoff_id:
            lineage["governing_plan"] = artifact_path or None
        else:
            lineage["governing_plan"] = None
    else:
        raise ValueError(f"baton_assemble: unrecognized kind {kind!r} (expected one of {KINDS})")

    # Additive-only, warn-never-block (AC4): this key is attached AFTER every
    # existing key above is finalized and changes none of them -- a
    # collision hit alters no directive, no exit code, and no scaffolded
    # output. See `_scan_deliverable_collision`'s own docstring for the
    # non-terminal predicate and the `state/handoffs/`-only scope.
    lineage["deliverable_collision"] = _scan_deliverable_collision(
        lineage["deliverable_id"],
        root / lineage["output_path"],
        root,
        (root / artifact_path) if artifact_path else None,
        lineage.get("additional_predecessors"),
    )
    if lineage["deliverable_collision"] is not None:
        _collision = lineage["deliverable_collision"]
        _address_suffix = _resolve_claimed_by_address_suffix(_collision["claimed_by"])
        print(
            "baton-assemble: deliverable_id "
            f"{lineage['deliverable_id']!r} already held by a live baton at "
            f"{_collision['path']!r} (claimed_by={_collision['claimed_by']!r}"
            f"{_address_suffix}) "
            "-- proceeding with this authoring anyway (warn-only).",
            file=sys.stderr,
        )

    return lineage


# ---------------------------------------------------------------------------
# directives[] -- every entry names an EXISTING atomic CLI/op; this module
# never reimplements one.
# ---------------------------------------------------------------------------


def _repo_relative_posix(value: str, root: Optional[Path]) -> str:
    """Render `value` as a repo-relative forward-slash path for a frontmatter
    path field.

    Absolute inputs are made relative to `root`; already-relative inputs are
    passed through with separators normalized. An absolute path that does not
    live under `root` is returned unchanged rather than rewritten via `..` --
    a cross-root path is a caller error this function has no mandate to
    silently paper over, and a `..`-prefixed frontmatter value would be worse
    than the absolute one it replaced.

    Windows: `PurePath.relative_to` handles the drive-letter case, and the
    explicit backslash fold means a `state\\handoffs\\x.md` input yields the
    same committed bytes as its POSIX counterpart -- frontmatter paths are
    compared as strings by `dag.py`'s `resolve_target`, so separator drift
    across platforms is a real DAG break, not cosmetic.
    """
    if not value:
        return value
    candidate = Path(value)
    if candidate.is_absolute() and root is not None:
        try:
            candidate = candidate.relative_to(root)
        except ValueError:
            return str(value).replace("\\", "/")
    return str(candidate).replace("\\", "/")


def _exists_under(rel_or_abs: str, root: Path) -> bool:
    """Is `rel_or_abs` (a repo-relative or absolute path) an existing FILE?
    Root-relative resolution in one place, rather than a `root / value` join
    per call site -- `lineage["output_path"]` is relative today but
    `lineage["predecessor"]` deliberately echoes whatever the caller passed,
    and a bare join silently mis-resolves an absolute value on Windows (a
    drive-letter path joined onto a root yields the absolute path back on
    POSIX but is a different failure mode to reason about per site)."""
    candidate = Path(rel_or_abs)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.is_file()


def _resolved_predecessor_canonical_kind(predecessor_path: Optional[str], root: Optional[Path]) -> str:
    """The resolved predecessor's own `kind` frontmatter field, canonicalized
    via the shared `coordinator_core.frontmatter.baton_class.canonical_kind`
    helper -- the SAME normalizer `reconcile/gate_eval.py`, `roadmap/audit.py`,
    `ops/fleet/archive_handoffs.py`, and `ops/session/boot_sweep.py` already
    use (plan's F2 finding: nothing in the d6 emission/apply path read this
    field at all before C3).

    Returns `""` (matching `canonical_kind`'s own empty-string convention for
    an absent value) when `predecessor_path` is falsy, `root` is unavailable,
    or the path does not resolve to an existing file -- callers gate on
    equality to `"roadmap-baton"`, and `""` never satisfies that comparison,
    so an unresolvable predecessor degrades to "not a baton" (mechanical
    fail-open on THIS narrow question only -- the d6 arming gate this
    function feeds is unaffected for every predecessor kind it already
    handled before C3)."""
    if not predecessor_path or root is None:
        return ""
    candidate = Path(predecessor_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        return ""

    from coordinator_core.frontmatter.baton_class import canonical_kind

    fm = _read_frontmatter(candidate)
    return canonical_kind(_fm_field(fm, "kind"))


def _resolved_predecessor_deployment_state(predecessor_path: Optional[str], root: Optional[Path]) -> str:
    """The resolved predecessor's own `deployment_state` frontmatter field,
    read raw (no canonicalizer -- `deployment_state` has no normalizer
    equivalent to `canonical_kind`). Same shape as
    `_resolved_predecessor_canonical_kind` immediately above, re-keyed on
    `deployment_state` instead of `kind`.

    Returns `""` (matching that sibling's own empty-string convention) when
    `predecessor_path` is falsy, `root` is unavailable, or the path does not
    resolve to an existing file -- callers gate on equality to `"closed"`,
    and `""` never satisfies that comparison, so an unresolvable predecessor
    degrades to "not closed" (mechanical fail-open on THIS narrow question
    only)."""
    if not predecessor_path or root is None:
        return ""
    candidate = Path(predecessor_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        return ""

    fm = _read_frontmatter(candidate)
    return _fm_field(fm, "deployment_state") or ""


def _resolved_predecessor_closed_reason(predecessor_path: Optional[str], root: Optional[Path]) -> str:
    """The resolved predecessor's own `closed_reason` frontmatter field,
    same fail-open-to-`""` resolution shape as
    `_resolved_predecessor_deployment_state` immediately above -- reused by
    both `_build_directives`'s per-predecessor closed-baton gate and
    `brief()`'s `d6-closed-predecessor-decline` judgment-point evidence, so
    the two never disagree on why a predecessor was skipped."""
    if not predecessor_path or root is None:
        return ""
    candidate = Path(predecessor_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        return ""

    fm = _read_frontmatter(candidate)
    return _fm_field(fm, "closed_reason") or ""


def _resolved_predecessor_sizing_object(
    predecessor_path: "Optional[str]", root: "Optional[Path]"
) -> str:
    """The resolved predecessor's own `sizing_object`, or `""` when it has none.

    Exists because `coordinator-doc-new --type=roadmap-baton` REFUSES without an
    explicit sizing answer -- either `--sizing-object <path>` or
    `--no-sizing-object`. DR-172's identity carry forwarded `--roadmap-id` and
    `--stub-id` and stopped there, so every roadmap-baton succession through
    `baton-assemble apply` died at d1 with "requires an explicit sizing answer"
    and no way to supply one: `apply` exposes no sizing flag, and the directive
    list is not hand-executable by contract. Observed 2026-08-27 on
    `hnd-the-gate-path-s-spawns-are-the-865bc9`.

    A continuation inherits its predecessor's sizing by construction -- it is
    the same sized work, which is what makes it a continuation rather than a
    fork. Fail-open to `""` in the same shape as the sibling
    `_resolved_predecessor_*` readers; the caller turns `""` into the explicit
    `--no-sizing-object`, so the answer is always stated and never guessed.
    """
    if not predecessor_path or root is None:
        return ""
    candidate = Path(predecessor_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        return ""
    value = (_fm_field(_read_frontmatter(candidate), "sizing_object") or "").strip()
    # `_fm_field` returns RAW frontmatter text, never a YAML-parsed value, so an
    # explicitly-null `sizing_object: null` arrives as the truthy string "null"
    # and was forwarded verbatim as `--sizing-object=null`. coordinator-doc-new
    # then resolves it as a path and refuses: "'null' does not resolve on disk
    # (looked for <root>/null)" -- which made EVERY roadmap-baton carrying an
    # explicit null sizing unable to hand off at all, the same class of dead end
    # this function's own docstring records being fixed for the missing-flag case.
    # An explicit null is the ABSENCE of a sizing object, so it takes the same
    # `--no-sizing-object` limb as a field that was never written.
    if value.lower() in {"null", "~", "none"}:
        return ""
    return value


def _resolved_predecessor_roadmap_identity(
    predecessor_path: Optional[str], root: Optional[Path]
) -> tuple[str, str, list[str]]:
    """The resolved predecessor's own `roadmap_id`/`stub_id` frontmatter
    fields, same fail-open-to-`""` resolution shape as the sibling
    `_resolved_predecessor_*` readers above -- feeds DR-172's mint-time
    identity carry (C10 Part 2): when the predecessor is `kind:
    roadmap-baton`, these two values are forwarded to `coordinator-doc-new`'s
    existing `--roadmap-id`/`--stub-id` flags so the successor mints as a
    roadmap-baton inheriting the same roadmap identity rather than a bare
    `handoff`.

    `blocks` IS now carried, as the third element -- the follow-on this
    docstring used to defer. Carrying `stub_id` without `blocks` was not a
    cosmetic gap: the successor inherits the identity the whole down-graph
    resolves against, then authors `blocks: []` against it, so every dependent's
    edge reads as severed the moment the successor becomes the chain head.
    `reconcile.gate_eval._has_asymmetry` then reports a symmetric graph as a data
    defect and `handoff.reconcile_open` refuses the baton. Nothing surfaces the
    loss at mint time, which is precisely why it cannot stay a placeholder for
    the author to fill: `sprint`/`wave` are visibly unset stubs, a dropped
    down-edge is invisible. `coordinator-doc-new` grew `--blocks` (repeatable,
    carry-through verbatim) for this.

    STILL not carried, named rather than silently dropped: `blocked_by`,
    `sprint`, `wave`. `blocked_by` is excluded ON PURPOSE, not for scope --
    it is derived readiness state (`--gated-open` owns it, `_derive_readiness`
    reads it), so inheriting a predecessor's gates would re-park a successor on
    blockers that may have cleared while the predecessor was in flight.
    `sprint`/`wave` are `bin/roadmap-number-stubs` topo outputs, not lineage.

    Fail-open to `[]` on the same terms as the two string fields: a
    missing/unreadable predecessor, or one with no `blocks`, yields the empty
    list and the scaffold's own `blocks: []` default, never a raise."""
    if not predecessor_path or root is None:
        return "", "", []
    candidate = Path(predecessor_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        return "", "", []

    fm = _read_frontmatter(candidate)
    return (
        _fm_field(fm, "roadmap_id") or "",
        _fm_field(fm, "stub_id") or "",
        _fm_string_list(fm, "blocks"),
    )


def _fm_string_list(fm: str, key: str) -> list[str]:
    """A frontmatter LIST field's string entries, or `[]`.

    The sibling `_fm_field` reader (`read_fm_field_unquoted`) is scalar-only: on a
    list it returns the raw `["a", "b"]` text for the flow spelling and nothing at
    all for the block spelling, so neither shape survives it. `blocks:` is authored
    both ways across the corpus, so this parses the frontmatter proper rather than
    pattern-matching one spelling and silently dropping the other -- a dropped
    `blocks` entry is exactly the class of silent edge loss the carry exists to stop.

    Fail-open to `[]` on unparseable frontmatter, matching every
    `_resolved_predecessor_*` reader's own contract: never a raise, never a partial
    list passed off as complete.
    """
    if not fm:
        return []
    import yaml

    try:
        parsed = yaml.safe_load(fm) or {}
    except Exception:  # noqa: BLE001 -- malformed frontmatter degrades to [], never raises
        return []
    if not isinstance(parsed, dict):
        return []
    value = parsed.get(key)
    if not isinstance(value, list):
        return []
    return [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]


def _build_closed_predecessor_decline_judgment_point(
    predecessor_path: str, closed_reason: str, d6_id: str, root: Path
) -> dict[str, Any]:
    """`d6-closed-predecessor-decline` -- surfaced by `brief()` in place of
    a `d6`/`d6-N` directive when the resolved predecessor at
    `predecessor_path` carries `deployment_state: closed`. A closed baton is
    terminal (docs/plans/2026-08-13-closed-baton-is-terminal-d6-declines-per-
    predecessor.md § Problem) -- superseding it would flip
    `deployment_state` to `continued` and strand its own `closed_reason`
    against the bidirectional schema rule
    (`frontmatter/schema_validate.py::_cf_closed_reason_required`), so d6
    declines to arm for this predecessor unless explicitly overridden. The
    kind-first roadmap-baton decline this point used to sit alongside is
    retired (DR-172, 2026-08-18, C10) -- this is now the only per-
    predecessor d6 decline in the module.

    Two legal decision values:
    `leave-closed` (default -- this predecessor's d6/d6-N stays unarmed,
    the mint proceeds -- resolves d1) and `force-supersede` (operator
    override -- resolves ONLY the `d6_id` this predecessor's own directive
    would have used, never every d6 in the fan-in)."""
    return build_untrusted_gate_judgment_point(
        id=f"{d6_id}-closed-predecessor-decline",
        question=(
            f"The resolved predecessor {predecessor_path!r} is closed "
            f"(closed_reason: {closed_reason!r}) -- supersede it (archive + "
            "stamp continued) as part of this mint, or leave the closure in "
            "place?"
        ),
        dispositions=[
            {"value": "leave-closed", "resolves": ["d1"]},
            {"value": "force-supersede", "resolves": [d6_id]},
        ],
        evidence=(
            f"deployment_state: closed, closed_reason: {closed_reason!r}. "
            "A closed baton is terminal -- superseding it would strand "
            "closed_reason against its own bidirectional schema rule."
        ),
        reason=(
            "A closed baton records a deliberate, documented closure "
            "(cancelled/displaced/stale) -- flipping it to continued as a "
            "side effect of an unrelated fan-in would overwrite that record "
            "with a succession edge saying something different. If the "
            "closure was wrong, reopen the predecessor first rather than "
            "overriding here."
        ),
    )


def _build_fan_in_cardinality_judgment_point(
    lineage: dict[str, Any], root: Path
) -> dict[str, Any]:
    """`j-fan-in-cardinality` -- surfaced by `brief()` whenever a session
    holds 2+ claimed handoffs and `resolve_lineage` resolves a fan-in
    (`lineage["additional_predecessors"]` non-empty). Per Resolution 1
    (`state/roadmap/sedge-2026-08-06/COORDINATOR-RESOLUTIONS.md`), N->1
    fan-in is the CORRECT shape and stays -- this point adds no new
    cardinality, it only narrates one that already exists: the primary
    predecessor's `deliverable_id` is the one that survives as the
    successor's OWN `deliverable_id` (via `resolve_lineage`'s own carry, see
    `resolve_deliverable_and_initiative`), while every additional
    predecessor's `deliverable_id`/`plan_id` is now carried too, onto the
    baton's `deliverable_ids`/`plan_ids` collections -- per the 2026-08-20 PM
    ruling recorded as a supersession annotation on
    `succession-edge-cardinality.md`'s Constraint #2 and on
    COORDINATOR-RESOLUTIONS.md Resolution 1 (both in
    `state/roadmap/sedge-2026-08-06/`), delivered by
    `docs/plans/2026-08-19-unified-baton-inherits-every-parents-material.md`.
    Cluster 7's ledger is not cancelled by that ruling -- if it ships, the
    baton's copy becomes a convenience rather than the sole authority.

    NARRATION-ONLY (non-negotiable, per the sedge-04 stub and the 2026-07-29
    Constraint #6 hazard `brief()`'s own docstring describes): this id is
    never wired into any directive's `depends_on`
    -- `_build_directives`'s d6/d6-N loop already arms unconditionally off
    `lineage["additional_predecessors"]` regardless of whether this point is
    resolved, so there is nothing here for a halt to gate. A future editor
    wiring `j-fan-in-cardinality` into a `depends_on` re-opens that hazard
    and must re-solve it, not assume this comment still covers it.

    Acknowledge-only by design (sedge-04's second open question, carried
    forward unresolved): the single `acknowledge` disposition below carries
    `resolves: []`, mirroring `j-continuation-vs-fork`'s own `resolves: []`
    precedent for a disposition with nothing to arm. Whether this point
    should offer an actionable disposition (e.g. nulling one
    `additional_predecessors` entry) is a direction-class call this stub
    does not make.
    """
    primary_path = lineage.get("predecessor")
    additional_paths = lineage.get("additional_predecessors") or []

    primary_deliverable_id = _deliverable_id_for(primary_path, root)
    entries = [
        f"primary predecessor {primary_path!r} (deliverable_id="
        f"{primary_deliverable_id!r}) -- SURVIVES onto the successor"
    ]
    for extra_path in additional_paths:
        extra_deliverable_id = _deliverable_id_for(extra_path, root)
        entries.append(
            f"additional predecessor {extra_path!r} (deliverable_id="
            f"{extra_deliverable_id!r}) -- carried onto the successor's "
            "deliverable_ids"
        )

    return build_untrusted_gate_judgment_point(
        id="j-fan-in-cardinality",
        question=(
            f"This session holds {1 + len(additional_paths)} claimed batons "
            "fanning into one successor -- per Resolution 1 this N->1 shape "
            "is correct and stays; acknowledge which predecessor's "
            "deliverable_id becomes the successor's own and which are "
            "carried alongside it."
        ),
        dispositions=[
            {"value": "acknowledge", "resolves": []},
        ],
        evidence="; ".join(entries),
        reason=(
            "Resolution 1 (state/roadmap/sedge-2026-08-06/COORDINATOR-"
            "RESOLUTIONS.md) keeps N->1 fan-in and retires fan-out -- this "
            "point makes the carry of batons 2..N's deliverable_id onto the "
            "successor's deliverable_ids legible to the operator rather than "
            "silent; it is narration only and never gates `apply` (see this "
            "function's own docstring for the 2026-07-29 hazard this must "
            "not reopen)."
        ),
    )


def _build_divergent_deliverable_id_judgment_point(
    plan_file: Optional[str],
    predecessor_file: Optional[str],
    additional_predecessor_files: list[str],
    root: Path,
    error: Exception,
) -> dict[str, Any]:
    """`j-divergent-deliverable-id` -- surfaced by `brief()` when the
    carry-or-mint cascade's rungs disagree and no disposition has named a
    winner yet. Replaces the terminal `DivergentDeliverableIdError` crash
    with the same judgment-point round trip every other genuine judgment
    call in this assembler takes: `brief` emits the candidates, the
    operator picks, `brief` is re-run with `--decisions`, and the existing
    `excise_rung` cut removes the losing rung BEFORE the cascade reaches
    the raise.

    Negative-spec, load-bearing (DR-207 DD#1 and
    `DivergentDeliverableIdError`'s own docstring): NOTHING here picks a
    winner. `build_untrusted_gate_judgment_point` structurally forbids a
    `recommendation`, no disposition is defaulted, and an unresolved point
    leaves `directives` empty so `apply` cannot proceed on a guess. The
    tiebreak is the earliest-artifact test, which is a fact about artifact
    history this engine cannot see -- exactly why it is asked rather than
    computed.

    Scoped to the CARRYING rungs (`plan_file`/`predecessor_file`) -- the
    only two that can supply the id. Fan-in legs never reach the divergence
    check at all (see `resolve_lineage`'s own comment): a leg cannot become
    the carried id, so its disagreement is a drop for `j-fan-in-cardinality`
    to narrate, never a question for this point to ask.

    Spec backlink: state/handoffs/2026-08-14-multi-plan-sessions-cannot-hand-off.md
    """

    plan_id = _deliverable_id_for(plan_file, root)
    predecessor_id = _deliverable_id_for(predecessor_file, root)

    entries = [
        f"claimed-plan rung {plan_file!r} names deliverable_id {plan_id!r}",
        f"predecessor rung {predecessor_file!r} names deliverable_id {predecessor_id!r}",
    ]
    # Fan-in rungs are listed as CONTEXT only -- they never reach the
    # divergence check (see `resolve_lineage`) and never carry, so there is
    # no per-leg verdict to compute here; naming their ids still helps the
    # operator run DR-207 DD#1's earliest-artifact test across the whole
    # picture rather than just the two carrying rungs.
    for extra_path in additional_predecessor_files:
        extra_id = _deliverable_id_for(extra_path, root)
        if extra_id:
            entries.append(
                f"fan-in rung {extra_path!r} names deliverable_id {extra_id!r} "
                "(context: never carried, not part of this divergence)"
            )

    dispositions = [
        {"value": "keep-plan", "resolves": []},
        {"value": "keep-predecessor", "resolves": []},
    ]
    question = (
        "The two rungs that can carry a deliverable_id disagree. Per DR-207 "
        "DD#1 the EARLIEST artifact's id wins -- apply that test to the "
        "artifact history and name the rung that survives. 'keep-plan' "
        "carries the claimed plan's id and cuts the predecessor rung; "
        "'keep-predecessor' carries the predecessor's id and cuts the "
        "claimed-plan rung. Re-run `brief` with --decisions "
        "'{\"j-divergent-deliverable-id\": {\"disposition\": <value>, "
        "\"decision_note\": <which artifact came first, and how you "
        "know>}}'. decision_note is REQUIRED and non-empty."
    )

    return build_untrusted_gate_judgment_point(
        id="j-divergent-deliverable-id",
        question=question,
        dispositions=dispositions,
        evidence="; ".join(entries) + f" || raised: {error}",
        reason=(
            "DR-207 DD#1 and DivergentDeliverableIdError's own docstring "
            "both refuse an auto-pick: preferring the plan is right on the "
            "ordinary plan->handoff edge and wrong when a roadmap stub "
            "predates a re-minting plan, and preferring the predecessor is "
            "wrong in the mirror case. This point removes the dead end "
            "without weakening that refusal -- the operator applies the "
            "earliest-artifact test, and no code path selects a winner."
        ),
    )


def _build_plan_no_ledger_claim_judgment_point(
    governing_plan: str, claim_error: str, root: Path
) -> dict[str, Any]:
    """PIN-3 `d6-plan-no-ledger-claim` -- surfaced by `brief()` when C4's
    plan-ness discriminator routes a `docs/plans/*.md` input through the
    session's durable handoff-claims ledger (`_resolve_held_handoff_for_
    session`) and that ledger holds ZERO claims for the current session.

    d6 exists to fix "a continuation baton's predecessor being left
    non-terminal forever" (module docstring) -- a silent "no claim -> no
    target -> d6 does not arm" would trade a rare false-positive
    supersession for a ROUTINE SILENT STRANDING, reachable via ordinary
    paths: a resumed session under a different session id, a handoff claim
    dropped by a separate `pickup_assemble.apply.drop()` flow, or a plan
    claimed by a prior session. This judgment point makes that absence
    VISIBLE rather than silently resolving `d1` unasked.

    REPLAY BEHAVIOUR (Review: coordinatorcode-reviewer-c2d43fc7 Finding 2 --
    corrected 2026-08-03; the plan's own C4 body and this docstring's prior
    revision both asserted a "d5 already released it on replay" mechanism
    that does not exist and was verified NOT to hold). d5 is
    `["release-artifact", "plan", <slug>]` -- it releases a `plan-claims`
    entry. `_resolve_held_handoff_for_session` raises its "ZERO handoff
    claims" error by filtering strictly on `class_ == "handoff-claims"`, an
    orthogonal claim class. Nothing in `apply.py` or `session/claims.py`'s
    documented claim-record lifecycle releases a `handoff-claims` entry
    except an explicit `pickup_assemble.apply.drop()`; d5's own
    `release_artifact` call is `class_="plan"` only. So a replay after THIS
    run's own d5 cannot be why the handoff-claims ledger read comes back
    empty. The genuine replay-shaped explanations are: this session resumed
    under a DIFFERENT `CLAUDE_SESSION_ID` than the one that originally
    claimed the plan's predecessor, or a separate `pickup_assemble.apply.
    drop()` flow released the handoff claim. `_resolve_held_handoff_for_
    session`'s own `ValueError` text (either "no current session id is
    resolvable" or "holds ZERO handoff claims") is surfaced verbatim in
    `evidence` so an operator is not left to re-derive which case applies.
    """
    return build_untrusted_gate_judgment_point(
        id="d6-plan-no-ledger-claim",
        question=(
            f"Plan input {governing_plan!r} resolved no held handoff claim in the "
            "durable claim ledger -- is this a legitimate REPLAY (this "
            "session resumed under a different session id, or the handoff "
            "claim was dropped by a separate flow), or was a claim NEVER "
            "recorded for this plan/session (a genuine stranding risk)? The "
            "only in-band action here is proceeding without a supersession "
            "target; supplying a predecessor path explicitly means "
            "re-invoking `brief()` with that path as `artifact_path`, "
            "outside this decision."
        ),
        dispositions=[
            {"value": "proceed-without-supersession", "resolves": ["d1"]},
        ],
        evidence=(
            f"{claim_error} This absence could be a REPLAY (this session "
            "resumed under a different session id than the one that "
            "claimed the plan's predecessor, or the handoff claim was "
            "dropped by a separate `pickup_assemble.apply.drop()` flow) or "
            "a claim that was NEVER recorded for this plan/session -- "
            "distinguish before assuming stranding."
        ),
        reason=(
            "d6 exists to close 'a continuation baton's predecessor left "
            "non-terminal forever' (baton_assemble module docstring) -- "
            "silently not arming d6 here would trade a rare false-positive "
            "supersession for a routine SILENT STRANDING (a resumed session "
            "under a different session id, a plan claimed by a prior "
            "session, or a handoff claim dropped by a separate flow). This "
            "judgment point surfaces that explicitly, rather than resolving "
            "it silently."
        ),
    )


def _build_directives(
    kind: str,
    lineage: dict[str, Any],
    title: Optional[str] = None,
    root: Optional[Path] = None,
    decisions: Optional[dict[str, Any]] = None,
    predecessor_canonical_kind: Optional[str] = None,
) -> list[dict[str, Any]]:
    # Review: coordinatorcode-reviewer-c2d43fc7 Finding 5 -- `predecessor_
    # canonical_kind`, when supplied, is `brief()`'s own already-computed
    # `_resolved_predecessor_canonical_kind(lineage["predecessor"], root)`
    # result, reused here instead of re-reading/re-parsing the predecessor's
    # frontmatter a second time. `None` (every direct/test caller that
    # constructs a `lineage` dict by hand with no `brief()` above it) falls
    # back to computing it locally, unchanged from before this fix.
    d1_args = [f"--type={kind}", f"--deliverable-id={lineage.get('deliverable_id') or ''}"]
    # DR-172 (2026-08-18, plan a-session-always-has-a-baton, chunk C10, Part
    # 2) -- succession identity carry: a `kind: roadmap-baton` predecessor's
    # successor is minted AS a roadmap-baton, not the bare `handoff` doc
    # type, so its `roadmap_id`/`stub_id` survive the succession
    # (`--type` overridden below, then `--roadmap-id`/`--stub-id` forwarded
    # via coordinator-doc-new's existing flags -- see
    # `_resolved_predecessor_roadmap_identity`'s own docstring for the
    # `blocks`/`blocked_by`/`sprint`/`wave` gap this does NOT close).
    if kind == "handoff":
        _mint_pred_kind = (
            predecessor_canonical_kind
            if predecessor_canonical_kind is not None
            else _resolved_predecessor_canonical_kind(lineage.get("predecessor"), root)
        )
        if _mint_pred_kind == "roadmap-baton":
            (
                _pred_roadmap_id,
                _pred_stub_id,
                _pred_blocks,
            ) = _resolved_predecessor_roadmap_identity(lineage.get("predecessor"), root)
            d1_args[0] = "--type=roadmap-baton"
            if _pred_roadmap_id:
                d1_args.append(f"--roadmap-id={_pred_roadmap_id}")
            if _pred_stub_id:
                d1_args.append(f"--stub-id={_pred_stub_id}")
            # The down-edges travel with the `stub_id` they are authored against.
            # Emitting `--stub-id` without these hands the successor the identity
            # the whole down-graph resolves on and an empty `blocks:` to answer it
            # with -- see `_resolved_predecessor_roadmap_identity`'s docstring for
            # why that reads downstream as a severed graph rather than as a stub
            # awaiting fill.
            for _blocked in _pred_blocks:
                d1_args.append(f"--blocks={_blocked}")
            # `--type=roadmap-baton` refuses without an explicit sizing answer.
            # A continuation inherits the predecessor's sizing -- same sized
            # work is what makes it a continuation. No sizing on the
            # predecessor is answered explicitly rather than left unstated.
            _pred_sizing = _resolved_predecessor_sizing_object(
                lineage.get("predecessor"), root
            )
            d1_args.append(
                f"--sizing-object={_pred_sizing}" if _pred_sizing else "--no-sizing-object"
            )
    # `--out` is `lineage["output_path"]` -- a FRESH path COMPUTED via
    # `_compute_fresh_output_path`, never `artifact_path` echoed verbatim.
    # `artifact_path` is the caller-supplied INPUT lineage source in both
    # kinds (the plan being handed off / predecessor handoff for
    # kind="handoff"; the origin handoff/stub/plan for kind="spinoff") --
    # echoing an EXISTING input straight into `--out` destroyed it the
    # moment d1 fired (`coordinator-doc-new`'s `--out` write is an
    # unconditional overwrite). See `_compute_fresh_output_path`'s own
    # docstring and bug backlog
    # `2026-07-27-baton-assemble-handoff-brief-computes-a-fe36a5dea88e.yaml`.
    # For the bare-slug mint convention (both kinds' documented calling
    # shape), `output_path` is idempotent with `artifact_path` -- no
    # behaviour change there.
    #
    # Without a computed/normalized `--out`, `coordinator-doc-new` derives
    # its own path from a placeholder title, so d2 would lint a file d1
    # never created. `--title` is optional and omitted entirely when absent,
    # leaving `coordinator-doc-new`'s default-placeholder title in force.
    d1_out = lineage.get("output_path") or _compute_fresh_output_path(
        lineage.get("artifact_path") or "", title=title
    )
    d1_args.append(f"--out={d1_out}")
    if title:
        d1_args.append(f"--title={title}")
    # The PULL side of the succession edge. d6 (below) stamps the PREDECESSOR
    # with `continued_into: <successor>`; these two flags stamp the SUCCESSOR
    # with the predecessor it continues. Both halves are required for a walkable
    # DAG, and until 2026-07-29 only the push half existed: `resolve_lineage`
    # computed `predecessor`/`predecessor_id` correctly and `_build_directives`
    # discarded them, so `coordinator-doc-new` fell through to its hardcoded
    # `predecessor: none` on EVERY continuation baton this engine minted. The
    # observable symptom was an EM hand-editing the field after the fact (or
    # guessing which id belonged in it) on a successor the engine had already
    # resolved the answer for.
    #
    # kind-gated to "handoff": the spinoff kinds are predecessor:none-by-design
    # (schema_validate.py Rule A3a-3 `_cf_spinoff_predecessor_none`) and carry
    # their ancestry on the separate origin_* axis instead, so threading a
    # predecessor into a spinoff scaffold would author a guaranteed validation
    # failure. Both flags are omitted entirely (never passed empty) when the
    # lineage value is None -- a fork scaffolds byte-identically to before this
    # block existed, and `coordinator-doc-new` refuses `--predecessor-id`
    # without `--predecessor`, so passing exactly one of the pair is not a
    # reachable state from here.
    #
    # The path is emitted REPO-RELATIVE with forward slashes, never as the
    # absolute path `lineage["predecessor"]` may legitimately hold. That
    # lineage value deliberately echoes whatever the caller passed (an
    # absolute path when `apply` was handed one -- pinned by
    # `resolve_lineage`'s own tests), but the `predecessor:` FRONTMATTER
    # field is contractually repo-relative: `schema_validate.py` Rule C2-1b
    # and `dag.py`'s `resolve_target` both resolve it against the repo root,
    # so an absolute value authors an unwalkable edge and a machine-specific
    # path into a committed artifact. Normalizing here (at the point the
    # value crosses into a scaffolded file) rather than in `resolve_lineage`
    # keeps that echo-the-caller contract intact for d6, which takes the
    # value in memory and never writes it into frontmatter.
    #
    # `--predecessor-id` is additionally gated on `_pred_id` matching
    # `handoff.schema.json`'s own `predecessor_id` pattern
    # (`^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$`) -- a
    # non-conforming value (legacy pre-id artifact, hand-authored id, etc.)
    # is omitted here for the SAME normalize-at-the-crossing-point reason
    # the repo-relative path normalization above lives here rather than in
    # `resolve_lineage`: `--predecessor` still carries the lineage edge, so
    # nothing is lost, while a malformed id would author a guaranteed
    # `predecessor_id` schema-validation failure on the freshly scaffolded
    # successor.
    _predecessor_id_pattern = re.compile(r"^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$")
    _pred = None
    if kind == "handoff":
        _pred = lineage.get("predecessor")
        _pred_id = lineage.get("predecessor_id")
        if _pred:
            _pred_rel = _repo_relative_posix(_pred, root)
            d1_args.append(f"--predecessor={_pred_rel}")
            if _pred_id and _predecessor_id_pattern.match(_pred_id):
                d1_args.append(f"--predecessor-id={_pred_id}")

        # Successor-side fan-in down-edge (sedge-02). `d6*` already stamps the
        # UP-edge on every additional predecessor (`continued_into` -> this
        # successor); without this the successor carries only its primary
        # parent, so legs 2..N become unreachable from the successor the moment
        # they are archived -- no ancestry walk starting here can find them.
        #
        # Written UNCONDITIONALLY, not behind a judgment point. The values are
        # the same `lineage["additional_predecessors"]` list `d6*` already acts
        # on unconditionally, so gating only the down-edge would reintroduce the
        # very N-up/zero-down asymmetry this edge exists to close -- and an
        # unresolved gate in an autonomous run silently writes nothing, which is
        # the pre-existing bug wearing a gate. The heuristic's documented
        # `claimed_at`-tie arbitrariness affects which leg is PRIMARY, an
        # ordering question owned by sedge-14; the fan-in SET this writes is
        # order-independent.
        #
        # Every entry goes through `_repo_relative_posix` -- load-bearing, not
        # cosmetic. `schema_validate._cf_additional_predecessors_integrity`
        # compares by EXACT STRING, so an absolute entry would slip past the
        # duplicate-of-primary check that the normalized `_pred_rel` above is
        # already subject to.
        for _extra in lineage.get("additional_predecessors") or []:
            d1_args.append(f"--additional-predecessor={_repo_relative_posix(_extra, root)}")

    # `deliverable_ids` / `plan_ids` (C2): passed once per id, only when the
    # corresponding `resolve_lineage`-computed key is non-None -- both keys
    # already carry that module's optional-array convention (`None`, never
    # `[]`, below 2 distinct ids), so a bare truthiness check here is the
    # same test the key's own producer already applied.
    for _deliverable_id in lineage.get("deliverable_ids") or []:
        d1_args.append(f"--deliverable-ids={_deliverable_id}")
    for _plan_id in lineage.get("plan_ids") or []:
        d1_args.append(f"--plan-ids={_plan_id}")
    for _carried_item in lineage.get("carried_items") or []:
        d1_args.append(f"--carried-items={json.dumps(_carried_item, sort_keys=True)}")

    # d7 -- precondition gate, not a post-step: composes
    # `coordinator_core.ops.handoff_carry_gate.evaluate_gate` (in-process, via
    # `_dispatch_handoff_carry_gate` in apply.py) over the PREDECESSOR's own
    # `carried_items` frontmatter array, before d1 scaffolds the successor.
    # Kind-gated identically to the `--predecessor`/`--predecessor-id` block
    # immediately above, and for the same reason: the spinoff kinds are
    # `predecessor: none` by design (schema_validate.py Rule A3a-3), so there
    # is no predecessor whose carried items could be gated. Omitted entirely
    # (never emitted with an empty arg) when there is no predecessor -- a
    # standalone handoff with no predecessor has nothing for this gate to
    # check. Never `already_satisfied`: it is a read-only check with no
    # converged state to detect.
    d7_directive: Optional[dict[str, Any]] = None
    if kind == "handoff" and _pred:
        d7_directive = {
            "id": "d7",
            "cli": "handoff-carry-gate",
            "args": [_pred],
            "depends_on": None,
            "already_satisfied": False,
        }
    # d1 is `already_satisfied` iff its own `--out` target is already a file on
    # disk. Two distinct jobs, one predicate:
    #   - RESUME. On a replay (`lineage["resumed_successor"]`, see
    #     `_resume_recorded_successor_path`) `--out` is the successor a prior
    #     attempt already scaffolded, so re-firing d1 would author it a second
    #     time over the operator's own edits.
    #   - SAFETY. `coordinator-doc-new`'s `--out` write is an UNCONDITIONAL
    #     overwrite with no existence check (see `_compute_fresh_output_path`'s
    #     docstring) and `_assert_no_directive_writes_over_input` only guards
    #     the caller-supplied INPUT path, never d1's own computed output -- so
    #     existence is the honest predicate for "firing this would destroy
    #     something", not merely "this landed already".
    # A CLEAN run is unaffected by construction: `_compute_fresh_output_path`
    # walks its collision ladder until it finds a path that does NOT exist, so
    # this is always False unless the path was resumed from the predecessor.
    d1_already_satisfied = bool(root is not None and d1_out and _exists_under(d1_out, root))
    d1_directive: dict[str, Any] = {
        "id": "d1",
        "cli": "coordinator-doc-new",
        "args": d1_args,
        "depends_on": None,
        "already_satisfied": d1_already_satisfied,
    }
    if d1_already_satisfied:
        # Emitted ONLY when the flag is True, so a clean run's directive dicts
        # stay byte-identical to their pre-replay shape. `apply()` surfaces this
        # in `report["replayed"]` and on stderr -- a skipped directive must not
        # read as a silent green.
        d1_directive["already_satisfied_reason"] = (
            f"{d1_out} already exists on disk -- a prior attempt of this same run "
            "scaffolded it, and `coordinator-doc-new --out` is an unconditional "
            "overwrite, so re-authoring it would destroy that content"
        )
        # Replay hazard, named rather than papered over (sedge-02). The fan-in
        # down-edge is written by d1 and ONLY by d1 -- `apply_base
        # .execute_directives` has no rollback and the `d6*` predecessor
        # mutations are deliberately emitted last, so a converging post-`d6`
        # stamp is out of scope by construction. The consequence is real: a run
        # resumed onto a successor a PRIOR attempt scaffolded without these legs
        # keeps whatever that attempt wrote, and this run's resolved list is not
        # applied. That is recoverable by hand and is not silent -- `apply()`
        # surfaces this string in `report["replayed"]` and on stderr -- but it
        # is only non-silent if the string SAYS SO, which is what this branch is
        # for. Naming the exact legs makes the manual fix a copy-paste.
        # Review: coordinator:code-reviewer (0d090196) -- renamed from
        # `_replay_extras`/`_replay_rel` to match this function's other
        # additional-predecessor locals (`_extra`, `_extra_path`,
        # `_ledger_extra_paths`) rather than a one-off pairing.
        _extra_paths = lineage.get("additional_predecessors") or []
        if _extra_paths:
            _extra_rels = [_repo_relative_posix(_e, root) for _e in _extra_paths]
            d1_directive["already_satisfied_reason"] += (
                "; NOTE this run resolved "
                f"{len(_extra_rels)} additional predecessor(s) -- "
                + ", ".join(_extra_rels)
                + " -- whose successor-side `additional_predecessors:` down-edge d1 "
                "would have written. Skipping d1 skips that write. Verify the field "
                "on the resumed successor and add any missing leg by hand; the `d6*` "
                "up-edges are separate directives and still fire"
            )
    # d1b -- C3, "the replay path carries the union too": the replay-only
    # fallback for the drop the d1_already_satisfied `already_satisfied_reason`
    # NOTE above names -- see that NOTE for the exact hazard this closes.
    # Emitted ONLY when d1 itself was skipped AND there is
    # a union to carry (either key non-None) -- a clean run's directive list
    # stays byte-identical to its pre-C3 shape, and a replay with nothing to
    # carry (single predecessor, both keys None) emits no extra directive
    # either. Placed between d1 and d2 (never after d6): d2 lints `d1_out`
    # and fires on the replay path too, so a stamp landing after it ships
    # unvalidated bytes; `execute_directives` has no rollback and the `d6*`
    # predecessor mutations are emitted last by design, so placing this
    # before them keeps a mid-run failure here pre-d6 -- the already-covered
    # orphan case -- rather than stranding a predecessor archived-as-continued
    # into a successor missing its ids.
    d1b_directive: Optional[dict[str, Any]] = None
    if d1_already_satisfied and (
        lineage.get("deliverable_ids") or lineage.get("plan_ids") or lineage.get("carried_items")
    ):
        _d1b_args = ["--file", d1_out]
        for _deliverable_id in lineage.get("deliverable_ids") or []:
            _d1b_args.append(f"--deliverable-ids={_deliverable_id}")
        for _plan_id in lineage.get("plan_ids") or []:
            _d1b_args.append(f"--plan-ids={_plan_id}")
        for _carried_item in lineage.get("carried_items") or []:
            _d1b_args.append(f"--carried-items={json.dumps(_carried_item, sort_keys=True)}")
        d1b_directive = {
            "id": "d1b",
            "cli": "baton-stamp-carried-ids",
            "args": _d1b_args,
            "depends_on": ["d1"],
            # Never `already_satisfied`: the dispatch itself is idempotent
            # (a read-back-equal union short-circuits with no write, per
            # `_dispatch_baton_stamp_carried_ids`'s own docstring), so gating
            # here would fork that convergence check into a second place.
            "already_satisfied": False,
        }

    # d1c -- R5, governing-plan stamp at mint (2026-08-21, rebuild-the-three-
    # ceremony-assemblers plan C5). Fires whenever `resolve_lineage` resolved
    # a non-None `governing_plan` -- on BOTH a clean mint (`governing_plan` is never
    # passed to `coordinator-doc-new`'s own flags -- `coordinator-doc-new.py`
    # is out of this chunk's `writes:` scope -- so the frontmatter-direct
    # writer d1b already uses is the only writer for it too) and a replay
    # (the same writer's own idempotent read-back-equal short circuit
    # converges it). Never gated on `d1_already_satisfied` the way d1b is:
    # d1b is a replay-ONLY fallback for a union d1's own flags would
    # otherwise have carried, but `governing_plan` is NEVER in d1's flags, so this
    # is the sole write path for it on every mint, clean or replayed.
    d1c_directive: Optional[dict[str, Any]] = None
    _governing_plan_stamp = lineage.get("governing_plan")
    if _governing_plan_stamp:
        d1c_directive = {
            "id": "d1c",
            "cli": "baton-stamp-carried-ids",
            "args": [
                "--file",
                d1_out,
                f"--governing-plan={_repo_relative_posix(_governing_plan_stamp, root)}",
            ],
            "depends_on": ["d1"],
            # Never `already_satisfied`: same idempotent-writer contract as
            # d1b -- a read-back-equal value short-circuits with no write.
            "already_satisfied": False,
        }

    directives: list[dict[str, Any]] = []
    if d7_directive is not None:
        directives.append(d7_directive)
    directives.append(d1_directive)
    if d1b_directive is not None:
        directives.append(d1b_directive)
    if d1c_directive is not None:
        directives.append(d1c_directive)
    _d2_depends_on = ["d1"]
    if d1b_directive is not None:
        _d2_depends_on.append("d1b")
    if d1c_directive is not None:
        _d2_depends_on.append("d1c")
    directives.append(
        {
            "id": "d2",
            "cli": "lint-frontmatter",
            # `lint-frontmatter.py`'s CLI trampoline (coordinator_core/frontmatter/
            # schema_validate.py's `_parse_argv`) accepts only --root/--file/
            # --json/--strict-refs -- a bare positional path is rejected with
            # "unknown argument" (rc=2). `--file <path>` is the required shape.
            #
            # Target is `d1_out` -- the artifact d1 authors -- NOT
            # `artifact_path`, the INPUT (plan / predecessor baton) `apply` was
            # handed. d2's `depends_on: ["d1"]` only makes sense against d1's
            # output; linting the input meant this leg validated a file the run
            # never wrote, and any pre-existing defect in that input (e.g. an
            # unmigrated legacy `kind:` on a predecessor baton) failed d2 and
            # rolled the whole assembly back over a record this run did not
            # author. No runtime threading is needed to reach it: d1's `--out`
            # is COMPUTED here, statically, at brief() time -- the same string
            # is handed to d1 and to d2, so `apply_base.execute_directives`'s
            # resolve-args-before-dispatch contract is untouched. (The earlier
            # "threading is out of scope" caveat mis-stated the problem: the
            # value was already in scope.)
            "args": ["--file", d1_out],
            # `d1b` (C3) and `d1c` (R5) each land between d1 and d2 when
            # emitted, so d2 must not lint before either converges --
            # omitting one from `depends_on` would let d2 validate `d1_out`
            # before that stamp writes, racing the very keys it exists to
            # add.
            "depends_on": _d2_depends_on,
            # Never `already_satisfied`: a lint MUTATES NOTHING, so it leaves no
            # residue a replay could skip, and re-running it on a resumed
            # artifact is the whole point (the scaffold may have been edited
            # between attempts).
            "already_satisfied": False,
        },
    )

    if kind == "handoff":
        # No d3 here (see module docstring, "kind=handoff's d3 slot
        # deliberately does NOT name ... handoff.stamp_phase") -- d1's
        # scaffold already stamps handoff_phase:continuation unconditionally,
        # so a dedicated stamp directive in this brief would always be a
        # no-op.
        #
        # d4 (render-project-tracker) was retired 2026-08-14 alongside the
        # renderer itself. Nothing replaces it: `docs/project-tracker.md` is a
        # retired rendered index, and per RENDERED-INDEX-NO-HAND-MAINTENANCE no
        # directive or judgment point may ask an EM to maintain it by hand --
        # the substrate is `state/workstreams/`, queried directly.
        # session-claim-cli is a multi-subcommand CLI (`<subcommand>
        # <args...>`, see coordinator_core.session.claims's
        # `release_artifact` docstring) -- the first arg MUST be the
        # subcommand name, and `release-artifact` takes `<class> <basename>`,
        # `basename` a bare SLUG, never a path. `lineage["artifact_path"]` is
        # a full path (e.g. ".../docs/plans/2026-07-26-some-plan.md");
        # `.stem` strips the directory and the `.md` suffix down to the slug
        # the CLI expects.
        #
        # Authoring a HANDOFF is a RELINQUISHMENT of the plan claim -- the
        # successor session that picks up the baton and runs /execute-plan
        # must find the plan unclaimed, so this directive fires on the
        # handoff path. `release_artifact` is holder-identity-checked and
        # no-ops to success when this session is not the current holder.
        #
        # It deliberately does NOT fire for a SPINOFF (kind=="spinoff" never
        # reaches this branch) -- a fork is neither a claim nor a release: the
        # session authoring the spinoff keeps executing its own plan, and
        # releasing that claim mid-flight would drop the live execution lock,
        # letting a concurrent session claim the same plan out from under it.
        # Same reasoning as d6's `Deliberately does NOT fire for a fork`
        # comment above -- a fork's origin must not be disposed of.
        #
        # 2026-08-04 break-class fix: a STANDALONE handoff (no predecessor,
        # no plan -- `lineage["artifact_path"]` empty, see `brief`'s
        # `standalone_no_predecessor_reason` handling) has no plan claim to
        # relinquish in the first place. Emitting d5 anyway sent
        # `release-artifact plan ""` (`Path("").stem == ""`) at
        # `session-claim-cli`, which correctly rejects the empty basename
        # ("basename required", rc=1) and aborted the whole mint over a
        # directive whose own premise ("authoring a handoff relinquishes
        # THE plan claim") does not hold when there is no plan. Gating the
        # append on a non-empty resolved slug -- rather than weakening
        # `session-claim-cli`'s validation or passing a placeholder
        # basename -- makes d5 simply absent for the standalone case; it is
        # unchanged for every case where `artifact_path` resolves (the
        # plan->execute trigger, and the continuation-from-handoff case
        # that already reached this branch before this fix).
        plan_release_slug = Path(lineage.get("artifact_path") or "").stem
        if plan_release_slug:
            directives.append(
                {
                    "id": "d5",
                    "cli": "session-claim-cli",
                    "args": [
                        "release-artifact",
                        "plan",
                        plan_release_slug,
                    ],
                    "depends_on": ["d1"],
                    # Never `already_satisfied`: `release_artifact` is already
                    # holder-identity-checked and no-ops TO SUCCESS when this session
                    # is not the current holder (see the paragraph above), so a
                    # replay converges through the CLI's own check. Reading the claim
                    # ledger here to pre-answer "is it already released?" would fork
                    # that predicate into a second place.
                    "already_satisfied": False,
                }
            )

        # d6 -- push-side succession write (2026-07-27, computed-skills-b4
        # plan C1). MECHANICAL discriminator only: a continuation baton
        # (kind=="handoff") that actually names a predecessor. Fired via
        # `_CLI_DISPATCH["handoff.supersede_predecessor"]` (apply.py), which
        # composes the existing op `handoff.archive_transition`
        # mode="supersede" -- it stamps the PREDECESSOR
        # status:claimed + deployment_state:continued +
        # continued_into:<this successor> and archives it, in the SAME
        # transaction as this successor's own mint. `exclude` MUST name this
        # successor's own (already-normalized) path, or the live-children
        # guard sees the successor itself (which names the predecessor via
        # its own `predecessor:` field) as a live child and silently
        # retains -- see apply.py's handler docstring for the full contract
        # and the FAIL POSTURE (superseded=False aborts the whole mint).
        #
        # Deliberately does NOT fire for a fork (kind=="spinoff" never
        # reaches this branch) -- a fork's origin must NOT be disposed of;
        # see `handoff.author_fork`'s own `predecessor: none` stamp at
        # ops/handoff_author_fork.py:~212. Also deliberately does NOT gate
        # on `j-continuation-vs-fork` (the judgment point below) -- that
        # point is `kind`-scoped residue for a DIFFERENT question ("is THIS
        # baton a continuation or should it fork") that SKILL-prose already
        # resolves before ever calling `brief()` with kind="handoff"; once
        # kind is "handoff" and a predecessor is named, the succession write
        # is mechanically determined, not a second judgment call.
        # GAP CLOSED (2026-07-28, d6-archived-predecessor fix): d6 composes
        # `handoff.archive_transition` mode="supersede", whose `handoff_path`
        # containment now ALSO admits an already-archived path (in addition
        # to `state/handoffs/`) when mode="supersede" -- see that module's
        # docstring § Archived-predecessor stamp-in-place. When
        # `resolve_lineage` resolved this predecessor via the archive search
        # (`_resolve_qualified_path_or_raise`), `lineage["predecessor"]` (and
        # therefore `args[0]` below) may legitimately name a path already
        # under `archive/handoffs/`; d6 is emitted exactly as it is for a
        # live predecessor, and `handoff.archive_transition` now stamps the
        # status flip (status:claimed + deployment_state:continued +
        # continued_into:<successor>) IN PLACE and returns -- no git-mv is
        # attempted (there is nothing left to move) and no live-children
        # guard question is asked (the guard exists to gate a move, not a
        # stamp). `lineage["predecessor_is_live"]` remains informational
        # metadata this branch does not gate on -- the archived-vs-live split
        # is now handled entirely inside `handoff.archive_transition` itself,
        # not by this caller.
        #
        # ORDERING (2026-07-29, break-class fix): d6 is emitted LAST in this
        # kind's directive list -- deliberately AFTER d4 and d5, not in its
        # original d1/d2/d4/d6/d5 slot -- because it is the ONLY directive
        # here that mutates a DIFFERENT, pre-existing artifact (the
        # predecessor). `apply_base.execute_directives` has no rollback: a
        # raised handler exception mid-run returns
        # APPLY_EXIT_PARTIAL_MUTATION with whatever already landed
        # (apply_base.py's own execute_directives contract). With d6 in its
        # old position, a later directive's failure (observed live: d5) left
        # the predecessor already stamped deployment_state:continued +
        # continued_into:<successor> pointing at a successor a subsequent
        # failure could leave never fully populated -- a corrupted
        # succession graph, not a partial mint. `order_by_depends_on`
        # (apply_base.py) is a STABLE topological sort that breaks ties on
        # the directives' ORIGINAL LIST ORDER, so this list's emission order
        # is what fixes execution order here. Placing d6 last means: (a) no
        # directive after it can ever strand the predecessor, because none
        # exists, and (b) any EARLIER directive's failure leaves the
        # predecessor completely untouched, making the whole run cleanly
        # re-runnable from scratch. This is directive-ordering discipline,
        # not a transaction manager -- do NOT move d6 earlier again without
        # re-solving the no-rollback hazard this ordering exists to avoid.
        if lineage.get("predecessor") is not None:
            # `successor_path` MUST be `lineage["output_path"]` -- the FRESH
            # path d1 actually scaffolds this successor at (see
            # `_compute_fresh_output_path`'s own docstring) -- never
            # `lineage["artifact_path"]`. `artifact_path` is the caller-
            # supplied INPUT (the predecessor handoff this session opened
            # with, or the plan being handed off), per the module docstring's
            # `d1's --out` entry; threading it here stamps the PREDECESSOR's
            # `continued_into` with the INPUT path (wrong -- for the
            # plan->execute trigger, a PLAN file) and passes that same wrong
            # value as `exclude`, which is silent-corruption-shaped: it
            # passes schema validation (`continued_into` is just a string)
            # and would rot lineage on every future handoff. 2026-07-27
            # follow-up fix -- this was explicitly out of scope for the d1
            # `--out` fix above ("d6's own (out-of-scope, concurrent-
            # investigation) `successor_path` argument"); that investigation
            # is complete and did not cover it, so this closes it.
            successor_path = lineage.get("output_path") or ""
            # N-predecessor fan-in (2026-07-29): one d6 PER predecessor --
            # primary first (id "d6", UNCHANGED from before this fix, so a
            # single-predecessor brief stays byte-identical), then one
            # additional d6 per `lineage["additional_predecessors"]` entry
            # (ids "d6-2", "d6-3", ... -- never colliding with "d1".."d5" or
            # each other; `order_by_depends_on`/`jp_by_id` key directives by
            # `id`, so uniqueness is load-bearing, not cosmetic). Each keeps
            # the EXISTING 3-arg shape (`predecessor_path, continued_into,
            # exclude_path`) `_dispatch_handoff_supersede_predecessor`
            # (apply.py) already unpacks -- only `predecessor_path` varies
            # per directive. Emitting N calls to the existing per-predecessor
            # handler is deliberately preferred over teaching that handler a
            # list-valued arg: the handler already does the right thing for
            # ONE predecessor, and widening its arity would drag in apply.py
            # (out of scope -- see this fix's own dispatch brief).
            all_predecessors = [lineage["predecessor"]] + list(
                lineage.get("additional_predecessors") or []
            )
            for _i, _pred_path in enumerate(all_predecessors):
                _d6_id = "d6" if _i == 0 else f"d6-{_i + 1}"
                # DR-172 (2026-08-18, plan a-session-always-has-a-baton,
                # chunk C10) RETIRES the roadmap-baton kind-first decline
                # that used to sit here (C3, 2026-08-02, PIN-3): DR-172
                # found the refusal's own rationale does not hold --
                # `reconcile/gate_eval.py` resolves `blocked_by` by
                # `stub_id`, never by file path, so an archival move cannot
                # sever the edge the decline existed to protect. A
                # roadmap-baton predecessor now supersedes exactly like any
                # other -- see `ops/handoff_archive_transition.py`'s module
                # docstring for the mirror-image removal at that seam.
                # 2026-08-13 (closed-baton-is-terminal plan, C1): a
                # closed predecessor is terminal and is EXEMPT from
                # supersede -- superseding it would flip its
                # deployment_state to continued and strand its own
                # closed_reason against the bidirectional schema rule
                # (_cf_closed_reason_required). Checked PER-PREDECESSOR,
                # INSIDE this loop. `continue` skips THIS predecessor's
                # directive only -- every other iteration still appends, so
                # the mint completes.
                _pred_deployment_state = _resolved_predecessor_deployment_state(_pred_path, root)
                if _pred_deployment_state == "closed":
                    _decline_decision = (decisions or {}).get(f"{_d6_id}-closed-predecessor-decline")
                    _force_supersede_closed = (
                        isinstance(_decline_decision, dict)
                        and _decline_decision.get("disposition") == "force-supersede"
                    )
                    if not _force_supersede_closed:
                        continue  # decline to arm -- brief() emits the judgment point instead
                directives.append(
                    {
                        "id": _d6_id,
                        "cli": "handoff.supersede_predecessor",
                        "args": [_pred_path, successor_path, successor_path],
                        "depends_on": ["d1"],
                        # Never `already_satisfied` -- and that is the fix, not a
                        # gap. `_supersede_continued`
                        # (ops/handoff_archive_transition.py) already owns the
                        # predicate: status:claimed + deployment_state:continued +
                        # continued_into == this successor is its OWN byte-identical
                        # no-op branch, returning exit_code:0 so this handler reports
                        # superseded:True and never raises. What used to wedge a
                        # replay was `successor_path` being a DIFFERENT (freshly
                        # disambiguated) path on every attempt, which that function
                        # correctly refuses as a conflicting succession edge; pinning
                        # `output_path` to the recorded successor
                        # (`_resume_recorded_successor_path`) makes the values match
                        # and the existing branch do the converging. Deriving the
                        # same conjunct here would be a second definition of it, and
                        # would additionally put the DR-242 gate inside this
                        # handler's own body behind a skip.
                        "already_satisfied": False,
                    }
                )
    else:  # spinoff
        # d3 -- STAMPS the five origin_* provenance fields onto d1's
        # already-minted artifact (Option A, ratified 2026-07-27); it no
        # longer authors a second file. `args[5]` is d1's OWN `--out` target
        # (`d1_out`, computed above -- the SAME fresh path d1 just scaffolded
        # this run), never `lineage["artifact_path"]` (the INPUT origin this
        # spinoff forks FROM) -- see
        # `coordinator_core.baton_assemble.apply._dispatch_handoff_author_fork`
        # for the receiving contract. `origin_goal_id` is a list (or None) in
        # `lineage`; joined with ";" here because directive `args` is a flat
        # `list[str]` with no room for a nested JSON value -- the dispatcher
        # splits it back apart.
        directives.append(
            {
                "id": "d3",
                "cli": "handoff.author_fork",
                "args": [
                    lineage.get("origin_handoff") or "",
                    lineage.get("origin_handoff_id") or "",
                    lineage.get("origin_session") or "",
                    lineage.get("origin_plan_id") or "",
                    ";".join(lineage.get("origin_goal_id") or []),
                    d1_out,
                ],
                "depends_on": ["d1"],
                # Never `already_satisfied`, and kind=spinoff never replays at
                # all: replay resumption is driven off the PREDECESSOR's recorded
                # `continued_into` (`_resume_recorded_successor_path`) and a fork
                # has no predecessor by design, so `output_path` here is always a
                # genuinely fresh path `_compute_fresh_output_path` has already
                # proven does not exist. Re-stamping the same origin_* values
                # onto the same artifact is `handoff.author_fork`'s own no-op
                # regardless.
                "already_satisfied": False,
            }
        )

    return directives


# ---------------------------------------------------------------------------
# judgment_points[] -- residue never auto-fired. Every entry built via
# `build_untrusted_gate_judgment_point` -- no `recommendation` parameter
# exists on that constructor, so it is structurally impossible to attach a
# verdict here.
# ---------------------------------------------------------------------------


def _build_judgment_points(
    kind: str,
    dirty_tree_attribution: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    points = [
        build_untrusted_gate_judgment_point(
            id="j-self-honesty",
            question="Step 0 self-honesty gate: is the branch/artifact state actually what it claims?",
            dispositions=[
                build_disposition(
                    "proceed",
                    ["d1"],
                    guidance=(
                        "Check this baton against the trigger gate before authoring anything: "
                        "auto-compaction imminent, an unavoidable restart, a hard blocker acting "
                        "right now, a literal PM `/handoff` invocation, the review-owed close "
                        "trampoline, or the plan→execute stamp (`execution_authorized_at`). "
                        "\"Feels like a good pause point\" is the trap the gate exists to catch, "
                        "not a trigger -- if none of the gate conditions actually hold, stop and "
                        "take the next action in this session instead of proceeding here."
                    ),
                )
            ],
            evidence="Left to the caller SKILL.md's own Step 0 prose (C4/C5).",
            reason="Judgment residue -- not mechanically decidable from disk state alone.",
        ),
        build_untrusted_gate_judgment_point(
            id="j-pm-auth",
            question="Has the PM authorized this baton's dispatch/continuation?",
            dispositions=[
                build_disposition(
                    "authorized",
                    ["d5"],
                    guidance=(
                        "Authorization is an act, never a stamp to go looking for. It is a "
                        "literal invocation -- the PM typing `/handoff` (or the skill name) "
                        "for this workstream by name, not an intent-shaped remark like "
                        "\"you can hand that off\" -- or, on an execution baton, the handover "
                        "itself: being handed the baton via `/pickup` IS the PM's "
                        "authorization for the plan it carries, as invoking `/execute-plan` "
                        "is for that plan. Do not treat `execution_authorized_at` as a "
                        "precondition -- it is minted AT execute time, so its absence on a "
                        "not-yet-executed plan is by design and is not a gap. Absent an "
                        "authorizing act, this disposition does not apply -- surface the gap "
                        "rather than assuming consent from context."
                    ),
                )
            ],
            evidence="PM authorization is a conversational fact, not a disk artifact.",
            reason="Judgment residue -- the EM/PM dialogue, never mechanically inferred.",
        ),
    ]
    if kind == "spinoff":
        # R6: the minter cannot know which plan or sizing a spinoff belongs
        # to -- that is genuine EM/PM knowledge, never mechanically
        # inferred and never offered as a scanned candidate list (a
        # candidate list is a corpus walk wearing a helpful hat, which is
        # what this plan exists to remove). Distinct from `origin_plan_id`
        # above: that field is the ALREADY-resolved parent-provenance
        # rung read off `artifact_path`'s own frontmatter; this judgment
        # point is a forward-looking association the EM supplies for the
        # freshly-minted spinoff itself. Narrow by ruling (F10 rejected):
        # scoped to spinoff mint only, never widened onto the
        # continuation/execution-handoff path, which carries its
        # governing_plan/sizing forward deterministically instead (C5's
        # carry leg).
        #
        # "none" is a first-class disposition, not an omission -- naming
        # it here lets an EM answer "this spinoff carries no plan or
        # sizing" as a cheap, TRUE recorded absence rather than leaving
        # the question open (which would read as unknown, not answered).
        points.append(
            build_untrusted_gate_judgment_point(
                id="j-spinoff-plan-sizing",
                question=(
                    "Does this spinoff belong to an existing plan and/or "
                    "sizing object the EM already knows about?"
                ),
                dispositions=[
                    build_disposition(
                        "associate",
                        [],
                        guidance=(
                            "Name the plan_id and/or sizing slug this spinoff "
                            "belongs to via decision_note -- this is genuine "
                            "EM/PM knowledge, never inferred from disk state "
                            "and never offered as a scanned candidate list."
                        ),
                    ),
                    build_disposition(
                        "none",
                        [],
                        guidance=(
                            "No plan or sizing is associated with this "
                            "spinoff. A true, recorded absence -- cheap to "
                            "answer, and distinct from leaving the question "
                            "open."
                        ),
                    ),
                ],
                evidence=(
                    "Which plan/sizing this spinoff belongs to is genuine "
                    "EM/PM knowledge -- the minter cannot know it and must "
                    "not scan for candidates (F10 rejected)."
                ),
                reason="Judgment residue -- never mechanically inferred from disk state.",
                reportable=False,
            )
        )
    if kind == "handoff":
        points.append(
            build_untrusted_gate_judgment_point(
                id="j-continuation-vs-fork",
                question="Is this a continuation of the predecessor, or should it fork instead?",
                # "resolves" repointed to d1 (was d3, "handoff.stamp_phase" --
                # removed 2026-07-25, see module docstring) since that
                # directive no longer exists in this brief's emission.
                #
                # "excise" (2026-08-05, break-glass predecessor removal):
                # a THIRD disposition alongside "continue" -- the operator
                # authors the handoff (still resolves d1, same as
                # "continue"), but the resolved predecessor is deliberately
                # discarded: `brief()` reads this decision and nulls
                # `lineage["predecessor"]`/`lineage["predecessor_id"]`
                # BEFORE `_build_directives` runs, so d1's existing
                # `if _pred:` guard omits --predecessor/--predecessor-id by
                # construction and d6's existing
                # `if lineage.get("predecessor") is not None:` guard never
                # arms it -- no new directive-gating flag, reusing the same
                # machinery "continue" already flows through. This is
                # DISTINCT from the pre-existing empty-`artifact_path`
                # standalone shape: excise keeps `artifact_path` (and with
                # it the `deliverable_id`/`initiative` carry) while only
                # the predecessor edge is cut. See `brief()`'s own
                # decision_note-required handling for this disposition.
                dispositions=[
                    build_disposition(
                        "continue",
                        ["d1"],
                        guidance=(
                            "This is a continuation, not a fork, when the work resumes THIS "
                            "session's own thread -- including the next phase of the same "
                            "multi-phase workstream (research → goal-setting → plan → execute "
                            "→ verify), even when the phase boundary reads like a new topic. "
                            "That boundary is illusory; the workstream is one arc. Keeps the "
                            "resolved predecessor edge intact."
                        ),
                    ),
                    build_disposition(
                        "excise",
                        ["d1"],
                        guidance=(
                            "Break-glass predecessor removal: author the handoff but discard "
                            "the resolved predecessor edge deliberately -- `deliverable_id`/"
                            "`initiative` still carry from `artifact_path`, only the "
                            "predecessor link is cut. Distinct from a genuinely different "
                            "mid-session topic someone picks up cold, which is `/spinoff` "
                            "(outside this judgment point's scope) rather than a disposition "
                            "of this one. Requires a non-empty `decision_note` explaining why "
                            "the predecessor edge was cut."
                        ),
                    ),
                ],
                evidence="Requires reading the predecessor's actual content, not just its frontmatter.",
                reason="Judgment residue -- stays SKILL prose (C4).",
            )
        )
    # 2026-07-31 fix (two-sided question, one-sided answer set): this used to
    # emit unconditionally with the SOLE disposition `{"value": "mine",
    # "resolves": ["d1"]}` -- on a tree whose dirty paths belong to a sibling
    # session, the only way to proceed was to assert `mine`, which is false
    # and then travels into the baton record. The enum is NOT widened here
    # (still one disposition, "mine" -- an EM affirming attribution is still
    # judgment residue, per the module docstring); instead the ASK itself is
    # now conditional on `_compute_dirty_tree_attribution` finding this
    # session's OWN dirty paths (`mine`) non-empty -- a tree with nothing to
    # attribute to this session carries no live ask for `d1` to wait on, same
    # "clean tree carries no ask" shape `workday_complete._build_judgment_
    # points` (AC10, 2026-07-25) already established for its own dirty-tree
    # judgment point. A probe FAILURE (`degraded=True` -- see that function's
    # docstring for every degradation path) falls back to today's
    # unconditional emission: a failure to compute must never silently
    # resolve `d1`.
    attribution = dirty_tree_attribution
    if attribution is None:
        attribution = {
            "degraded": True,
            "evidence": (
                "dirty-tree attribution probe: no attribution computed for "
                "this call -- falling back to an unconditional ask."
            ),
        }
    if attribution.get("degraded") or attribution.get("mine"):
        if attribution.get("degraded"):
            evidence = attribution.get(
                "evidence",
                "dirty-tree attribution unavailable -- falling back to an unconditional ask.",
            )
        else:
            evidence = _dirty_tree_case_c_evidence(attribution)
        points.append(
            build_untrusted_gate_judgment_point(
                id="j-dirty-tree-case-c",
                question="Dirty-tree case-c: are the uncommitted changes this baton's own, or a sibling session's?",
                dispositions=[build_disposition("mine", ["d1"])],
                evidence=evidence,
                reason="Judgment residue -- stays SKILL prose (C4/C5).",
            )
        )
    return points


def _ready_summary(directives: list[dict[str, Any]], judgment_points: list[dict[str, Any]]) -> tuple[str, str]:
    """Narration/next-move pair for the envelope.

    A replay (at least one `already_satisfied` directive) gets its own suffix on
    BOTH strings: `brief` is the surface an operator reads before dispatching,
    and a brief that silently drops a directive from the work it is about to do
    reads as a narrower run than the one that was asked for. The suffix is
    appended only when something IS satisfied, so a clean run's two strings stay
    byte-identical to their pre-replay values."""
    satisfied = [d["id"] for d in directives if d.get("already_satisfied")]
    blocked_by = [jp["id"] for jp in judgment_points if jp.get("id")]
    if blocked_by:
        narration = (
            f"Computed the {len(directives)}-directive baton brief: "
            f"{len(blocked_by)} judgment point(s) open."
        )
        next_move = "Resolve the open judgment point(s) before dispatching the ready directives."
    else:
        narration = f"Computed the baton brief: {len(directives)} directive(s) ready to run."
        next_move = "Coast is clear -- dispatch the directives."
    if satisfied:
        narration += (
            f" {len(satisfied)} already satisfied on disk ({', '.join(satisfied)}) -- "
            "this is a REPLAY of a prior partially-applied run."
        )
        next_move += (
            f" {', '.join(satisfied)} will be reported as landed without re-dispatching; "
            "see each directive's own already_satisfied_reason."
        )
    return narration, next_move


def _emit(decision_object: dict[str, Any], exit_code: int) -> BriefResult:
    """The single validation chokepoint this module's `brief()` routes
    through -- mirrors pickup_assemble's `_emit` fail-loud discipline
    (non-empty `narration`; every `judgment_points[]` entry carries a
    `recommendation` key, even when its value is `None`)."""
    narration = decision_object.get("narration")
    if not narration:
        raise ValueError("_emit: decision object missing non-empty 'narration'")
    for jp in decision_object.get("judgment_points") or []:
        if "recommendation" not in jp:
            raise ValueError(
                f"_emit: judgment_points entry {jp.get('id', '<no id>')!r} missing "
                "required 'recommendation' key"
            )
    return BriefResult(decision_object, exit_code)


def _resolve_held_handoff_for_session(
    root: Path, *, allow_standalone: bool = False, session_id: Optional[str] = None
) -> tuple[Optional[str], list[str], bool]:
    """Self-resolves kind="handoff"'s predecessor(s) from the CURRENT
    session's own DURABLE claim ledger, for the `brief`/`apply` calling
    convention where the caller supplies no `artifact_path` at all -- the
    kind=handoff analogue of kind=spinoff's pre-existing self-resolution
    (`handoff_author_fork._resolve_origin_handoff`).

    Deliberately NOT the same mechanism as `_resolve_origin_handoff`: that
    resolver matches the `claimed_by`/`consumed_by` FRONTMATTER mirror on a
    live `state/handoffs/*.md` file, which goes stale the instant the file
    is swept to `archive/handoffs/` -- exactly the failure mode this
    resolver exists to survive. `coordinator_core.session.claims.
    list_claims_by_session` instead reads the DURABLE claim-record store
    (`<sessions_dir>/handoff-claims/<basename>/session_id`), which the boot
    sweep does not touch: that function's own "Claim-record LIFECYCLE"
    docstring documents a handoff claim SURVIVING both ship and archive.
    This is the ONE place in this module that resolves a claim-ledger entry
    to a baton path -- callers reuse this function rather than re-deriving
    the lookup.

    Returns `("state/handoffs/<basename>", [additional-basenames...],
    degraded)` -- the LIVE-directory contract shape (mirrors
    `_resolve_origin_handoff`'s own return convention) even when the named
    file(s) have since moved to `archive/handoffs/`; `resolve_lineage`'s
    archive-aware resolution (`_resolve_qualified_path_or_raise`) is what
    actually finds them there when these strings are fed back into
    `resolve_lineage` -- this function's own contract stops at "which
    basename(s) does the session hold", not "where do they currently live on
    disk". `degraded` (AC-6, folded candidate 7, 2026-08-13) is `True` when
    the composite key below could not distinguish two or more held claims --
    i.e. they tied on every leg through `basename`, or the set carried no
    readable claim metadata at all. This is a SET-LEVEL signal, not a
    statement about the primary pick specifically: it means the ORDERING OF
    THE SET (primary and additional alike) contains at least one position
    decided by the arbitrary basename tiebreak rather than an ordering fact
    -- it does NOT by itself imply the primary pick was arbitrary, since the
    tie may be entirely among non-primary claims while the primary is still
    cleanly ordered ahead of them. `False` for a single-claim set (nothing to
    tie) and for any multi-claim set the key actually orders. This reports
    THAT degradation happened somewhere in the set; it does NOT change which
    claim is picked.

    Fails loud (`ValueError`) rather than silently minting or silently
    picking one, on either of two conditions -- each names what was (or
    was not) found so the caller can pass the predecessor path explicitly
    instead of retrying blind:
      - no current session id is resolvable at all.
      - zero held handoff claims for the resolved session id, UNLESS
        `allow_standalone=True` (see below).

    `allow_standalone` (2026-08-03 break-class fix): a session that picked
    up via a CROSS-REPO MEMO rather than a handoff legitimately holds ZERO
    handoff claims and has no predecessor path to pass -- the `/handoff`
    skill names this shape explicitly ("Neither? This handoff has no
    predecessor -- write standalone."), so the engine hard-failing here was
    STRICTER than the doctrine it implements. When `allow_standalone=True`
    and the resolved session holds zero handoff claims, this returns
    `(None, [])` instead of raising -- a caller-recognizable "standalone,
    no predecessor" signal, never a stale or guessed path. The
    no-resolvable-session-id condition is unaffected by this flag and
    always raises: that one names a genuine environment failure (no way to
    even ask the ledger), not a legitimate zero-claims shape. Defaults to
    `False` so the OTHER call site of this function (the `is_plan_input`
    ledger read in `resolve_lineage`, where a claimed plan with no matching
    handoff claim is a suspicious shape worth a judgment point, not a
    silent standalone brief) keeps its existing fail-loud contract
    unchanged.

    The sharper reason `lineage["standalone_no_predecessor_reason"]`
    earns its keep: a bare-slug fresh-mint call (caller passes an
    intentionally empty `artifact_path`) and this standalone-fallback
    call (session self-resolved to zero held claims) BOTH land on
    `discovery == "mint"` with `predecessor is None` -- nothing else in
    `lineage` tells those two shapes apart. The field is the only signal
    that distinguishes "caller explicitly named a fresh mint target" from
    "self-resolution found nothing to claim".

    2026-07-29 break-class fix -- a session holding MORE than one handoff
    claim is a legitimate shape (a session can genuinely hold two batons),
    not an error: this used to hard-fail here as "ambiguous", permanently
    stranding whichever predecessor lost the coin flip at
    `deployment_state` non-terminal (see this module's own docstring's
    "76/91 DoE" note). The FIRST (earliest-claimed) held handoff becomes the
    PRIMARY predecessor -- DoE's `coordinator/skills/handoff/SKILL.md` §
    Predecessor identification defines the predecessor as "whatever handoff
    this session was opened with", which this extends rather than replaces
    for the N>1 case: the handoff the session was opened with is the one it
    claimed FIRST. Every other held claim is returned as an ADDITIONAL
    predecessor, in the same earliest-first order, for the caller to thread
    into `resolve_lineage`'s `additional_predecessor_paths`.

    Ordering signal (DR-291, `docs/decisions/DR-291-*.md`, extended by
    DR-292 -- see `docs/decisions/` for the re-brief-ordering leg below):
    each held claim sorts on a FOUR-part composite key, applied per-claim
    rather than set-wide -- `(stage_rank, claimed_at_or_sentinel,
    claim_mtime, basename)`. The legs are deliberately ordered by how much
    they actually know about claim order:

    0. `stage_rank` (2026-08-13, DR-292, closing sedge-15 AC-8; widened
       2026-08-13 to a three-way tier once the `stamped` marker existed to
       ask it) -- `0` for an `apply`-stage claim whose frontmatter stamp is
       CONFIRMED landed (`session.claims.claim_stamped`), `1` for an
       `apply`-stage claim that is not (including every pre-existing claim
       dir predating the `stamped` marker -- see the inline comment at the
       stage-rank computation for the back-compat guarantee this preserves),
       `2` for a `brief`-stage claim OR for a claim whose `stage` file is
       missing/unreadable/unrecognized. This leg exists because
       `session.claims.touch_brief_claim` rewrites a
       `brief`-stage claim's `claimed_at` (and, with it, the claim-file
       mtime -- see leg 1's correction below) on every re-brief, so legs 1
       and 2 alone can move a re-briefed reservation LATER than a worked
       (`apply`-stage) baton with an earlier claim time. Ranking `apply`
       ahead of `brief` restores "the baton actually being worked sorts
       first" without needing a new writer: the `stage` file already exists
       in every claim dir written by `_write_claim_meta`. An unreadable/
       absent stage is NOT privileged into rank 0 -- unknown stage is
       treated the same as `brief`, mirroring leg 1's own "unknown is not
       earliest" rule for `claimed_at`. GUARANTEE this leg gives: among
       held claims, every `apply`-stage claim outranks every `brief`-stage
       (or stage-unknown) claim, regardless of `claimed_at`. It does NOT
       guarantee earliest-claimed-first ordering WITHIN the `apply` tier or
       WITHIN the `brief` tier when a `brief`-stage claim has been
       re-briefed -- that residual (leg 1's re-brief hole) is UNCHANGED by
       this leg; it narrows the leg's effect to same-stage-tier claims, it
       does not close it.
    1. `claimed_at` -- the claim dir's own `claimed_at` file (ISO8601,
       written once by `session.claims._write_claim_meta` at claim time).
       This is write-once for an `apply`-stage claim, but NOT for a
       `brief`-stage one: `session.claims.touch_brief_claim` deliberately
       rewrites `claimed_at` to now on re-brief, as a lease refresh, and
       `list_claims_by_session` does not filter by stage -- so a re-briefed
       claim's rewritten `claimed_at` reaches this ordering function too
       (2026-08-13 correction; the prior "never touched again" text here was
       false). Consequence: a re-brief can move a claim LATER in the
       ordering, so PRIMARY may not be the earliest-claimed baton once a
       brief-stage claim has been re-briefed. This is still the durable,
       recorded INTENT of claim order, so it leads. A claim missing (or
       unable to read) this file sorts on the high sentinel `"￿"` instead --
       UNKNOWN is not "earliest"; treating a missing timestamp as earliest
       would silently promote an unrecorded claim ahead of ones this
       function can actually prove came first.
    2. `claim_mtime` -- the `claimed_at` FILE's own `st_mtime` (falling back
       to the claim DIR's `st_mtime`, then to `float("inf")` when neither
       stats). This sits under `claimed_at` because it is only a
       machine-local filesystem fact, not a recorded claim -- but it still
       tracks write order, which is exactly what's needed to break a
       same-second `claimed_at` tie (two claims recorded within the same
       ISO8601 second) without falling all the way to an alphabetical
       tiebreak that carries no time content at all. The `claimed_at` file
       specifically (not the sibling `stage` file) is used because for an
       `apply`-stage claim `_write_claim_meta` writes it once and never
       rewrites it, while `stage` IS rewritten on brief->apply promotion and
       would desync mtime from claim order. Note this mtime leg does NOT
       backstop the `touch_brief_claim` re-brief case described in leg 1
       above: the same rewrite that moves `claimed_at` also moves the file's
       `st_mtime`, so both legs move together and neither corrects the
       other.
    3. `basename` -- the terminal, total, deterministic tiebreak, unchanged
       from before. It carries no time content at all, so it only ever
       decides when both of the above are exactly equal (or entirely
       absent, e.g. no `sessions_dir`).

    Applying this key PER-CLAIM (rather than set-wide, as before) fixes the
    prior degradation: one claim missing `claimed_at` used to collapse the
    ENTIRE set to basename order, discarding real ordering signal for every
    OTHER claim that did have one. Now a claim with a known `claimed_at`
    always outranks one without, regardless of what else is in the set.

    `degraded` (AC-6): computed after sorting, by comparing every claim's
    full `(stage_rank, claimed_at_key, mtime_key)` prefix against its
    immediate neighbour in sorted order -- `True` iff two or more claims tie
    on all three (which also covers "the set carried no readable claim
    metadata at all": every claim then lands on the same sentinel triple).
    This mirrors DR-291's own "two claims tie on claimed_at and mtime"
    residual, widened by one leg to match the new key.
    """
    from coordinator_core.ops.session_context import resolve_current_session_id
    from coordinator_core.session import core as _session_core
    from coordinator_core.session.claims import (
        CLAIM_STAGE_APPLY,
        claim_stamped,
        list_claims_by_session,
    )

    # `session_id`, when the caller supplies it, is an ALREADY-RESOLVED id
    # and is used verbatim — this module's standing contract (every tier
    # takes resolved inputs rather than re-deriving them). Re-deriving here
    # is what broke `apply --session-id <id>`: apply() resolves the id, binds
    # it into `apply_base.session_identity`'s per-var ContextVars, and calls
    # brief() inside that scope — but `core.resolve_session_id` reads its own
    # override ContextVar and then `os.environ`, neither of which that scope
    # touches. The two identity mechanisms never meet, so an explicit
    # --session-id resolved to nothing here and the caller was told no
    # session was resolvable while holding one in its hand. Callers with no
    # id of their own keep the ambient resolution below, unchanged.
    session_id = session_id or resolve_current_session_id(root)
    if not session_id:
        raise ValueError(
            "baton_assemble: kind='handoff' with no artifact-path supplied, but no "
            "current session id is resolvable (CLAUDE_SESSION_ID / "
            "CLAUDE_CODE_SESSION_ID / sentinel file) -- pass the predecessor path "
            "explicitly."
        )
    matches = list_claims_by_session(session_id, cwd=str(root))
    held_handoffs = sorted(
        basename for class_, basename in matches if class_ == "handoff-claims"
    )
    if not held_handoffs:
        if allow_standalone:
            return None, [], False
        raise ValueError(
            f"baton_assemble: kind='handoff' with no artifact-path supplied, but "
            f"session {session_id!r} holds ZERO handoff claims in the durable claim "
            "ledger -- pass the predecessor path explicitly."
        )
    if len(held_handoffs) == 1:
        return "state/handoffs/" + held_handoffs[0], [], False

    sessions_dir = _session_core.sessions_dir(str(root))
    _SENTINEL = "￿"
    ordering: list[tuple[int, str, float, str]] = []
    for basename in held_handoffs:
        stage_rank = 2
        claimed_at_key = _SENTINEL
        mtime_key = float("inf")
        if sessions_dir:
            claim_dir = Path(sessions_dir) / "handoff-claims" / basename
            stage_file = claim_dir / "stage"
            try:
                stage_raw = stage_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                # Review: coordinatorstaff-eng-f4ecb2da Finding 0 -- a
                # non-UTF-8 stage file must degrade ordering, never crash
                # brief()'s unguarded call site.
                stage_raw = None
            # Unlike `session.claims.claim_stage` (which defaults a missing/
            # unreadable stage to "apply" for a LIVENESS-check caller, where
            # that default must never make a live holder's claim takeable),
            # an absent/unreadable stage here is deliberately NOT privileged
            # into rank 0 -- see this function's own docstring, leg 0: unknown
            # is not "worked", any more than leg 1 treats unknown as
            # "earliest".
            # A `stage == apply` claim can be either a genuinely-landed
            # frontmatter stamp or one whose stamp attempt was refused --
            # `apply.py::apply` promotes brief->apply unconditionally BEFORE
            # any directive runs and never reverts it on refusal (see
            # f592df0bb329, the pickup_assemble sibling fix for the same
            # defect class). `stage` alone therefore cannot distinguish
            # "worked and landed" from "worked and refused" within the apply
            # tier -- but it STILL soundly answers "has this claim moved
            # past mere reservation", which is the question leg 0 above is
            # actually asking, so it is not replaced here, only refined.
            # `claim_stamped` supplies the missing leg: RANKED ALONGSIDE
            # stage (not replacing it) as a three-way brief / apply-
            # unstamped / apply-stamped ordering, because a claim whose
            # stamp is CONFIRMED landed is more genuinely "worked" than one
            # merely promoted-and-unconfirmed, which is itself still more
            # worked than a bare reservation.
            #
            # Back-compat: claim dirs written before f592df0bb329 carry no
            # `stamped` marker at all (mark_claim_stamped is a new writer),
            # so every pre-existing `apply`-stage claim lands in the middle
            # apply-unstamped tier (rank 1), never the bottom `brief` tier
            # (rank 2) -- it still outranks every `brief`-stage claim exactly
            # as before this change, so the existing ordering is refined, not
            # inverted, for the entire pre-existing corpus.
            if stage_raw == CLAIM_STAGE_APPLY:
                stage_rank = 1
                if claim_stamped(claim_dir):
                    stage_rank = 0
            claimed_at_file = claim_dir / "claimed_at"
            try:
                claimed_at = claimed_at_file.read_text(encoding="utf-8").strip() or None
            except (OSError, UnicodeDecodeError):
                # Review: coordinatorstaff-eng-f4ecb2da Finding 0 -- same
                # corrupt-metadata edge as the stage read above.
                claimed_at = None
            if claimed_at:
                claimed_at_key = claimed_at
            try:
                mtime_key = claimed_at_file.stat().st_mtime
            except OSError:
                try:
                    mtime_key = claim_dir.stat().st_mtime
                except OSError:
                    mtime_key = float("inf")
        ordering.append((stage_rank, claimed_at_key, mtime_key, basename))

    ordering.sort(key=lambda quad: (quad[0], quad[1], quad[2], quad[3]))
    ordered_basenames = [basename for _, _, _, basename in ordering]

    degraded = any(
        ordering[i][:3] == ordering[i + 1][:3] for i in range(len(ordering) - 1)
    )

    primary = "state/handoffs/" + ordered_basenames[0]
    additional = ["state/handoffs/" + basename for basename in ordered_basenames[1:]]
    return primary, additional, degraded


# ---------------------------------------------------------------------------
# `/handoff` residue segments -- the consumer half of `residue_segments`'s
# `filter_key`/`legal_values`/`active_values` parameterisation (73a9da8d3).
# `coordinator_core.review_assemble.residue` is the reference/precedent
# caller: same `resolve_content_root()` -> `load_segments` -> `select_segments`
# idiom, deliberately mirrored here, NOT re-invented. See this section's
# `_load_handoff_residue_segments` docstring for the one place this caller's
# contract diverges from that precedent (fail-open, not fail-loud).
# ---------------------------------------------------------------------------

#: The closed enum of legal `case:` frontmatter values for the handoff
#: residue segment corpus. The corpus itself lives in the coordinator
#: content root (resolved via `resolve_content_root()`), NOT in this repo --
#: `skills/handoff/residue/*.md`, one file per segment, each carrying a
#: `case:` field drawn from this tuple.
SEGMENT_CASES: tuple[str, ...] = ("shared", "dirty-tree", "carried-items", "predecessor")

#: The one true handoff-residue segment directory, relative to the content
#: root -- the `segment_dir` parameter passed to the shared loader. Mirrors
#: `review_assemble.residue`'s `_RESIDUE_SEGMENT_DIR` naming/shape.
_HANDOFF_RESIDUE_SEGMENT_DIR = os.path.join("skills", "handoff", "residue")


def _predecessor_carried_items_active(root: "Optional[Path]", predecessor: "Optional[str]") -> bool:
    """True iff `predecessor` (a `lineage["predecessor"]` value -- either
    root-relative or absolute, per `resolve_lineage`'s own storage
    convention) names a file whose frontmatter carries a `carried_items:`
    value that is itself a non-empty YAML list. Mirrors `DoE-claude@HEAD:
    coordinator/hooks/scripts/handoff-segment-inject.py`'s
    `_carried_items_active` exactly, list-type check included
    (`isinstance(items, list) and len(items) > 0`) -- a `carried_items:`
    key present but holding a mapping/scalar/empty value does NOT arm this
    case, matching the consumer rather than the looser "non-empty raw
    text" test this helper used before. A read/parse failure (missing
    file, unreadable, undecodable, malformed frontmatter fence, invalid
    YAML, non-mapping frontmatter, `carried_items:` absent or
    present-but-not-a-non-empty-list) degrades to False -- fail-open,
    never raises. `yaml` is imported locally here, same precedent as
    `_walk_deliverable_ancestor_set`'s own local `import yaml` elsewhere in
    this module, so this function's own frontmatter read (`path.read_text`
    via `_read_frontmatter`, then `yaml.safe_load`) is caught locally
    rather than relying on a broader caller-side `try` -- this helper is
    itself fail-open end to end, independent of `brief()`'s own
    `try/except` around segment loading."""
    if not predecessor:
        return False
    candidate = Path(predecessor)
    if not candidate.is_absolute() and root is not None:
        candidate = root / predecessor
    try:
        fm_text = _read_frontmatter(candidate)
    except (OSError, UnicodeDecodeError):
        return False
    if not fm_text:
        return False
    import yaml

    try:
        fm_dict = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return False
    if not isinstance(fm_dict, dict):
        return False
    items = fm_dict.get("carried_items")
    return isinstance(items, list) and len(items) > 0


def _resolve_handoff_residue_active_cases(
    dirty_tree_attribution: dict[str, Any],
    lineage: dict[str, Any],
    root: "Optional[Path]",
) -> set[str]:
    """Resolve the active `case:` set for this `brief()` call, matched
    signal-for-signal against `DoE-claude@HEAD:coordinator/hooks/scripts/
    handoff-segment-inject.py`'s `compute_active_cases` -- the consumer this
    `segments` key is meant to let retire its own copy of this computation.
    A drift here is not cosmetic: it silently changes what the consumer
    renders once it adopts this key, so each conditional below cites the
    consumer function it is deliberately matching, not just how this call
    site's own inputs happen to be shaped.

    `shared` is unconditional. `dirty-tree` arms when the tree is dirty AT
    ALL, matching the consumer's `_is_dirty_tree` (a bare non-empty `git
    status --porcelain`) -- NOT this call's own `mine`-only ("dirt
    attributable to this session") test, which under-fires on a machine
    running many concurrent sessions against one shared tree. This call's
    own `_compute_dirty_tree_attribution(root)` result already carries both
    halves of that whole-tree signal (`mine` plus `residue_count`, the
    complement) from the SAME porcelain read `j-dirty-tree-case-c` uses, so
    no second git probe is added here -- `len(mine) + residue_count > 0`
    reconstructs "porcelain was non-empty" from values already in scope. A
    `degraded=True` probe (no signal computed at all) degrades this case to
    inactive, fail-open, same as every other predicate here -- matching,
    not diverging from, the consumer: `_is_dirty_tree` returns `False`
    whenever its own `_resolve_repo_root()` returns `None` (repo root
    undeterminable), and that function's docstring names the exact same
    choice ("callers degrade the `dirty-tree` case to inactive rather
    than raising"). Verified directly against `DoE-claude@HEAD:
    coordinator/hooks/scripts/handoff-segment-inject.py` -- this is an
    equivalent degrade path on both sides, not a signal-fidelity gap.
    `carried-items`
    arms when `lineage["predecessor"]` names a file whose OWN frontmatter
    carries a non-empty `carried_items:` array, matching the consumer's
    `_carried_items_active` -- NOT merely "a `d7` directive exists", which
    the previous revision used as a stand-in and which fires for every
    predecessor regardless of whether its `carried_items` array is empty or
    absent (`d7` is gated only on `kind == "handoff" and <a predecessor>`).
    See `_predecessor_carried_items_active` for the frontmatter read itself.
    `predecessor` arms when `lineage["predecessor"]` is non-null -- already
    correct, matched to the consumer's own `predecessor` case unchanged."""
    active = {"shared"}
    if not dirty_tree_attribution.get("degraded") and (
        dirty_tree_attribution.get("mine") or dirty_tree_attribution.get("residue_count")
    ):
        active.add("dirty-tree")
    if _predecessor_carried_items_active(root, lineage.get("predecessor")):
        active.add("carried-items")
    if lineage.get("predecessor"):
        active.add("predecessor")
    return active


def _load_handoff_residue_segments(active_values: set[str]) -> list[dict[str, Any]]:
    """Load and select the handoff-residue segments applicable to
    *active_values*: `resolve_content_root()` -> `load_segments` ->
    `select_segments`, mirroring `review_assemble.residue`'s idiom exactly.

    Fail-open is this CALL SITE's contract alone, never this helper's or
    the shared loader's: `brief()` catches `SegmentLoadError` /
    `ResolveCoordinatorCloneError` around the call to this helper and
    attaches nothing on either. That is a deliberate inversion of
    `review_assemble.residue`'s fail-loud contract -- `/handoff` fires
    under context pressure by definition, and a retrieval seam that
    hard-fails there is worse than one that says nothing -- and it must
    NOT be "harmonised" toward fail-loud in either direction. Zero
    applicable segments is NOT this helper's error to raise: it returns
    `[]` and the caller attaches an empty (present, not absent) list. This
    helper itself stays exception-transparent -- it raises neither
    exception itself; both propagate from `load_segments`/
    `resolve_content_root` unchanged so the caller's `except` catches
    them by name. The shared loader gains no `strict=`/`fail_open=`
    parameter for this or any other caller -- that refusal is reaffirmed,
    not revisited, here."""
    content_root = Path(resolve_content_root())
    segments = load_segments(
        content_root,
        _HANDOFF_RESIDUE_SEGMENT_DIR,
        filter_key="case",
        legal_values=SEGMENT_CASES,
    )
    return select_segments(segments, filter_key="case", active_values=active_values)


def brief(
    kind: str,
    artifact_path: str,
    decisions: Optional[dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
    title: Optional[str] = None,
    explicit_deliverable_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> BriefResult:
    """`brief <kind> [artifact-path] [--decisions <json>] [--title <text>]
    [--deliverable-id <id>]` -- the single-shot decision-object computation
    for a `kind` in {handoff, spinoff}. Read-only throughout; mutates
    nothing. `decisions` is currently accepted for signature parity with the
    Tier-B contract's two-phase-stateless protocol. Every judgment point
    built here is untrusted-gate in the sense that resolving it never
    mutates state directly -- `_build_directives`'s per-predecessor
    `{d6_id}-closed-predecessor-decline` point is the landed exception to
    "no directive fires purely off a disposition" (its sibling, the
    roadmap-baton kind-first decline, is RETIRED -- DR-172, 2026-08-18,
    C10). C4/C5 wire further directive-gating dependencies as the SKILL
    callers land. `title`, when
    supplied, is passed through to d1's `coordinator-doc-new` scaffold as
    `--title=<title>`; when omitted, d1 omits the flag entirely and
    `coordinator-doc-new`'s own default (placeholder) title applies
    unchanged.

    `explicit_deliverable_id` (2026-08-05, closing the unreachable opt-in
    `resolve_lineage`'s own docstring names): the ONE caller-facing entry
    point for `resolve_lineage`'s `explicit_deliverable_id` kwarg. Accepted
    ONLY for `kind == "spinoff"` -- threaded through unchanged, never
    re-minted, never slug-derived, never overridden by the progenitor read
    (`resolve_lineage`'s spinoff branch already implements this precedence;
    this parameter is the missing caller). Supplying it for `kind ==
    "handoff"` raises `ValueError` rather than silently ignoring the flag:
    the handoff cascade already has its own claimed-plan -> predecessor ->
    mint tiers (`resolve_deliverable_and_initiative`), and an explicit
    override would cut across which of THOSE tiers is authoritative with no
    named precedence rule -- `resolve_lineage`'s own docstring already
    states `explicit_deliverable_id` is not consulted on that branch, so
    silently accepting it here would look like it worked while doing
    nothing. A future ruling that wants an explicit handoff override needs
    to name where in that cascade it wins, not just thread the flag through.

    `decisions["j-continuation-vs-fork"] == {"disposition": "excise",
    "decision_note": <reason>}` (2026-08-05, break-glass predecessor
    excise) is a SECOND way to reach "no predecessor", alongside the
    empty-`artifact_path` standalone shape below -- but unlike that shape
    it keeps `artifact_path` (and the `deliverable_id`/`initiative` carry
    resolved from it), only cutting the predecessor edge itself.
    `decision_note` is REQUIRED and non-empty (fail-loud `ValueError`
    otherwise) and is threaded straight into `lineage[
    "standalone_no_predecessor_reason"]` as the recorded reason.

    `artifact_path` is OPTIONAL for `kind="handoff"` (2026-07-28 -- kind=
    spinoff already self-resolved its own origin baton via
    `handoff.author_fork`'s stamping mode; kind=handoff had no equivalent,
    so a caller had to guess/name the predecessor path -- and could guess
    wrong). A falsy `artifact_path` with `kind="handoff"` self-resolves via
    `_resolve_held_handoff_for_session` -- the session's OWN held handoff
    claim, read from the durable claim ledger (survives the predecessor
    handoff being archived, unlike the `claimed_by` frontmatter mirror).
    `kind="spinoff"` keeps requiring a truthy `artifact_path` (the bare-slug
    mint convention already covers its "nothing existing to name" case).

    2026-08-03 break-class fix: that self-resolution call now passes
    `allow_standalone=True` -- a session that picked up via a cross-repo
    memo (rather than a handoff) legitimately holds ZERO handoff claims,
    which used to hard-fail here even though the `/handoff` skill
    explicitly sanctions a standalone, no-predecessor brief for exactly
    this shape. `lineage["standalone_no_predecessor_reason"]` is always
    present (`None` on every other path) and non-`None` only when THIS
    call self-resolved to zero held claims -- an explicit, self-describing
    signal that downstream consumers must not read as "predecessor
    unknown" or invent a lineage for."""
    if kind not in KINDS:
        raise ValueError(f"baton_assemble.brief: unrecognized kind {kind!r} (expected one of {KINDS})")
    if explicit_deliverable_id and kind == "handoff":
        raise ValueError(
            "baton_assemble.brief: --deliverable-id is spinoff-only -- "
            "kind='handoff' already resolves deliverable_id via its own "
            "claimed-plan -> predecessor -> mint cascade "
            "(resolve_deliverable_and_initiative), and an explicit override "
            "would cut across which tier of that cascade is authoritative "
            "with no named precedence rule. Omit --deliverable-id for a "
            "handoff brief, or use kind='spinoff'."
        )
    decisions = decisions or {}
    repo_root_was_cwd_derived = repo_root is None
    root = repo_root or resolve_repo_root()
    if root is None:
        raise TransportFailure("could not resolve a git worktree root")

    # C3 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md):
    # the C1 foreign-repo gate, mirroring C2's wiring in
    # `workstream_complete.brief`. `_resolve_current_session_id()` is a pure
    # env read that ignores `root`, so it is uncontaminated by whether `root`
    # itself is the wrong repo. Gated ONLY when `root` was cwd-derived (an
    # explicitly-passed `repo_root` is an unambiguous statement of caller
    # intent that never touched cwd) -- MISMATCH still never refuses when
    # `repo_root` was explicit, but the gate is always CALLED and its
    # verdict always RECORDED in `gates["repo_identity"]` below.
    repo_identity_gate = compute_repo_identity_gate(root, _resolve_current_session_id())
    if repo_root_was_cwd_derived and repo_identity_gate["verdict"] == "MISMATCH":
        raise TransportFailure(repo_identity_gate["message"])

    # Break-glass predecessor excise (2026-08-05, cascade reach added
    # 2026-08-14): `j-continuation-vs-fork`'s "excise" disposition (see
    # `_build_judgment_points`) is read HERE, before `resolve_lineage`/
    # `_build_directives` run. The gate below is fail-loud, no partial
    # computation, when "excise" carries no non-empty `decision_note` --
    # mirrors the `disposition_detail`-required shape the carried_items/
    # plan-tasks carry gate uses for its own terminal states
    # (schema_validate.py `_validate_disposition_carrying_items`): a
    # predecessor removed with no recorded reason is exactly the
    # silent-lineage-break `lineage["standalone_no_predecessor_reason"]`
    # exists to prevent, so this verb refuses to compute a brief that would
    # carry that gap forward. `_excise_predecessor`/`_excise_decision_note`
    # feed TWO downstream effects, not one: `excise_rung` below (threaded
    # into `resolve_lineage`, reaching `resolve_deliverable_and_initiative`
    # so a divergent rung can actually be waived) and the post-return
    # null-out further down (which still only nulls `lineage["predecessor"]`
    # / `["predecessor_id"]` for `_build_directives`'s benefit).
    _continuation_decision = decisions.get("j-continuation-vs-fork")
    _excise_predecessor = (
        isinstance(_continuation_decision, dict)
        and _continuation_decision.get("disposition") == "excise"
    )
    _excise_decision_note: Optional[str] = None
    if _excise_predecessor:
        _note = _continuation_decision.get("decision_note")
        if not isinstance(_note, str) or not _note.strip():
            raise ValueError(
                "baton_assemble.brief: j-continuation-vs-fork disposition "
                "'excise' requires a non-empty decision_note -- a "
                "predecessor removed with no recorded reason is exactly "
                "the silent-lineage-break standalone_no_predecessor_reason "
                "exists to prevent"
            )
        _excise_decision_note = _note

    # Which rung the excise cuts (2026-08-14): captured BEFORE the
    # self-resolution block below can overwrite `artifact_path`, so this
    # reflects what the CALLER supplied, not what `brief` filled in on its
    # behalf. Explicit `artifact_path` -> the operator named the predecessor
    # -> cut `_plan_file` (the auto-discovered claimed-plan rung). Empty
    # `artifact_path` -> the predecessor is about to be self-resolved from
    # the claim ledger -> cut `_predecessor_file` instead, keeping the
    # operator-named claimed plan. `None` for `kind == "spinoff"` or when no
    # excise was requested; `resolve_lineage` ignores it for spinoff and
    # no-ops on `None`.
    _excise_rung: Optional[str] = None
    if _excise_predecessor and kind == "handoff":
        _excise_rung = "plan_file" if artifact_path else "predecessor_file"

    # Divergent-deliverable_id resolution (`j-divergent-deliverable-id`),
    # reaching the SAME cut `j-continuation-vs-fork` already threads: the
    # operator names which rung carries, and `resolve_lineage`'s existing
    # excise removes the loser before `resolve_deliverable_and_initiative`
    # would raise. No auto-pick is introduced -- an absent or unresolved
    # decision leaves `_excise_rung` alone and the raise below converts to
    # a judgment point instead, per DR-207 DD#1.
    _divergence_decision = decisions.get("j-divergent-deliverable-id")
    _divergence_disposition: Optional[str] = None
    if isinstance(_divergence_decision, dict):
        _divergence_disposition = _divergence_decision.get("disposition")
    if _divergence_disposition in ("keep-plan", "keep-predecessor"):
        if kind != "handoff":
            raise ValueError(
                "baton_assemble.brief: j-divergent-deliverable-id is a "
                f"handoff-cascade point and does not apply to kind={kind!r} "
                "-- the spinoff branch resolves its deliverable_id from the "
                "progenitor read, not from the claimed-plan/predecessor rungs"
            )
        _divergence_note = _divergence_decision.get("decision_note")
        if not isinstance(_divergence_note, str) or not _divergence_note.strip():
            raise ValueError(
                "baton_assemble.brief: j-divergent-deliverable-id requires "
                "a non-empty decision_note -- DR-207 DD#1 resolves a "
                "divergence by the earliest-artifact test, and which "
                "artifact came first is exactly the fact this engine cannot "
                "recompute later from the id it carried"
            )
        _divergence_cut = (
            "predecessor_file" if _divergence_disposition == "keep-plan" else "plan_file"
        )
        if _excise_rung is not None and _excise_rung != _divergence_cut:
            raise ValueError(
                "baton_assemble.brief: j-continuation-vs-fork and "
                "j-divergent-deliverable-id name opposing rungs "
                f"({_excise_rung!r} vs {_divergence_cut!r}) -- excise_rung "
                "cuts exactly one rung, so these two dispositions cannot "
                "both be honoured; resolve them to the same rung and re-run"
            )
        _excise_rung = _divergence_cut
    elif _divergence_disposition is not None:
        raise ValueError(
            "baton_assemble.brief: j-divergent-deliverable-id disposition "
            f"{_divergence_disposition!r} is not one of 'keep-plan' / "
            "'keep-predecessor'"
        )

    # Also asserts the shared operator-config resolution seam (B0) resolves
    # cleanly -- a corrupt settings_home/claude_klabauter_root/doe_root value fails
    # loud here rather than downstream in a directive dispatch.
    resolve_operator_config()

    additional_predecessor_paths: list[str] = []
    # Present-as-None on every run (matches `resolve_lineage`'s own
    # `resumed_successor`/`adopted_scaffold` convention) -- non-None only
    # when THIS call self-resolved via the claim ledger and found zero held
    # handoff claims. Distinct from `lineage["predecessor"] is None` alone:
    # that also happens for a genuinely-supplied artifact_path whose own
    # frontmatter simply names no predecessor, which is not this shape.
    standalone_no_predecessor_reason: Optional[str] = None
    # Only this self-resolution call site has a degradation fact to override
    # `resolve_lineage`'s own default (`False`, set unconditionally near its
    # `is_plan_input` branch -- see that function's comment) with; `None`
    # means "this call site never ran", not "not degraded".
    _brief_ledger_degraded: Optional[bool] = None
    if not artifact_path and kind == "handoff":
        resolved_predecessor, additional_predecessor_paths, _brief_ledger_degraded = (
            _resolve_held_handoff_for_session(
                root, allow_standalone=True, session_id=session_id
            )
        )
        if resolved_predecessor is None:
            # 2026-08-03 break-class fix: a memo-pickup session legitimately
            # holds zero handoff claims and has no predecessor path to pass
            # -- the `/handoff` skill sanctions this exact shape ("Neither?
            # This handoff has no predecessor -- write standalone."), so
            # this proceeds to `resolve_lineage` with `artifact_path` still
            # empty (the SAME bare-slug/mint convention kind=spinoff's own
            # empty-path case already uses) rather than raising.
            standalone_no_predecessor_reason = (
                "kind='handoff' self-resolution: the current session holds "
                "ZERO handoff claims in the durable claim ledger and no "
                "artifact-path was supplied -- standalone handoff, no "
                "predecessor."
            )
        else:
            artifact_path = resolved_predecessor

    try:
        lineage = resolve_lineage(
            kind,
            artifact_path,
            root,
            additional_predecessor_paths=additional_predecessor_paths,
            title=title,
            explicit_deliverable_id=explicit_deliverable_id,
            excise_rung=_excise_rung,
            session_id=session_id,
        )
    except DivergentDeliverableIdError as _divergence_exc:
        # Actionable, not terminal. The refusal to auto-pick is UNCHANGED --
        # this converts a crash with no sanctioned exit into the assembler's
        # ordinary judgment-point round trip. `directives` is empty by
        # construction, so `apply` cannot proceed until the operator names a
        # winner; nothing here selects one. A multi-plan session (the
        # `/mise-en-place` shape) reaches this arm rather than the dead end.
        _divergence_plan_rel = resolve_claimed_plan_path(cwd=root)
        _divergence_plan_file = (
            str(root / _divergence_plan_rel) if _divergence_plan_rel else None
        )
        # Archive-aware, matching what `resolve_lineage` actually read:
        # a predecessor already swept to `archive/handoffs/` is the routine
        # case, and reporting the caller-supplied live path would read its
        # deliverable_id as absent -- losing the very fact DR-207 DD#1's
        # earliest-artifact test needs. Fail-soft: this is an error-reporting
        # path, so an unresolvable path degrades to the raw one rather than
        # raising over the top of the divergence being reported.
        _divergence_predecessor_file = artifact_path or None
        if _divergence_predecessor_file:
            try:
                _divergence_predecessor_file = str(
                    _resolve_qualified_path_or_raise(
                        _normalize_artifact_path(_divergence_predecessor_file), root, kind
                    )
                )
            except Exception:  # noqa: BLE001 -- see fail-soft note above
                pass
        _divergence_point = _build_divergent_deliverable_id_judgment_point(
            _divergence_plan_file,
            _divergence_predecessor_file,
            _resolve_additional_predecessor_paths(
                additional_predecessor_paths, root, kind
            ),
            root,
            _divergence_exc,
        )
        _divergence_narration = (
            "Blocked on a deliverable_id divergence -- the carry-or-mint "
            "cascade's rungs disagree. Resolve j-divergent-deliverable-id "
            "and re-run `brief`."
        )
        return _emit(
            build_envelope(
                artifact={"path": artifact_path, "kind": kind, "lineage": {}},
                preflight={},
                gates={},
                directives=[],
                judgment_points=[_divergence_point],
                decisions=decisions,
                narration=_divergence_narration,
                next_move=(
                    "Apply DR-207 DD#1's earliest-artifact test to the rungs "
                    "named in j-divergent-deliverable-id's evidence, then "
                    "re-run `brief` with --decisions naming the surviving "
                    "rung and a decision_note recording which artifact came "
                    "first. No directive fires until then."
                ),
            ),
            EXIT_OK,
        )
    lineage["standalone_no_predecessor_reason"] = standalone_no_predecessor_reason
    if _brief_ledger_degraded is not None:
        lineage["predecessor_ordering_degraded"] = _brief_ledger_degraded
    # Break-glass predecessor excise, continued from the decision-note gate
    # above: nulling `predecessor`/`predecessor_id` here -- BEFORE
    # `_build_directives` reads either -- is the whole mechanism. d1's
    # existing `if _pred:` guard (below, in `_build_directives`) then omits
    # --predecessor/--predecessor-id by construction, and d6's existing
    # `if lineage.get("predecessor") is not None:` guard never arms --
    # no separate directive-gating flag needed for either effect.
    # `deliverable_id`/`initiative` are UNTOUCHED here (they came from
    # `artifact_path` via `resolve_lineage`, not from the predecessor edge
    # being cut) -- that is the entire point of this disposition over the
    # pre-existing empty-`artifact_path` standalone shape, which threw the
    # carry away along with the predecessor.
    if _excise_predecessor:
        lineage["predecessor"] = None
        lineage["predecessor_id"] = None
        lineage["standalone_no_predecessor_reason"] = _excise_decision_note
    # `lineage["artifact_path"]` is the single already-normalized value (see
    # `_normalize_artifact_path`) -- reused here for the envelope's own
    # top-level `artifact.path` so it never desyncs from what d1/d2/d3/d5
    # actually received.
    normalized_artifact_path = lineage.get("artifact_path") or artifact_path
    # Review: coordinatorcode-reviewer-c2d43fc7 Finding 5 -- computed ONCE
    # here (rather than once in `_build_directives`'s d6 gate and again
    # below for the judgment-point check) and threaded through both call
    # sites, since neither reads/writes `lineage` between them.
    _predecessor_canonical_kind = (
        _resolved_predecessor_canonical_kind(lineage["predecessor"], root)
        if lineage.get("predecessor") is not None
        else ""
    )
    directives = _build_directives(
        kind,
        lineage,
        title=title,
        root=root,
        decisions=decisions,
        predecessor_canonical_kind=_predecessor_canonical_kind,
    )
    # Backstop invariant (fail loud, not silent): no computed directive may
    # write its output over the input artifact just read for lineage. See
    # `_assert_no_directive_writes_over_input`'s own docstring.
    _assert_no_directive_writes_over_input(directives, normalized_artifact_path, root)
    dirty_tree_attribution = _compute_dirty_tree_attribution(root)
    judgment_points = _build_judgment_points(kind, dirty_tree_attribution)
    # DR-172 (2026-08-18, plan a-session-always-has-a-baton, chunk C10)
    # RETIRES the roadmap-baton kind-first decline judgment point that used
    # to be surfaced here (C3, PIN-3): DR-126 § Clarifications C-1's rule --
    # never automatically supersede a roadmap-baton predecessor, in any
    # state -- is gone; a roadmap-baton predecessor now supersedes exactly
    # like any other, and its successor is minted AS a roadmap-baton
    # inheriting its identity (`_build_directives`'s d1_args mint-kind
    # flip). See `ops/handoff_archive_transition.py`'s module docstring for
    # the mirror-image removal at that seam.
    #
    # C4 (PIN-3): a plan input whose plan-ness discriminator routed it to the
    # durable claim ledger, but found zero held claims -- NEVER a silent
    # non-arm. See `_build_plan_no_ledger_claim_judgment_point`'s own
    # docstring for the replay-vs-stranding distinction this surfaces.
    # C1 (2026-08-13, closed-baton-is-terminal plan): surface
    # `d6-closed-predecessor-decline` (per-predecessor) whenever a resolved
    # predecessor in the fan-in carries `deployment_state: closed` -- the
    # SAME discriminator `_build_directives`'s per-predecessor closed-baton
    # gate applies. One point per skipped predecessor, ids unique per
    # predecessor (`{d6_id}-closed-predecessor-decline`) so `jp_by_id` stays
    # keyed correctly -- mirrors the d6/d6-N id uniqueness constraint.
    #
    # This loop is reachable for every predecessor now that
    # `_build_directives`'s per-predecessor loop is unconditional (no more
    # all-or-nothing gate hoisted on the primary predecessor's kind) --
    # every judgment point surfaced here has a corresponding directive-
    # building code path able to consult its resolution.
    if kind == "handoff" and lineage.get("predecessor") is not None:
        _all_predecessors_for_decline = [lineage["predecessor"]] + list(
            lineage.get("additional_predecessors") or []
        )
        for _decline_i, _decline_pred_path in enumerate(_all_predecessors_for_decline):
            _decline_d6_id = "d6" if _decline_i == 0 else f"d6-{_decline_i + 1}"
            if _resolved_predecessor_deployment_state(_decline_pred_path, root) == "closed":
                judgment_points.append(
                    _build_closed_predecessor_decline_judgment_point(
                        _decline_pred_path,
                        _resolved_predecessor_closed_reason(_decline_pred_path, root),
                        _decline_d6_id,
                        root,
                    )
                )
    if kind == "handoff" and lineage.get("plan_ledger_no_claim"):
        judgment_points.append(
            _build_plan_no_ledger_claim_judgment_point(
                normalized_artifact_path, lineage["plan_ledger_no_claim"], root
            )
        )
    # sedge-04 (PIN-3): narration-only fan-in cardinality legibility. Never
    # wired into `_build_judgment_points` (no `lineage` in scope there, per
    # the research corpus § 3) -- appended here alongside the other
    # `lineage`-scoped conditional points above, same append-site precedent.
    if kind == "handoff" and lineage.get("additional_predecessors"):
        judgment_points.append(_build_fan_in_cardinality_judgment_point(lineage, root))
    narration, next_move = _ready_summary(directives, judgment_points)

    envelope = build_envelope(
        artifact={"path": normalized_artifact_path, "kind": kind, "lineage": lineage},
        preflight={},
        gates={"repo_identity": repo_identity_gate},
        directives=directives,
        judgment_points=judgment_points,
        decisions=decisions,
        narration=narration,
        next_move=next_move,
    )
    result = _emit(envelope, EXIT_OK)
    if kind == "handoff":
        # Active-case resolution runs OUTSIDE the try below, on purpose:
        # `_resolve_handoff_residue_active_cases` (and the
        # `_predecessor_carried_items_active` frontmatter read it calls) is
        # fail-open in its own right and never raises
        # `SegmentLoadError`/`ResolveCoordinatorCloneError`, so widening the
        # `try` to cover it would only risk silently swallowing a genuine
        # bug in that resolution -- the opposite of what this call site's
        # fail-open contract is for. Fail-open at THIS call site only --
        # see `_load_handoff_residue_segments`'s docstring for why.
        # Attached post-`_emit` (post-validation), mirroring
        # `review_assemble.residue`'s `result["segments"] = selected`
        # idiom exactly; never a ninth `build_envelope` key. On either
        # named exception the `segments` key stays ABSENT and every other
        # field of `result` is exactly what it is today -- never a bare
        # `except Exception`, which would swallow a genuine bug in the
        # brief computation above.
        active_cases = _resolve_handoff_residue_active_cases(
            dirty_tree_attribution, lineage, root
        )
        try:
            selected_segments = _load_handoff_residue_segments(active_cases)
        except (SegmentLoadError, ResolveCoordinatorCloneError):
            pass
        else:
            result.decision_object["segments"] = selected_segments
        _print_commits_into_baton(root)
    return result


def _print_commits_into_baton(root: Path) -> None:
    """C5 (docs/plans/2026-08-18-a-session-always-has-a-baton.md § C5, part
    b): resolve this session's attributed commit shas via C4's
    ``session.commits`` primitive and merge them into the session's baton
    record (``session_baton.store.merge_baton`` — dedup-extends, never
    replaces). Fired at the handoff-kind brief's tail only, mirroring
    quick_wrap_assemble's own call site.

    Compatible with this function's own READ-ONLY contract (see `brief`'s
    docstring): that guarantee is about NOT mutating tracked-tree state
    (handoffs, plans — the Tier-B stateless-computation contract); the
    session baton is the ephemeral, ``.git/coordinator-sessions/<sid>/``
    -scoped advisory record C1 establishes, already written unconditionally
    on every session's first prompt (``session_baton_mint``) — this call
    adds no new mutation SURFACE, only more complete data on an existing one.

    Fail-open throughout: session-id resolution, primitive resolution, and
    the baton write are all best-effort — an advisory record must never
    block a handoff brief's own computation.
    """
    sid = _resolve_current_session_id()
    if not sid:
        return
    try:
        commits = resolve_session_commits(root, sid)
    except (ValueError, RuntimeError):
        return
    shas = [c["sha"] for c in commits]
    if not shas:
        return
    try:
        merge_baton(sid, cwd=str(root), commits=shas)
    except Exception:  # noqa: BLE001 — advisory write must never raise into brief()
        pass


# ---------------------------------------------------------------------------
# CLI entrypoint (mirrors pickup_assemble's argv-parse shape)
# ---------------------------------------------------------------------------


def validate_decisions_shape(decisions: Any) -> Optional[str]:
    """Validates a parsed `--decisions` JSON value against the required
    judgment-point-id -> `{"disposition": <str>}` map shape.

    DUPLICATED, not imported, from `coordinator_core.pickup_assemble.
    validate_decisions_shape` (072ae91c) — `baton_assemble` and
    `pickup_assemble` are independently-developed sibling computed-skill
    engines with no established cross-import between their own modules (the
    ONE deliberately shared surface is `coordinator_core.contract.apply_base`,
    the generic directive-execution machinery; this validator is not part of
    that contract). Importing pickup_assemble here would invert that
    established twin/no-cross-import shape for a ~15-line self-contained
    function. Keep both copies in sync by hand if the shape contract ever
    changes.

    Well-formed JSON with the wrong VALUE shape (`{"j1": "proceed"}` instead
    of `{"j1": {"disposition": "proceed"}}`) used to be silently ignored: the
    judgment point stayed unresolved with no error, which reads as a gating
    outcome rather than a usage error. This closes that gap by making a
    wrong-shaped payload fail loud, mirroring the existing malformed-JSON
    usage-error path exactly (same exit code, same stderr channel) rather
    than inventing a new error convention.

    Returns `None` when `decisions` is a valid map (including the empty
    map). Returns a one-line, actionable error string naming the first
    offending id, the shape it received, and the expected shape otherwise.

    `value` is accepted as an exact equivalent of `disposition` (normalized
    to `disposition` in place, so every downstream reader keeps seeing one
    shape): `brief`'s own OUTPUT vocabulary names the choice-key `value`
    (`dispositions=[{"value": "proceed", ...}]` in `_build_judgment_points`
    below), and an operator round-tripping that output straight back into
    `--decisions` was rejected for using the engine's own word. Extra
    sibling keys (e.g. `decision_note`) are tolerated, not rejected — this
    engine has no closed content-key allowlist (contrast
    `pickup_assemble.validate_decisions_shape`, which does). If BOTH keys
    are present and disagree, that is genuinely ambiguous and fails loud
    naming both values.

    Negative-spec: does NOT coerce a bare string into
    `{"disposition": <string>}`. Coercion would silently make two distinct
    payloads mean the same thing and paper over an operator's mistake — the
    ruling here is fail-loud, not be-liberal. A dict carrying NEITHER
    `disposition` nor `value` still fails loud, same as before.
    """
    if not isinstance(decisions, dict):
        return (
            f"--decisions must be a JSON object mapping judgment-point id to "
            f'{{"disposition": <value>}}, got {type(decisions).__name__}'
        )
    for jp_id, value in decisions.items():
        if not isinstance(value, dict):
            return (
                f"--decisions[{jp_id!r}] must be shaped "
                f'{{"disposition": <value>}}, got {value!r} — expected form: '
                f'{{"{jp_id}": {{"disposition": "<value>"}}}}'
            )
        has_disposition = "disposition" in value
        has_value = "value" in value
        if not has_disposition and not has_value:
            return (
                f"--decisions[{jp_id!r}] must be shaped "
                f'{{"disposition": <value>}}, got {value!r} — expected form: '
                f'{{"{jp_id}": {{"disposition": "<value>"}}}}'
            )
        if has_disposition and has_value and value["disposition"] != value["value"]:
            return (
                f"--decisions[{jp_id!r}] carries both 'disposition' "
                f"({value['disposition']!r}) and 'value' ({value['value']!r}) "
                f"and they disagree — supply only one"
            )
        if not has_disposition:
            value["disposition"] = value.pop("value")
        elif has_value:
            del value["value"]
    return None


_USAGE_LINES = (
    "usage: {prog} brief <kind> [artifact-path] [--decisions <json> | --decisions-file <path>] [--title <text>]",
    "       {prog} apply <kind> [artifact-path] [--session-id <id>] [--decisions <json> | --decisions-file <path>] [--title <text>]",
    "       (artifact-path is optional for kind=handoff on BOTH verbs -- self-resolves",
    "        the predecessor from the current session's own claim ledger)",
    "       --decisions is a JSON object: {{\"<jp-id>\": {{\"disposition\": \"<value>\", ...}}}}",
    "       (\"value\" is accepted as an exact equivalent of \"disposition\" -- brief's own",
    "        output uses that key). Legal <value>s for a given jp-id are that judgment",
    "        point's own dispositions[].value entries from this run's `brief` output.",
    "       --deliverable-id ID  (spinoff only) Existing deliverable_id to carry (never",
    "        re-mint). Rejected for kind=handoff, which resolves its own via the",
    "        claimed-plan -> predecessor -> mint cascade.",
)


def _usage(prog: str) -> int:
    for line in _USAGE_LINES:
        print(line.format(prog=prog), file=sys.stderr)
    return EXIT_USAGE


def _usage_help(prog: str) -> int:
    """`--help`/`-h` request at ANY parse point in the top-level CLI or
    `brief`'s own arm -- prints the usage lines to stdout (the conventional
    --help stream, unlike `_usage`'s stderr for a genuine error) and
    returns `EXIT_OK` (0), never a usage-error exit. `apply`'s own
    `--help`/`-h` handling lives in `apply.main_apply` (see that module's
    `_usage_help`) -- this function only covers the bare top level and the
    `brief` arm this module owns directly.

    Checked BEFORE any positional token is treated as `subcmd`/`kind`/
    `artifact_path`, mirroring `apply.main_apply`'s own ordering -- see
    that module's `_usage_help` docstring for the reproduced live break
    (`baton-assemble apply handoff --help` silently mutating disk) this
    ordering discipline closes fleet-wide across both CLI arms."""
    for line in _USAGE_LINES:
        print(line.format(prog=prog))
    return EXIT_OK


def main(argv: list[str]) -> int:
    if not argv:
        return _usage("baton-assemble")
    if argv[0] in ("--help", "-h"):
        return _usage_help("baton-assemble")
    subcmd, rest = argv[0], argv[1:]

    if subcmd == "apply":
        from coordinator_core.baton_assemble.apply import main_apply

        return main_apply(rest)

    if subcmd != "brief":
        print(f"baton-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
        return _usage("baton-assemble")

    if rest and rest[0] in ("--help", "-h"):
        return _usage_help("baton-assemble")

    if not rest:
        return _usage("baton-assemble")

    kind = rest[0]
    if kind.startswith("-"):
        print(f"baton-assemble: kind must not look like a flag: {kind!r}", file=sys.stderr)
        return EXIT_USAGE

    # `artifact-path` is a MANDATORY positional for every kind EXCEPT
    # "handoff" (2026-07-28): kind="handoff" now self-resolves the
    # predecessor from the current session's own claim ledger when omitted
    # (see `_resolve_held_handoff_for_session`) -- kind="spinoff" (and any
    # future kind) keeps requiring it, since the bare-slug mint convention
    # already covers spinoff's "nothing existing to name" case and self-
    # resolution is a handoff-only addition per this fix's own scope.
    after_kind = rest[1:]
    if after_kind and after_kind[0] in ("--help", "-h"):
        return _usage_help("baton-assemble")

    artifact_path = ""
    tail = after_kind
    if after_kind and not after_kind[0].startswith("-"):
        artifact_path = after_kind[0]
        tail = after_kind[1:]

    if not artifact_path and kind != "handoff":
        return _usage("baton-assemble")

    if any(tok in ("--help", "-h") for tok in tail):
        return _usage_help("baton-assemble")

    decisions: dict[str, Any] = {}
    title: Optional[str] = None
    deliverable_id: Optional[str] = None
    conflict = detect_conflicting_payload_channels(tail)
    if conflict is not None:
        print(f"baton-assemble: {conflict}", file=sys.stderr)
        return EXIT_USAGE
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--deliverable-id":
            if i + 1 >= len(tail):
                return _usage("baton-assemble")
            deliverable_id = tail[i + 1]
            i += 2
        elif (payload := resolve_json_payload_flag(tail, i)).consumed:
            if payload.error is not None:
                print(f"baton-assemble: {payload.error}", file=sys.stderr)
                return EXIT_USAGE
            decisions = payload.value
            shape_error = validate_decisions_shape(decisions)
            if shape_error is not None:
                print(f"baton-assemble: {shape_error}", file=sys.stderr)
                return EXIT_USAGE
            i += payload.consumed
        elif tok == "--title":
            if i + 1 >= len(tail):
                return _usage("baton-assemble")
            title = tail[i + 1]
            i += 2
        elif not tok.startswith("-") and title is None and artifact_path:
            # Unambiguous position: once artifact-path and --title are the
            # only two positional/flag slots left, a bare non-flag token
            # here can only be the title. Accepting it removes a whole
            # round-trip (a `--help` re-read to discover `--title` exists)
            # for the common `brief spinoff <slug> "Some Title"` shape.
            title = tok
            i += 1
        elif not tok.startswith("-") and title is None:
            # artifact-path is optional for kind=handoff and was NOT bound
            # from a positional here -- a bare token in this position is
            # genuinely ambiguous between artifact-path and title, so this
            # stays an error. Offer the fix rather than just naming the
            # violation (design-as-offers, project CLAUDE.md).
            print(
                f"baton-assemble: unrecognized argument {tok!r} — did you mean "
                f"--title {tok!r}?",
                file=sys.stderr,
            )
            return EXIT_USAGE
        else:
            print(f"baton-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return EXIT_USAGE

    _brief_kwargs: dict[str, Any] = {"title": title}
    if deliverable_id is not None:
        _brief_kwargs["explicit_deliverable_id"] = deliverable_id
    try:
        result = brief(kind, artifact_path, decisions, **_brief_kwargs)
    except TransportFailure as exc:
        print(f"baton-assemble: transport failure: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL
    except ValueError as exc:
        print(f"baton-assemble: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(json.dumps(result.decision_object, indent=2))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
