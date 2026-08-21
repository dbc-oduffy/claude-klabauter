"""
coordinator_core.review_assemble.residue — the `review-assemble residue`
computed-skill engine backing the `/review` skill's residue-brief seam.

Purpose: `/review` needs a small, surface-appropriate set of standing
reminders (residue) rendered alongside a plan review or a diff review —
today that content is scattered prose the EM has to remember to reread.
This module composes ONE `brief(artifact_arg)` op that (1) resolves which
surface is in play (`plan` / `diff` / genuinely unresolved) per the
`--surface RESOLUTION CONTRACT` below, (2) reads every segment file under
the residue directory, filters by resolved surface, sorts by declared
`order`, and (3) emits the result through the shared
`coordinator_core.contract.decision_object.envelope` chokepoint — mirroring
`coordinator_core.orient_assemble`'s `brief(cadence)` idiom exactly:
`brief(...) -> build_envelope(...) -> return dict(_envelope_emit(envelope))`.

Residue directory (fixed resolution, no parallel ladder):
    os.path.join(resolve_content_root(), "skills", "review", "residue")
via `coordinator_core.resolve_coordinator_clone.resolve_content_root` — the
same content-root resolver every other computed-skill engine uses. This
module deliberately does NOT read a DoE-root pointer directly (that
resolves only a DoE clone and breaks on a pure OSS install); it composes
the shared resolver instead.

Segment contract (frozen, authored alongside this module): each file
directly under the residue directory carries YAML frontmatter with exactly
four keys —

    ---
    segment_id: <stable-kebab-case-id, unique within the residue directory>
    surface: plan | diff | shared
    class: protected | droppable
    order: <integer>
    ---

The directory listing IS the manifest — there is no separate registry file
a segment must additionally appear in. A segment's own frontmatter is its
registration; this module discovers segments purely by listing the
directory.

`--surface RESOLUTION CONTRACT` (implemented exactly, not improvised) — a
precedence ladder, explicit beats inferred:
  1. an explicit `--surface plan|diff` (a caller/engine that has already
     resolved the surface, e.g. `coordinator/commands/review.md` passing
     `--surface plan`, `review-code.md` passing `--surface diff`)
                                          -> resolved surface = that value,
     no inference, no judgment point. HIGHEST precedence — an explicit
     surface WINS even when the artifact/diff context would otherwise infer
     the opposite surface (that contradiction is exactly the silent
     mis-resolution this ladder exists to close). An explicit `--surface`
     value outside `{"plan", "diff"}` is a `ResidueUsageError` (CLI: usage
     exit code) — never a silent fallthrough to inference.
  2. else an artifact argument resolving to a path under `docs/plans/`, or
     any path ending in `.md`                     -> resolved surface = plan
  3. else no artifact argument at all, with a non-empty local diff surface
     (`git diff --stat HEAD` against the resolved repo root is non-blank)
                                                    -> resolved surface = diff
  4. else (an artifact argument that is neither a plans-path nor a `.md`
     path; or no artifact argument and an empty diff) is genuine ambiguity
     -> this module does NOT silently default. It emits the `shared`-class
     segments only and raises a `judgment_points[]` entry (via
     `build_judgment_point`, `recommendation=None`) offering the
     `plan`/`diff` dispositions back to the caller. Judgment points are
     offers, never verdicts — this module never picks a surface on the
     caller's behalf.

Negative-spec:
  - Does NOT import `coordinator_core.pickup_assemble` for its
    `resolve_repo_root` helper — that module is 12,000+ lines and measured
    at 443.8ms to import for one function, against this module's other
    engine imports at 40-58ms. This module re-homes an equivalent
    `resolve_repo_root(start=None)` locally (same `git rev-parse
    --show-toplevel` resolution and `None`-on-failure contract), so
    `residue.py` sits at the interpreter floor rather than inheriting that
    cliff. See `coordinator_core/benchmarks/process_time.py`.
  - Does NOT copy `pickup_assemble`'s forked `_emit` (that implementation
    predates the shared `contract/decision_object` library) — this module
    routes exclusively through `coordinator_core.contract.decision_object.
    envelope.build_envelope`/`_emit`.
  - Does NOT construct a parallel resolution ladder for the residue
    directory — the one `resolve_content_root()` call above is the only
    resolution step; no fallback probing of alternate locations.
  - Does NOT do per-domain routing beyond `plan`/`diff`/`shared` — this is
    a single flat assembler, not a god-assembler dispatching to other
    skills' concerns (DoE-claude `docs/decisions/DR-092-canonical-resolution-
    engine.md`; NOT the retired prime-v1 DR-092, which numbers a different
    decision in `DoE-claude/archive/prime-v1-decisions/`).
  - Does NOT emit a well-formed envelope carrying zero residue. An empty
    residue directory, an unreadable content root
    (`ResolveCoordinatorCloneError`), or a segment set that resolves to
    zero applicable segments after surface filtering is a fail-loud
    `ResidueAssembleError` (or the resolver's own error propagated
    unchanged) — never a silent empty-but-valid envelope. That silent
    fail-open shape is exactly what AC-14(a) exists to catch.

Spec backlink: DoE-claude:pln-review-skill-computed-residue--db84bf, chunk C3
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    _emit as _envelope_emit,
)
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.contract.residue_segments import (
    SegmentLoadError,
    load_segments,
    select_segments,
)
from coordinator_core.resolve_coordinator_clone import (
    ResolveCoordinatorCloneError,
    resolve_content_root,
)
from coordinator_core.win_portability import no_console_creationflags

#: The three legal values of a segment's `surface:` frontmatter field.
#: `shared` applies to every resolved surface; `plan`/`diff` apply only when
#: that surface is the one actually resolved this call.
SEGMENT_SURFACES: tuple[str, ...] = ("plan", "diff", "shared")

#: The two legal values of an explicit `--surface` argument — deliberately
#: narrower than `SEGMENT_SURFACES` (no `shared`; a caller resolves to a
#: concrete surface, never to the segment-authoring category).
EXPLICIT_SURFACES: tuple[str, ...] = ("plan", "diff")

#: Windows console-window suppression, matching every other subprocess
#: call site this package touches (`baton_assemble/__init__.py`,
#: `baton_assemble/apply.py`) — this repo ships to a Windows-primary
#: audience (DR-148); a bare `subprocess.run` spawning `git` without this
#: flag flashes a console window on every invocation.
_NO_CONSOLE = no_console_creationflags()


#: The review-side name for the shared segment-loader's failure type — an
#: alias, not a subclass, so every existing `except ResidueAssembleError`
#: call site keeps catching fail-louds raised by the shared loader
#: (`coordinator_core.contract.residue_segments`) as well as the ones this
#: module raises directly (e.g. zero applicable segments after filtering).
#: One exception hierarchy, not two.
ResidueAssembleError = SegmentLoadError


class ResidueUsageError(RuntimeError):
    """Raised when an explicit `--surface` value is given but is not one of
    `EXPLICIT_SURFACES`. A CLI wrapper maps this to its own usage exit code
    (2) — never a silent fallthrough to inference. Deliberately a distinct
    type from `ResidueAssembleError` (a business/content-shape failure): this
    is a caller-usage failure, resolved before any residue directory or git
    state is even touched."""


#: The one true residue directory, relative to the content root — the
#: `segment_dir` parameter this module passes to the shared loader.
_RESIDUE_SEGMENT_DIR = "skills/review/residue"


def _residue_dir(content_root: Path) -> Path:
    """Resolve the one true residue directory relative to *content_root*:
    ``<content-root>/skills/review/residue``."""
    return content_root / _RESIDUE_SEGMENT_DIR


def resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Resolve the enclosing git worktree root for *start* (default cwd).

    A module-local re-home of `pickup_assemble.resolve_repo_root` — that
    module is 12,000+ lines and measured at 443.8ms to import for this one
    function; this module's other engine imports are 40-58ms. Same
    `git rev-parse --show-toplevel` resolution, same "returncode != 0 (incl.
    not-a-worktree) -> None" contract, using this module's own `_NO_CONSOLE`
    subprocess posture rather than `pickup_assemble`'s `_run_git`.
    """
    cwd = start or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def _diff_is_nonempty(repo_root: Path) -> bool:
    """True when `git diff --stat HEAD` against *repo_root* produces any
    output. Read-only (no mutation, no fetch) — mirrors the read-only-git
    posture every computed-skill engine holds."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


def _artifact_is_plan_shaped(artifact_arg: str) -> bool:
    """True when *artifact_arg* resolves to a path under `docs/plans/` or
    any path ending in `.md` — the exact `--surface RESOLUTION CONTRACT`
    "plan" clause, no broader inference."""
    normalized = artifact_arg.replace("\\", "/")
    return normalized.startswith("docs/plans/") or normalized.endswith(".md")


def _ambiguous_surface_judgment_point(artifact_arg: Optional[str]) -> dict[str, Any]:
    """Build the judgment point this module raises on genuine surface
    ambiguity — an offer of `plan`/`diff` dispositions, never a verdict."""
    evidence = (
        f"artifact argument {artifact_arg!r} did not resolve to docs/plans/ or a "
        ".md path" if artifact_arg is not None
        else "no artifact argument given and the local diff surface is empty"
    )
    return build_judgment_point(
        None,
        id="review-assemble-residue-surface-ambiguous",
        question="Which surface should this residue brief render for — plan or diff?",
        dispositions=[
            build_disposition("plan"),
            build_disposition("diff"),
        ],
        evidence=evidence,
        reason="insufficient-evidence",
    )


def _resolve_surface(
    artifact_arg: Optional[str],
    repo_root: Path,
    *,
    explicit_surface: Optional[str] = None,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Resolve `plan` / `diff` / unresolved, per the `--surface RESOLUTION
    CONTRACT` precedence ladder in the module docstring. Returns
    `(surface, judgment_point)` where exactly one of the pair is non-`None`:
    a resolved surface carries no judgment point, and an unresolved surface
    (`None`) always carries one — this function never returns `(None,
    None)`.

    `explicit_surface` — already validated to be one of `EXPLICIT_SURFACES`
    by the caller (`brief`) — takes HIGHEST precedence and short-circuits
    every inference rule below it, including a context that would otherwise
    infer the opposite surface. That is deliberate: an explicit surface
    contradicting inferred context is exactly the silent mis-resolution this
    ladder exists to close, not a case to reconcile.
    """
    if explicit_surface is not None:
        return explicit_surface, None

    if artifact_arg is not None:
        if _artifact_is_plan_shaped(artifact_arg):
            return "plan", None
        return None, _ambiguous_surface_judgment_point(artifact_arg)

    if _diff_is_nonempty(repo_root):
        return "diff", None

    return None, _ambiguous_surface_judgment_point(artifact_arg)


def _select_segments(
    segments: list[dict[str, Any]], surface: Optional[str]
) -> list[dict[str, Any]]:
    """Filter *segments* to those applicable to *surface* (a `shared`
    segment applies to every surface, including an unresolved one), sorted
    ascending by declared `order`. `surface=None` (unresolved) selects only
    the `shared` segments, per the module docstring's UNRESOLVED-surface
    rule. Thin wrapper over the shared loader's `select_segments`, supplying
    review's own `surface` filter key and `{surface, "shared"}` /
    `{"shared"}` active-value set."""
    active_values = {"shared"} if surface is None else {surface, "shared"}
    return select_segments(segments, filter_key="surface", active_values=active_values)


def brief(
    artifact_arg: Optional[str] = None,
    *,
    repo_root: Optional[Path] = None,
    explicit_surface: Optional[str] = None,
) -> dict[str, Any]:
    """Compute the review-residue decision object for *artifact_arg*.

    `artifact_arg` is the raw `--artifact` value the caller passed (a plan
    path, or `None` when no artifact was named). `explicit_surface` is the
    raw `--surface` value the caller passed (already-resolved by the
    caller/engine per `coordinator/skills/review/SKILL.md`) — when given, it
    takes HIGHEST precedence and short-circuits inference entirely; resolve
    to a `plan` / `diff` / unresolved surface otherwise follows the
    `--surface RESOLUTION CONTRACT` precedence ladder in the module
    docstring exactly. Read-only: performs no disk mutation and no git fetch
    (only a read-only `git diff --stat` probe when no artifact argument is
    given and no explicit surface was passed).

    Raises `ResidueUsageError` if `explicit_surface` is given but is not one
    of `EXPLICIT_SURFACES` — a caller-usage failure, checked before any
    residue directory or git state is touched, and never silently
    downgraded to inference. Raises `ResolveCoordinatorCloneError`
    (propagated unchanged) or `ResidueAssembleError` on any other fail-loud
    condition — this function never returns a well-formed envelope carrying
    zero residue.
    """
    if explicit_surface is not None and explicit_surface not in EXPLICIT_SURFACES:
        raise ResidueUsageError(
            "review-assemble residue: --surface must be one of "
            f"{EXPLICIT_SURFACES!r}, got {explicit_surface!r}"
        )

    root = repo_root or resolve_repo_root()
    if root is None:
        raise ResidueAssembleError(
            "review-assemble residue: could not resolve a git worktree root"
        )
    root = Path(root)

    content_root = Path(resolve_content_root())
    residue_dir = _residue_dir(content_root)
    segments = load_segments(
        content_root,
        _RESIDUE_SEGMENT_DIR,
        filter_key="surface",
        legal_values=SEGMENT_SURFACES,
    )

    surface, judgment_point = _resolve_surface(
        artifact_arg, root, explicit_surface=explicit_surface
    )
    selected = _select_segments(segments, surface)

    if not selected:
        raise ResidueAssembleError(
            "review-assemble residue: resolved surface "
            f"{surface!r} has zero applicable segments in {residue_dir}"
        )

    judgment_points = [judgment_point] if judgment_point is not None else []

    envelope = build_envelope(
        artifact={"artifact_arg": artifact_arg, "surface": surface},
        preflight={},
        gates={},
        directives=[],
        judgment_points=judgment_points,
        decisions={"residue_dir": residue_dir.relative_to(content_root).as_posix()},
        narration=(
            f"review-assemble residue: surface={surface!r}, "
            f"{len(selected)} segment(s) selected of {len(segments)} total."
            if surface is not None
            else "review-assemble residue: surface unresolved -- rendering "
            f"{len(selected)} shared segment(s) only; see judgment_points[]."
        ),
        next_move=(
            "Render segments[] in order."
            if surface is not None
            else "Resolve judgment_points[0] (plan vs diff), then re-run with "
            "--artifact set accordingly."
        ),
    )
    result = dict(_envelope_emit(envelope))
    result["segments"] = selected
    return result
