"""
coordinator_core.workday_complete.cockpit_contract_freshness — computes the
`gates.cockpit_contract_freshness` OBSERVABILITY row `brief.py` emits on
EVERY `/workday-complete` run, in every repo (not just claude-klabauter's).

Purpose: DoE's ceremony Step 10 Final Summary line surfaces whether the
`cockpit-contract-release` tag (the published pin every repo re-vendors from)
is FRESH against the newest commit touching DoE's
`coordinator/cockpit-contract/schema/` in the local DoE clone. This row
GATES NOTHING and triggers no publish — it is read-only signal.

Verdict semantics:
    FRESH     published peel IS the newest commit touching the schema dir.
    STALE     candidate is a STRICT DESCENDANT of the published peel (a
              schema change has landed since the last publish).
    DIVERGED  any other relationship (behind, unrelated, or diverged
              histories) — a human must reconcile.
    UNKNOWN   could not determine (no DoE clone on this machine, network
              unavailable, unexpected error). NOT an error — the common
              case on a consumer machine with no DoE clone.

Cost contract (load-bearing — this runs in every repo's daily ceremony on
every machine): the DoE clone root is resolved LOCALLY FIRST, with ZERO
network calls (`_resolve_doe_root_local`, reusing
`coordinator_core.ops.emit.doe_drift.resolve_doe_clone`'s bootstrap-safe
direct-TOML-read registry ladder — no `machine-local` CLI subprocess, no
`__file__`/git-toplevel self-location). Only when a DoE clone resolves
locally does this module ever shell out, and then exactly ONE bounded
`git ls-remote` with an explicit small timeout
(`_LS_REMOTE_TIMEOUT_SECONDS`). Every subsequent git call in the happy path
is LOCAL (peel, candidate-commit lookup, contract-version read, ancestry
check) — no further network.

Candidate-scope negative-spec (Review: code-reviewer F1 — candidate was
implicitly scoped to bare `HEAD`, so the verdict depended on whichever
branch the DoE clone happened to be checked out to):
    - The candidate query is explicitly scoped to the DoE clone's current
      `HEAD` — never a hardcoded canonical ref like `origin/main`. This is a
      deliberate choice, not an oversight: as of this writing NO
      cockpit-contract schema commit has ever reached DoE's `origin/main`
      (`docs/wiki/cockpit-contract-revendor.md` — DoE's schema work lives on
      a work branch, 4 minors ahead of `origin/main`), so scoping to
      `origin/main` would report STALE/DIVERGED forever and make the gate
      permanently useless.
    - Because "whatever HEAD happens to be" is an honest but unstable
      measurement, the emitted `candidate.resolved_from_ref` field names
      exactly what was measured (the branch name, or `"HEAD (detached)"`)
      so a consumer can see the scope the verdict depends on rather than
      inferring it.
    - Does NOT reimplement `coordinator/bin/lib/coordinator_registry.py`'s
      `doe_root()` — that helper's module-level import has its own
      side-effecting manifest load (and, on the split-repo layout, an
      unconditional `machine-local` CLI subprocess probe at IMPORT time) and
      calls `sys.exit(2)` on failure, neither of which this read-only,
      never-exiting, zero-network-until-resolved probe may inherit. This
      module reuses `doe_drift.resolve_doe_clone()` instead — the same
      three-tier ladder shape (env override -> machine-local registry),
      already proven bootstrap-safe (no subprocess) by its own module
      contract.
    - Does NOT read claude-klabauter's own
      `coordinator_core/contract/cockpit_schema/emit_schema.py` literal for
      any version number — that is what claude-klabauter emits NEXT, not what DoE has
      published. Both `published.contract_version` and
      `candidate.contract_version` are read from the DoE clone's own git
      history via `git show <sha>:coordinator/cockpit-contract/schema/
      cockpit-contract.schema.json`.
    - Every git call here targets the DoE clone, which is a DIFFERENT
      repository from the one this ceremony is running in. `git -C` alone does
      NOT scope to it — an inherited `GIT_DIR` (git exports one to every hook
      it runs, often as a relative `"."`) still wins over discovery, and the
      probe then computes a FRESH/STALE/DIVERGED verdict against the LOCAL
      repo while every emitted field names DoE. Every call therefore runs with
      `coordinator_core.git_scope.scoped_git_env()`, and `_compute` gates the
      whole probe on `foreign_repo_unusable_reason(doe_root)` — which also
      confirms the resolved git dir lies inside the DoE tree — so an
      unanswerable probe degrades to UNKNOWN instead of emitting a confident
      DIVERGED about somebody else's history. See `coordinator_core/git_scope.py`
      for the 2026-08-03 incident that makes this a correctness defect.
    - Never raises. `compute_cockpit_contract_freshness()` is wrapped so
      that no exception of any kind escapes it — an unexpected failure
      degrades to `UNKNOWN` with the exception summary in `reason`. A broken
      freshness probe must never break the daily ceremony in any repo.

Spec backlink: DoE-claude ceremony Step 10 Final Summary line (2026-07-25 ask)
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from coordinator_core.git_scope import (
    PROBE_UNKNOWN,
    PROBE_YES,
    foreign_repo_unusable_reason,
    git_predicate,
    scoped_git_env,
)
from coordinator_core.ops.emit import doe_drift

# Bounded ls-remote timeout (seconds) — the ONE network call this module ever
# makes, and only when a DoE clone resolved locally first. Kept deliberately
# small: this runs on every repo's daily ceremony on every machine.
_LS_REMOTE_TIMEOUT_SECONDS = 5

# Every other subprocess this module runs is a LOCAL git call against the
# already-resolved DoE clone (peel, log, show, merge-base) — bounded the same
# way for symmetry, though none of them touch the network.
#
# Review: code-reviewer (F6, nit) — per-call timeouts bound each hop but not
# the total. Worst case the happy/degraded path chains up to 5 sequential
# calls (1x _LS_REMOTE_TIMEOUT_SECONDS + up to 4x _LOCAL_GIT_TIMEOUT_SECONDS),
# ~45s worst case. Not wired to an overall wall-clock budget today — worth
# revisiting with a budget wrapping `_compute` if this ever becomes a real
# complaint in practice; per-call timeouts already guarantee it can never
# hang indefinitely.
_LOCAL_GIT_TIMEOUT_SECONDS = 10

_RELEASE_REF = "refs/tags/cockpit-contract-release"
_SCHEMA_DIR_RELPATH = "coordinator/cockpit-contract/schema"
_SCHEMA_FILE_RELPATH = f"{_SCHEMA_DIR_RELPATH}/cockpit-contract.schema.json"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _FreshnessProbeError(Exception):
    """Internal signal carrying an operator-readable reason string. Never
    escapes `compute_cockpit_contract_freshness()` — every raise site here is
    caught by its immediate caller and folded into an UNKNOWN entry."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _entry(
    verdict: str,
    checked_at: str,
    reason: str,
    remediation: Optional[str],
    *,
    published_peel: Optional[str] = None,
    published_version: Optional[str] = None,
    candidate_sha: Optional[str] = None,
    candidate_version: Optional[str] = None,
    candidate_ref: Optional[str] = None,
) -> dict[str, Any]:
    # Review: code-reviewer (F4) — the trailing same-typed (Optional[str])
    # args are keyword-only so a transposed call fails loudly (TypeError) at
    # the call site instead of type-checking and silently swapping data in
    # the emitted envelope.
    return {
        "verdict": verdict,
        "checked_at": checked_at,
        "reason": reason,
        "remediation": remediation,
        "published": {
            "ref": _RELEASE_REF,
            "peel": published_peel,
            "contract_version": published_version,
        },
        "candidate": {
            "sha": candidate_sha,
            "contract_version": candidate_version,
            # Review: code-reviewer (F1) — names exactly what the candidate
            # query was scoped to (see module candidate-scope negative-spec),
            # additive field, agreed FRESH/STALE/DIVERGED/UNKNOWN shape
            # unchanged.
            "resolved_from_ref": candidate_ref,
        },
    }


def _unknown(checked_at: str, reason: str) -> dict[str, Any]:
    return _entry("UNKNOWN", checked_at, reason, None)


# ---------------------------------------------------------------------------
# Step 1 — LOCAL-ONLY root resolution (no network, no CLI subprocess).
# ---------------------------------------------------------------------------

def _resolve_doe_root_local() -> Optional[Path]:
    """DOE_ROOT env -> REPO_DOE_CLAUDE env -> machine-local `repos.doe_claude`
    (direct TOML read via `doe_drift.resolve_doe_clone`, bootstrap-safe: no
    subprocess).

    Precedence is strict and NON-FALLTHROUGH: an explicit env override is a
    DIRECTIVE, not a hint.
        1. DOE_ROOT set (non-empty after `.strip()`) -> that is the answer.
           If it does not resolve to an existing directory, resolution FAILS
           (raises `_FreshnessProbeError` naming the var and the bad path) —
           REPO_DOE_CLAUDE and the registry are NEVER consulted.
        2. Else REPO_DOE_CLAUDE set (non-empty) -> same rule: wins outright,
           or fails hard without falling through to the registry.
        3. Else -> the machine-local registry (current behaviour, unchanged).
    An empty/whitespace-only value is treated as UNSET, not as a directive.

    Returns None — never raises for this case — only when BOTH env vars are
    unset and the registry itself can't resolve a clone; that is the common,
    cheap, expected path on a consumer machine with no DoE clone. Raises
    `_FreshnessProbeError` (caught by the caller, folded into UNKNOWN) when an
    env override is set but bad — never silently substitutes a different
    clone for the one the operator named.
    """
    for env_name in ("DOE_ROOT", "REPO_DOE_CLAUDE"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                return candidate
            raise _FreshnessProbeError(
                f"{env_name} is set to {raw!r} but that is not an existing "
                "directory — an explicit env override is a directive, not a "
                "hint, so resolution fails here rather than falling through "
                "to REPO_DOE_CLAUDE or the machine-local registry"
            )
    try:
        return doe_drift.resolve_doe_clone()
    except doe_drift.DoeResolveError:
        return None


# ---------------------------------------------------------------------------
# Step 2/3 — ONE bounded network probe, then a LOCAL peel.
# ---------------------------------------------------------------------------

def _ls_remote_release_tag(doe_root: Path) -> str:
    """One bounded `git ls-remote origin refs/tags/cockpit-contract-release`
    against the DoE clone. Returns the raw ls-remote SHA (a TAG-OBJECT sha
    for an annotated tag — callers must peel before using it as a commit).

    Raises `_FreshnessProbeError` on timeout, non-zero exit, missing git, or
    an absent ref (not published yet) — never raises anything else, never
    hangs past `_LS_REMOTE_TIMEOUT_SECONDS`.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(doe_root), "ls-remote", "origin", _RELEASE_REF],
            capture_output=True,
            text=True,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise _FreshnessProbeError(
            f"git ls-remote timed out after {_LS_REMOTE_TIMEOUT_SECONDS}s "
            "probing DoE origin for cockpit-contract-release"
        ) from exc
    except OSError as exc:
        raise _FreshnessProbeError(
            f"git ls-remote could not run against the DoE clone: {exc}"
        ) from exc

    if result.returncode != 0:
        raise _FreshnessProbeError(
            "git ls-remote returned exit "
            f"{result.returncode} probing DoE origin: {result.stderr.strip()}"
        )

    output = result.stdout.strip()
    if not output:
        raise _FreshnessProbeError(
            f"'{_RELEASE_REF}' is not published yet on the DoE origin"
        )
    return output.split()[0]


def _peel_to_commit(doe_root: Path, sha: str) -> str:
    """`git rev-parse <sha>^{commit}` — ls-remote returns the TAG-OBJECT sha
    for an annotated tag, and `merge-base` exits 128 ("Not a commit") on a
    tag object, silently breaking the ancestry test downstream. Peel first,
    always, before any ancestry comparison.

    Review: code-reviewer (F2) — if the tag object `ls-remote` reported isn't
    yet in the DoE clone's local object database (the ordinary state right
    after a tag is cut, before the next fetch), `rev-parse` exits non-zero.
    Mirroring `doe_drift.probe_freshness_ref`'s documented graceful fallback
    (`coordinator_core/ops/emit/doe_drift.py:359`), this returns the raw sha
    unchanged rather than degrading the whole probe to UNKNOWN. That raw sha
    can still reach `_is_ancestor` as a tag-object sha, where `merge-base`
    exits 128 ("Not a commit") — `_is_ancestor` treats that as indeterminate
    (None), which the caller folds into DIVERGED, never a crash. A hard
    subprocess failure (timeout, missing git) is a different, harder failure
    than "tag object absent" and still raises, degrading the whole probe to
    UNKNOWN.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(doe_root), "rev-parse", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _FreshnessProbeError(
            f"could not peel published ref {sha} to a commit: {exc}"
        ) from exc

    if result.returncode != 0:
        return sha
    peeled = result.stdout.strip()
    if not peeled:
        return sha
    return peeled


def _current_ref_label(doe_root: Path) -> str:
    """Human-readable label naming what `_candidate_sha` resolved its answer
    from — the DoE clone's current HEAD, as an explicit branch name when on
    one, or the literal `"HEAD (detached)"` otherwise. Never raises; folds
    any resolution failure into a literal `"HEAD (unresolvable)"` label
    rather than aborting the probe, since this is a best-effort annotation,
    not load-bearing for the verdict computation itself.

    Surfaced in the emitted entry's `candidate.resolved_from_ref` field — see
    the module's candidate-scope negative-spec.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(doe_root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "HEAD (unresolvable)"
    branch = result.stdout.strip()
    if result.returncode == 0 and branch:
        return branch
    return "HEAD (detached)"


# ---------------------------------------------------------------------------
# Step 4/5 — LOCAL reads only (candidate commit, contract version, ancestry).
# ---------------------------------------------------------------------------

def _candidate_sha(doe_root: Path) -> str:
    """Newest commit touching DoE's `coordinator/cockpit-contract/schema/`,
    reachable from the DoE clone's current `HEAD` — explicitly, not by the
    absence of a ref argument. See the module's candidate-scope negative-spec
    for why `HEAD` (not a hardcoded canonical ref) is the deliberate choice
    here, and `_current_ref_label` for how that scope is surfaced to
    consumers of the emitted entry."""
    try:
        result = subprocess.run(
            ["git", "-C", str(doe_root), "log", "-1", "--format=%H", "HEAD",
             "--", _SCHEMA_DIR_RELPATH],
            capture_output=True,
            text=True,
            timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _FreshnessProbeError(
            f"could not resolve the newest commit touching {_SCHEMA_DIR_RELPATH}: {exc}"
        ) from exc

    if result.returncode != 0:
        raise _FreshnessProbeError(
            f"git log failed resolving the candidate commit: {result.stderr.strip()}"
        )
    sha = result.stdout.strip()
    if not sha:
        raise _FreshnessProbeError(
            f"no commit found touching {_SCHEMA_DIR_RELPATH} in the DoE clone"
        )
    return sha


def _contract_version_at(doe_root: Path, sha: str) -> Optional[str]:
    """Top-level `"version"` key of `cockpit-contract.schema.json` AT `sha`
    (never claude-klabauter's own emitter literal — see module negative-spec)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(doe_root), "show", f"{sha}:{_SCHEMA_FILE_RELPATH}"],
            capture_output=True,
            text=True,
            timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
            check=False,
            env=scoped_git_env(),
            creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _FreshnessProbeError(
            f"could not read {_SCHEMA_FILE_RELPATH} at {sha}: {exc}"
        ) from exc

    if result.returncode != 0:
        raise _FreshnessProbeError(
            f"git show failed reading {_SCHEMA_FILE_RELPATH} at {sha}: "
            f"{result.stderr.strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise _FreshnessProbeError(
            f"{_SCHEMA_FILE_RELPATH} at {sha} is not valid JSON: {exc}"
        ) from exc
    version = data.get("version")
    return str(version) if version is not None else None


def _is_ancestor(doe_root: Path, ancestor_sha: str, descendant_sha: str) -> Optional[bool]:
    """`git merge-base --is-ancestor ancestor_sha descendant_sha`.

    Returns True/False on a definite answer, None when indeterminate (git
    error, missing objects, timeout) — the caller treats None as DIVERGED,
    never as a crash.

    Delegates the exit-code mapping to `git_scope.git_predicate` so the 0/1/other
    tri-state (and the stripped repo-scoping environment that makes `-C` actually
    scope to the DoE clone) is the shared implementation rather than a local
    re-derivation.
    """
    verdict, _reason = git_predicate(
        doe_root,
        ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
        timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if verdict == PROBE_UNKNOWN:
        return None
    return verdict == PROBE_YES


def _doe_clone_unusable_reason(doe_root: Path) -> Optional[str]:
    """None when `doe_root` is readable AS the DoE clone, else why it is not.

    A thin named seam over `git_scope.foreign_repo_unusable_reason` — same
    module-function stubbing convention every other git hop in this file
    follows, so the test suite can exercise the verdict logic against a
    synthetic root without also having to materialise a real clone.

    Runs only AFTER a root resolved, so the module's zero-shell-out
    consumer-machine cost contract is unaffected.
    """
    return foreign_repo_unusable_reason(doe_root)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _compute(checked_at: str) -> dict[str, Any]:
    try:
        doe_root = _resolve_doe_root_local()
    except _FreshnessProbeError as exc:
        return _unknown(checked_at, str(exc))
    if doe_root is None:
        return _unknown(
            checked_at,
            "no DoE clone resolvable on this machine (checked DOE_ROOT, "
            "REPO_DOE_CLAUDE, and the machine-local repos.doe_claude "
            "registry) — freshness check skipped; this is the expected, "
            "zero-network path on a consumer machine with no DoE clone",
        )

    # Gate the whole probe on the DoE clone actually being readable AS the DoE
    # clone. Without this, an inherited GIT_DIR (or a .git file pointing
    # elsewhere) lets every call below succeed against the WRONG repository and
    # emit a confident FRESH/STALE/DIVERGED verdict labelled with DoE's path.
    # Un-answerable is its own outcome — see the module's git-scoping
    # negative-spec and `coordinator_core/git_scope.py`.
    unusable = _doe_clone_unusable_reason(doe_root)
    if unusable is not None:
        return _unknown(
            checked_at,
            f"the DoE clone at {doe_root} could not be read as a git repository "
            f"({unusable}) — freshness could not be determined; this is NOT a "
            "claim that the published tag is stale or diverged",
        )

    try:
        raw_sha = _ls_remote_release_tag(doe_root)
    except _FreshnessProbeError as exc:
        return _unknown(checked_at, str(exc))

    try:
        published_peel = _peel_to_commit(doe_root, raw_sha)
    except _FreshnessProbeError as exc:
        return _unknown(checked_at, str(exc))

    try:
        candidate_sha = _candidate_sha(doe_root)
    except _FreshnessProbeError as exc:
        return _entry(
            "UNKNOWN", checked_at, str(exc), None,
            published_peel=published_peel,
        )

    # Review: code-reviewer (F1) — best-effort annotation naming the scope
    # `_candidate_sha` measured; never raises, so a resolution failure here
    # must not abort a verdict the SHA comparison below can still determine.
    candidate_ref: Optional[str] = None
    try:
        candidate_ref = _current_ref_label(doe_root)
    except Exception:  # noqa: BLE001 - best-effort annotation, never load-bearing
        candidate_ref = "HEAD (unresolvable)"

    # Contract-version annotations are best-effort — a read failure here
    # narrows the entry's usefulness but must not abort a verdict the SHA
    # comparison below can still determine.
    published_version: Optional[str] = None
    candidate_version: Optional[str] = None
    try:
        published_version = _contract_version_at(doe_root, published_peel)
    except _FreshnessProbeError:
        pass

    if published_peel == candidate_sha:
        # Review: code-reviewer (F5) — same sha, so re-reading the same blob
        # via a second identical `git show` would be a redundant subprocess
        # call; reuse the value already read above.
        candidate_version = published_version
        return _entry(
            "FRESH",
            checked_at,
            "the published cockpit-contract-release peel IS the newest "
            f"commit touching {_SCHEMA_DIR_RELPATH} — nothing to publish",
            None,
            published_peel=published_peel,
            published_version=published_version,
            candidate_sha=candidate_sha,
            candidate_version=candidate_version,
            candidate_ref=candidate_ref,
        )

    try:
        candidate_version = _contract_version_at(doe_root, candidate_sha)
    except _FreshnessProbeError:
        pass

    is_descendant = _is_ancestor(doe_root, published_peel, candidate_sha)
    if is_descendant is True:
        return _entry(
            "STALE",
            checked_at,
            "candidate is a strict descendant of the published peel — a "
            f"schema change has landed under {_SCHEMA_DIR_RELPATH} since the "
            "last cockpit-contract-release publish",
            "run the cockpit-contract-release publish directive to advance "
            "the tag to the candidate commit",
            published_peel=published_peel,
            published_version=published_version,
            candidate_sha=candidate_sha,
            candidate_version=candidate_version,
            candidate_ref=candidate_ref,
        )

    return _entry(
        "DIVERGED",
        checked_at,
        "candidate is NOT a strict descendant of the published peel (behind, "
        "unrelated, or diverged history) — human required to reconcile",
        "a human must reconcile the DoE clone's "
        f"{_SCHEMA_DIR_RELPATH} history against the published "
        "cockpit-contract-release tag before the next publish",
        published_peel=published_peel,
        published_version=published_version,
        candidate_sha=candidate_sha,
        candidate_version=candidate_version,
        candidate_ref=candidate_ref,
    )


def compute_cockpit_contract_freshness() -> dict[str, Any]:
    """Compute the `cockpit_contract_freshness` gate entry. Never raises —
    any unexpected failure anywhere in the probe degrades to UNKNOWN with the
    exception summary in `reason`, per this module's negative-spec."""
    checked_at = _now_iso()
    try:
        return _compute(checked_at)
    except Exception as exc:  # noqa: BLE001 - a broken probe must never break the ceremony
        return _unknown(
            checked_at,
            f"unexpected error computing cockpit-contract freshness: {exc}",
        )
