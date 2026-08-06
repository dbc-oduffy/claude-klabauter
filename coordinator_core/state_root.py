"""
coordinator_core.state_root — canonical state-root seam resolver.

Purpose: single entry point for resolving the coordinator state directory root.
Encodes the ratified state-placement taxonomy and example-doctrine-repo/claude-klabauter plane routing so
every state-writing caller resolves a root through one seam instead of
open-coding a root variable.

Port of: coordinator-state-root.sh (example-doctrine-repo 6fb5fb37, 2026-07-22).

COMPOSED — this module does NOT reimplement the four sibling resolver ladders. It
dispatches the 5-rule state-root routing on top of the already-native peers:
  - coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()  (Optional[str])
  - coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root()            (str, raises)
  - coordinator_core.artifact_subject.classify()                     (engine|doctrine|cross-cutting)
  - coordinator_core.meta_repo_identity.is_meta_repo()               (bool, raises)

Spec backlinks:
  docs/plans/2026-07-03-stop-the-rot-claude-klabauter-state-home-placement.md § C2 / AC2
  docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.3
  docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (de-bash W2)

Five routing rules (verbatim from the bash oracle's header):

  Rule 1  central=True, subject="doctrine"
            -> <coordinator_doe_root()>/state
            Fail-loud (StateRootError) if the example-doctrine-repo root cannot resolve. Does NOT
            fall back to claude-klabauter.

  Rule 2  central=True, subject="engine"
            -> <coordinator_claude_klabauter_root()>/state

  Rule 3  central=True, artifact=<path>
            -> classify(<path>); map result:
                 doctrine      -> example-doctrine-repo state   (Rule 1)
                 engine        -> claude-klabauter state (Rule 2)
                 cross-cutting -> fail-loud: raise CrossCuttingStateRoot (rc 2),
                                  do NOT return a state path.

  Rule 4  central=True, no subject, no artifact  [BACKWARD-COMPAT DEFAULT]
            -> <coordinator_claude_klabauter_root()>/state
            Every existing --central caller resolves to claude-klabauter, unchanged.

  Rule 5  central=False  [BACKWARD-COMPAT DEFAULT]
            -> <coordinator_claude_klabauter_root()>/state  when cwd git root IS the meta-repo
               <git_root>/state                   when cwd git root is a sibling repo
            Fail-loud on unresolvable git root.

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
    - Does NOT fall back to claude-klabauter when doctrine example-doctrine-repo root fails (Rule 1).
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
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core.meta_repo_identity import (
    MetaRepoResolutionError,
    is_meta_repo,
)
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_STATE_SUBDIR = "state"


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
    """Rule 1 helper: example-doctrine-repo doctrine state root. Fail-loud; no claude-klabauter fallback."""
    doe = coordinator_doe_root()
    if not doe:
        raise StateRootError(
            "coordinator_state_root: cannot resolve example-doctrine-repo doctrine root — "
            "repos.example_doctrine_repo is not set. Does NOT fall back to claude-klabauter for the "
            "doctrine subject. Remediate: machine-local set repos.example_doctrine_repo "
            "/path/to/example-doctrine-repo, or re-run /coordinator:install."
        )
    return _state_of(doe)


def _claude_klabauter_state() -> str:
    """Rule 2/4 helper: claude-klabauter engine state root. Raises StateRootError on failure."""
    try:
        claude-klabauter = coordinator_claude_klabauter_root()
    except RuntimeError as exc:
        raise StateRootError(str(exc)) from exc
    return _state_of(claude-klabauter)


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
    # Sibling repo -> per-repo state stays in the repo itself.
    return _state_of(resolved_git_root)


def coordinator_state_root_central() -> str:
    """Shared ``coordinator_state_root(central=True)`` wrapper, folding
    ``StateRootError`` to ``""``.

    Review: code-reviewer — previously hand-duplicated verbatim across
    ``coordinator_core.ops.central_run_due`` and
    ``coordinator_core.ops.learn_lessons_roots`` (Rule 4 -- no subject/artifact
    given, so it resolves to ``<coordinator_claude_klabauter_root()>/state``, matching
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

    # Engine root. On failure: null + one stderr WARN line, continue.
    try:
        claude-klabauter = coordinator_claude_klabauter_root()
        subjects["engine"] = _state_of(claude-klabauter)
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
