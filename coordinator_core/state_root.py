"""
coordinator_core.state_root — canonical state-root seam resolver.

Purpose: single entry point for resolving the coordinator state directory root.
Encodes the ratified state-placement taxonomy and DoE/claude-klabauter plane routing so
every state-writing caller resolves a root through one seam instead of
open-coding a root variable.

Port of: coordinator-state-root.sh (DoE 6fb5fb37, 2026-07-22).

COMPOSED — this module does NOT reimplement the four sibling resolver ladders. It
dispatches the 5-rule state-root routing on top of the already-native peers:
  - coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()  (Optional[str])
  - coordinator_core.engine_root.coordinator_engine_root()            (str, raises)
  - coordinator_core.artifact_subject.classify()                     (engine|doctrine|cross-cutting)
  - coordinator_core.meta_repo_identity.is_meta_repo()               (bool, raises)

Spec backlinks:
  docs/plans/2026-07-03-stop-the-rot-claude-klabauter-state-home-placement.md § C2 / AC2
  docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.3
  docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (de-bash W2)

Five routing rules (verbatim from the bash oracle's header):

  Rule 1  central=True, subject="doctrine"
            -> <coordinator_doe_root()>/state
            Fail-loud (StateRootError) if the DoE root cannot resolve. Does NOT
            fall back to claude-klabauter.

  Rule 2  central=True, subject="engine"
            -> <coordinator_engine_root()>/state

  Rule 3  central=True, artifact=<path>
            -> classify(<path>); map result:
                 doctrine      -> DoE state   (Rule 1)
                 engine        -> claude-klabauter state (Rule 2)
                 cross-cutting -> fail-loud: raise CrossCuttingStateRoot (rc 2),
                                  do NOT return a state path.

  Rule 4  central=True, no subject, no artifact  [BACKWARD-COMPAT DEFAULT]
            -> <coordinator_engine_root()>/state
            Every existing --central caller resolves to claude-klabauter, unchanged.

  Rule 5  central=False  [BACKWARD-COMPAT DEFAULT]
            -> <coordinator_engine_root()>/state  when cwd git root IS the meta-repo
               <git_root>/state                   when cwd git root is a sibling repo
                                                    AND is not the published mirror
                                                    (see "Published-mirror guard" below)
            Fail-loud on unresolvable git root.

  Published-mirror guard (applies to every rule that resolves claude-klabauter engine
  state — Rules 2, 4, and 5's meta-repo branch — AND Rule 5's sibling-repo
  branch): DR-132's two-tier gate means the resolved claude-klabauter root can now be
  a PUBLISHED engine mirror (`RESOLUTION_RESOLVED_ENGINE`), not only a live
  working tree (`RESOLUTION_LIVE_WORKING_TREE`). State must never be written
  into a published mirror — doing so leaks runtime/session artifacts
  (observed: operator-machine-codename-bearing filenames) into a public repo
  and can strand or split a state corpus. This module fail-louds
  (`StateRootError`) whenever engine-subject state would resolve under a
  `RESOLUTION_RESOLVED_ENGINE` root (Rules 2/4/5-meta), mirroring this
  module's existing fail-loud posture for other unresolvable/ambiguous cases
  (Rule 1, Rule 3's cross-cutting branch). It does NOT invent an alternative
  state location.

  Rule 5's sibling-repo branch gets the SAME guard applied to an arbitrary
  candidate git root rather than only "my own" claude-klabauter root: before treating
  a non-meta cwd git root as its own state root, it asks
  `coordinator_core.engine_root.published_engine_mirror_path()` (the same
  `repos.claude_klabauter` discriminator `_claude_klabauter_state()` already uses,
  exposed standalone) whether that git root IS the registered published
  mirror clone (e.g. a `claude-klabauter` checkout sitting on disk as an
  otherwise-ordinary sibling repo). If so: fail-loud, same rationale and
  same StateRootError shape as the engine-subject guard above — a mirror
  clone must never become a state root, whether reached via "this is the
  claude-klabauter engine root" (Rules 2/4/5-meta) or "this is just some sibling repo"
  (Rule 5 sibling). Bug backlog:
  `state/bug-backlog/2026-08-13-state-root-rule-5-cannot-tell-a-publishe-fd79452138b2.yaml`.

Public API:
    coordinator_state_root(central=False, subject=None, artifact=None, git_root=None) -> str
        Returns the resolved state-root path. Raises StateRootError (the rc-1
        shape) on any failure; raises CrossCuttingStateRoot (the rc-2 shape) when
        an artifact classifies cross-cutting (Rule 3). ``git_root`` (Rule 5 only)
        lets a caller that already resolved its own repo root pass it through
        explicitly instead of relying on cwd-based git-toplevel discovery.
    print_map() -> str
        Single-line JSON central map {doctrine, engine} — unresolvable subjects
        emit JSON null (never a hard error), mirroring ``--print-map``.
    main(argv) -> int
        CLI-shaped wrapper preserving the bash oracle's exit codes (0/1/2) for
        parity testing.

Argument rules (mirror the bash oracle):
    subject and artifact are mutually exclusive; passing both raises StateRootError.
    subject must be "engine" or "doctrine" when given.

Negative-spec (faithfully reproduced — do NOT "fix" mid-port):
    - Does NOT write any state; only resolves the path.
    - Does NOT fall back silently when git root is missing (Rule 5) — fail-loud.
    - Does NOT fall back to claude-klabauter when doctrine DoE root fails (Rule 1).
    - Does NOT auto-route a cross-cutting artifact (Rule 3) — fail-loud rc 2.
    - Does NOT reimplement any of the four sibling resolver ladders.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

from coordinator_core.artifact_subject import Subject, classify, remediation_message
from coordinator_core.git import repo_root as _repo_root_seam
from coordinator_core.engine_root import (
    _RESOLUTION_UNVERIFIED_ENV_LITERAL,
    classify_env_resolved_root,
    coordinator_engine_root,
    coordinator_engine_root_with_class,
    published_engine_mirror_path,
)
from coordinator_core.meta_repo_identity import (
    MetaRepoResolutionError,
    is_meta_repo,
)
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_STATE_SUBDIR = "state"

#: Review: code-reviewer — duplicated from the C3 shim's
#: `RESOLUTION_RESOLVED_ENGINE` module-level string constant
#: (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`) rather than loading
#: the shim just to read one string, mirroring the SAME duplication pattern
#: `coordinator_core.engine_root` already uses for
#: `_RESOLUTION_LIVE_WORKING_TREE_LITERAL` (see that module's comment for the
#: full rationale). Per the shim's own docstring, this string is "part of the
#: contract, not just its name" — if the shim's constant value ever changes,
#: this one must change with it.
_RESOLUTION_RESOLVED_ENGINE_LITERAL = "resolved-engine"


class StateRootError(RuntimeError):
    """Rc-1 shape: a state-root resolution failure (bad flags, unresolvable root)."""


class CrossCuttingStateRoot(StateRootError):
    """Rc-2 shape: Rule 3 artifact classified cross-cutting; human routing required.

    Subclasses StateRootError so a caller that only distinguishes success/failure
    still treats it as a failure, while a caller that wants the bash oracle's
    exit-2 semantics can catch this specifically.
    """

    def __init__(self, artifact: str, message: str):
        super().__init__(message)
        self.artifact = artifact
        self.message = message


def _state_of(root: str) -> str:
    """Append the ``state`` subdir to a resolved root, matching the bash oracle's
    ``printf '%s/state'`` — os.path.join keeps this correct on Windows roots."""
    return os.path.join(root, _STATE_SUBDIR)


def _doe_state() -> str:
    """Rule 1 helper: DoE doctrine state root. Fail-loud; no claude-klabauter fallback."""
    doe = coordinator_doe_root()
    if not doe:
        raise StateRootError(
            "coordinator_state_root: cannot resolve DoE doctrine root — "
            "repos.doe_claude is not set. Does NOT fall back to claude-klabauter for the "
            "doctrine subject. Remediate: machine-local set repos.doe_claude "
            "<path>, or re-run /coordinator:install."
        )
    return _state_of(doe)


def _claude_klabauter_state() -> str:
    """Rule 2/4/5 helper: claude-klabauter engine state root. Raises StateRootError on
    failure OR when the resolved claude-klabauter root is a published engine mirror
    (`RESOLUTION_RESOLVED_ENGINE`) rather than a live working tree — see this
    module's docstring, "Published-mirror guard". Uses
    ``coordinator_engine_root_with_class()`` (not the class-less
    ``coordinator_engine_root()``) specifically so this check is possible;
    the ``RESOLUTION_LIVE_WORKING_TREE`` path below returns byte-identical to
    the prior class-less resolution.

    An ``unverified-env`` class means the resolver took its free environment
    rung and did not establish which tree the path is (see
    ``engine_root._RESOLUTION_UNVERIFIED_ENV_LITERAL``). This is a state
    WRITE, so it pays ``classify_env_resolved_root()`` to find out rather
    than inheriting the rung's convenient assumption — that assumption is
    what let writers file into the mirror while this guard read as armed."""
    try:
        claude_klabauter_root, resolution_class = coordinator_engine_root_with_class()
    except RuntimeError as exc:
        raise StateRootError(str(exc)) from exc
    if resolution_class == _RESOLUTION_UNVERIFIED_ENV_LITERAL:
        resolution_class = classify_env_resolved_root(claude_klabauter_root)
    if resolution_class == _RESOLUTION_RESOLVED_ENGINE_LITERAL:
        raise StateRootError(
            "coordinator_state_root: engine-subject state resolved to a "
            f"PUBLISHED engine mirror ('{claude_klabauter_root}'), not a live working "
            "tree — refusing to write state into a published mirror. This "
            "would leak runtime/session artifacts (e.g. operator-machine "
            "identifiers) into a public repo and can strand or split a state "
            "corpus. Remediate: run from a live working-tree checkout of "
            "claude-klabauter (e.g. unset any published-mirror registration "
            "for this session), or set CLAUDE_KLABAUTER_ROOT explicitly to a working "
            "tree."
        )
    return _state_of(claude_klabauter_root)


def _resolve_git_root(git_root: Optional[str] = None) -> str:
    """Resolve cwd's git root for Rule 5, or return an explicitly-supplied
    ``git_root`` unchanged (skips the subprocess round-trip entirely for a
    caller that already resolved its own repo root — see
    ``coordinator_state_root``'s ``git_root`` parameter). Fail-loud
    (StateRootError) when empty or unresolvable — never silently pick a
    branch (detect-then-fail-loud)."""
    if git_root:
        return git_root
    resolved = _repo_root_seam.show_toplevel()
    if not resolved:
        raise StateRootError(
            "coordinator_state_root: git rev-parse --show-toplevel failed or "
            "returned empty — not inside a git repository. Remediation: run from "
            "within a git repository, or pass central=True for central state that "
            "is not repo-scoped."
        )
    return resolved


def coordinator_state_root(
    central: bool = False,
    subject: Optional[str] = None,
    artifact: Optional[str] = None,
    git_root: Optional[str] = None,
) -> str:
    """Resolve the coordinator state-root path via the 5-rule dispatch.

    Returns the resolved ``<root>/state`` path. Raises StateRootError on failure
    (the bash oracle's rc-1) or CrossCuttingStateRoot on a cross-cutting artifact
    (Rule 3, the bash oracle's rc-2).

    Review: code-reviewer — ``git_root`` (Rule 5 only; ignored when
    ``central=True``) lets a caller that has already resolved its own repo
    root (e.g. a subprocess-spawn boundary that would otherwise need a
    process-global ``os.chdir`` to make Rule 5's cwd-based git-toplevel
    resolution see the right tree) pass it through explicitly instead.
    Mirrors ``coordinator_core.meta_repo_identity.is_meta_repo``'s own
    optional ``git_root`` parameter.
    """
    # Argument validation (mirrors the bash oracle).
    if subject is not None and artifact is not None:
        raise StateRootError(
            "coordinator_state_root: subject and artifact are mutually exclusive; "
            "specify at most one"
        )

    if central:
        # Rule 1 / Rule 2: explicit subject override.
        if subject is not None:
            if subject == "doctrine":
                return _doe_state()  # Rule 1
            if subject == "engine":
                return _claude_klabauter_state()  # Rule 2
            raise StateRootError(
                f"coordinator_state_root: unknown subject value '{subject}'; "
                "expected 'engine' or 'doctrine'"
            )

        # Rule 3: artifact-subject classification routes to the appropriate plane.
        if artifact is not None:
            classified = classify(artifact)
            if classified == Subject.DOCTRINE:
                return _doe_state()  # -> Rule 1
            if classified == Subject.ENGINE:
                return _claude_klabauter_state()  # -> Rule 2
            # Subject.CROSS_CUTTING: fail-loud, preserve exit-2 semantics.
            raise CrossCuttingStateRoot(artifact, remediation_message(artifact))

        # Rule 4: central with no subject and no artifact. BACKWARD-COMPAT DEFAULT.
        return _claude_klabauter_state()

    # Rule 5: default branch (central=False). BACKWARD-COMPAT DEFAULT.
    # Call _resolve_git_root() zero-arg when no override is supplied (matches
    # its long-standing zero-arg call shape byte-for-byte) and only pass
    # git_root through when a caller actually supplied one.
    resolved_git_root = _resolve_git_root(git_root) if git_root else _resolve_git_root()
    try:
        meta = is_meta_repo(resolved_git_root)
    except MetaRepoResolutionError as exc:
        raise StateRootError(str(exc)) from exc

    if meta:
        # Meta-repo -> central state is in claude-klabauter.
        return _claude_klabauter_state()
    # Sibling repo -> per-repo state stays in the repo itself, UNLESS this
    # sibling IS the registered published-engine mirror clone (see this
    # module's docstring, "Published-mirror guard") — reuses the exact same
    # `repos.claude_klabauter` discriminator `_claude_klabauter_state()` already
    # applies via `coordinator_engine_root_with_class`, exposed standalone as
    # `published_engine_mirror_path()` so this branch can ask the question
    # for an arbitrary candidate path rather than only for "my own" root.
    _mirror = published_engine_mirror_path()
    # realpath (not normpath) so a registry value and a git-toplevel-resolved
    # path that differ only by an unresolved symlink component (e.g. macOS
    # /var -> /private/var) still compare equal -- normpath alone would
    # under-fire on exactly that class of path.
    if _mirror and os.path.realpath(_mirror) == os.path.realpath(resolved_git_root):
        raise StateRootError(
            "coordinator_state_root: cwd's git root "
            f"('{resolved_git_root}') is the PUBLISHED engine mirror "
            "(repos.claude_klabauter) — refusing to treat it as a per-repo "
            "state root. This would leak runtime/session artifacts (e.g. "
            "operator-machine identifiers) into a public repo and can strand "
            "or split a state corpus. Remediate: run from a live working-tree "
            "checkout instead of the published mirror clone."
        )
    return _state_of(resolved_git_root)


def coordinator_state_root_central() -> str:
    """Shared ``coordinator_state_root(central=True)`` wrapper, folding
    ``StateRootError`` to ``""``.

    Review: code-reviewer — previously hand-duplicated verbatim across
    ``coordinator_core.ops.central_run_due`` and
    ``coordinator_core.ops.learn_lessons_roots`` (Rule 4 -- no subject/artifact
    given, so it resolves to ``<coordinator_engine_root()>/state``, matching
    the retired bash oracle's ``coordinator-state-root.sh --central``
    default). Centralized here so both callers share one definition instead of
    two independently-maintained copies -- the same C11 centralization
    principle this porting wave already applied to
    ``resolve_coordinator_clone.resolve_content_root()``.

    Returns "" on any failure -- callers that shelled out to
    ``coordinator-state-root.sh --central`` folded a failed resolution into an
    unconditional string-concat rather than checking exit code; this
    preserves that contract exactly.
    """
    try:
        return coordinator_state_root(central=True)
    except StateRootError:
        return ""


def print_map() -> str:
    """Return the central map as a single-line JSON object (no trailing newline),
    mirroring ``coordinator_state_root --print-map``.

    Unresolvable subjects emit JSON null (not a hard error); one WARN line per
    unresolvable subject is written to stderr. Always returns a string (rc 0
    semantics).
    """
    subjects: dict = {}

    # Doctrine root. On failure: null + one stderr WARN line, continue.
    doe = coordinator_doe_root()
    if doe:
        subjects["doctrine"] = _state_of(doe)
    else:
        sys.stderr.write(
            "coordinator_state_root --print-map: doctrine root unresolvable — "
            "WARN+skip semantics apply\n"
        )
        subjects["doctrine"] = None

    # Engine root. Routed through the class-aware resolver so the printed
    # map reflects the same published-mirror guard `_claude_klabauter_state()` applies
    # (Rule 2/4/5) — the class-less `coordinator_engine_root()` would happily
    # report a mirror path that the resolver itself refuses to hand out for
    # writing, which is worse than no diagnostic. On failure OR on a resolved
    # published-mirror class: null + one stderr WARN line, continue.
    # Review: code-reviewer.
    try:
        engine_root, resolution_class = coordinator_engine_root_with_class()
        if resolution_class == _RESOLUTION_UNVERIFIED_ENV_LITERAL:
            resolution_class = classify_env_resolved_root(engine_root)
        if resolution_class == _RESOLUTION_RESOLVED_ENGINE_LITERAL:
            sys.stderr.write(
                "coordinator_state_root --print-map: engine root resolved to a "
                "PUBLISHED engine mirror — refusing to report it as the state "
                "map's engine root (state is never written into a published "
                "mirror); WARN+skip semantics apply\n"
            )
            subjects["engine"] = None
        else:
            subjects["engine"] = _state_of(engine_root)
    except RuntimeError:
        sys.stderr.write(
            "coordinator_state_root --print-map: engine root unresolvable — "
            "WARN+skip semantics apply\n"
        )
        subjects["engine"] = None

    return json.dumps(
        {"schema": "coordinator-state-root-map/v1", "subjects": subjects},
        separators=(",", ":"),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI-shaped wrapper preserving the bash oracle's exit codes for parity:
    prints the resolved path to stdout (no trailing newline) and returns 0; or
    writes remediation to stderr and returns 1 (StateRootError) / 2
    (CrossCuttingStateRoot). ``--print-map`` prints the JSON map and returns 0."""
    args = list(sys.argv[1:] if argv is None else argv)

    central = False
    subject: Optional[str] = None
    artifact: Optional[str] = None
    want_print_map = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--central":
            central = True
            i += 1
        elif arg == "--subject":
            if i + 1 >= len(args) or not args[i + 1]:
                sys.stderr.write(
                    "coordinator_state_root: --subject requires an argument: "
                    "engine or doctrine\n"
                )
                return 1
            subject = args[i + 1]
            i += 2
        elif arg == "--artifact":
            if i + 1 >= len(args) or not args[i + 1]:
                sys.stderr.write(
                    "coordinator_state_root: --artifact requires an argument: "
                    "an artifact path\n"
                )
                return 1
            artifact = args[i + 1]
            i += 2
        elif arg == "--print-map":
            want_print_map = True
            i += 1
        else:
            sys.stderr.write(
                f"coordinator_state_root: unknown flag '{arg}'; valid flags: "
                "--central, --subject, --artifact, --print-map\n"
            )
            return 1

    if want_print_map:
        if subject is not None or artifact is not None:
            sys.stderr.write(
                "coordinator_state_root: --print-map cannot be combined with "
                "--subject/--artifact\n"
            )
            return 1
        sys.stdout.write(print_map())
        return 0

    try:
        path = coordinator_state_root(central=central, subject=subject, artifact=artifact)
    except CrossCuttingStateRoot as exc:
        sys.stderr.write(exc.message + "\n")
        return 2
    except StateRootError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1

    sys.stdout.write(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
