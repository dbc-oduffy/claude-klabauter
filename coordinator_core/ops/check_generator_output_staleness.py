"""
coordinator_core.ops.check_generator_output_staleness — the local-repo leg of
the generator-output staleness predicate: STALE iff a declared pair's
`sources` moved after the artifact's own emission stamp.

Purpose: a generator's output can silently stop reflecting its inputs the
same way a manifest can silently stop reflecting its measurements
(`check_import_budget_staleness.py`'s incident) — the difference is the
inputs here are whole generator source trees, not a hand-listed
`measured_paths`. This module answers "did any declared pair's `sources`
move since its artifact was last emitted?" per pair, never once for the
whole repo, using C0's shared since-range comparison
(`coordinator_core.ops.staleness_git`) and C1's discovery
(`coordinator_core.ops.generator_provenance`).

## ONE LEG, DELIBERATELY (AC2)

The verdict is SINGLE-LEG: STALE iff `commits_touching_since(sources, stamp)`
is non-empty. There is no calendar-age threshold and no second conjunct. An
earlier revision of this plan copied `check_import_budget_staleness.py`'s AND
*shape* while making leg 1 ("stamp older than the generator's last-touching
commit") a restatement of leg 2 ("`git log --since=<stamp>` over the
generator path is non-empty") — those are the same fact, so the conjunction
could never fail: a vacuous gate reading as a rigorous one. The damper for
non-semantic generator movement (a comment-only edit, a rename) is
content-idempotence (regenerate and diff bytes) at the ceremony that owns
regeneration, not a calendar threshold and not a content filter here.

## `sources`, never the generator's own path (§ Mechanism correction)

The generators swept by C1 are thin CLI shims whose logic lives in
`coordinator_core/`. A since-range over the shim's own path is empty
essentially forever, so this module always compares against the declared
`Pair.sources` pathspec, never `Pair.generator`.

## Stamp extraction (this module's only leg-specific work)

The artifact's emission stamp is read from the artifact's own CONTENT
(AC3), never from the artifact file's git history: a `.json` artifact's
`stamp_key` is a top-level key; any other artifact is read as Markdown/YAML
frontmatter (a `---`-delimited block at the top of the file) and `stamp_key`
is a top-level key within that block. A missing artifact, an artifact with
no readable stamp, or a `stamp_key` absent from wherever it should live is
UNSTAMPED, never an exception and never silently FRESH.

## AC9 — comparison is C0's, not respelled here

`commits_touching_since` / `verdict_from_range` / `Verdict` are imported from
`coordinator_core.ops.staleness_git`, not reimplemented. This module's stamp
readers are the only leg-specific code; the since-range walk, the
commit-ish-vs-timestamp discrimination, and the regeneration-commit exclusion
all live in C0 and are shared with C6's vendored leg.

## C6 — the vendored leg (AC8/AC9)

`compute_vendored_staleness` answers the same question for a peer
(DoE-claude)-owned generator emitting a peer-owned artifact that claude-klabauter only
CONSUMES — vendored copies read via a resolved peer clone, never written to.
Its leg-specific work is stamp EXTRACTION ONLY (`_extract_vendored_stamp`
reads the nested `x-effective-delivery`-shaped block); the comparison itself
is the SAME `commits_touching_since(..., artifact_path=...)` C3's local leg
uses (AC9), called against the peer repo root with `since_point` set to the
COMMIT-ISH `generated_from_sha` rather than a timestamp.

Three traps this leg exists to close, all verified live at
DoE-claude@a0d10df52 (`coordinator/hooks/hooks.json`'s
`x-effective-delivery` block):

  - **Parent offset.** `generated_from_sha` is the emitter's HEAD *at emit
    time*, which is by construction the PARENT of the commit that ends up
    carrying the block (the block is written before that commit exists).
    `commits_touching_since(sha..HEAD, ...)` already treats `sha` as
    EXCLUSIVE, so a freshly-regenerated artifact whose stamp names its own
    parent reads FRESH, not drifted — no extra offset arithmetic needed
    here.
  - **Dirty tree.** `generated_from_dirty_tree: true` means the SHA is a
    LOWER BOUND on the emitting state, not an identification of it —
    uncommitted emitter edits at emission time are invisible to a SHA
    comparison by construction. `_extract_vendored_stamp` reads the bool
    field directly (never a `-dirty` suffix strip), and a true value forces
    INDETERMINATE before any git comparison runs, never FRESH.
  - **Fix-and-regenerate-in-one-commit.** A peer commit that touches both
    the declared `sources` and the declared `artifact` is a regeneration,
    not evidence of drift — `commits_touching_since(..., artifact_path=...)`
    (C0) already excludes it; this leg passes `artifact_path` rather than
    re-deriving the exclusion.

## Peer-repo resolution

`resolve_peer_repo_path` imitates the order
`coordinator_core.frontmatter.schema_drift_watch.resolve_doe_repo_path`
already uses (REPO_DOE_CLAUDE env var, then the fleet registry's
`repos.doe_claude` entry, then the durable pointer file, then the legacy
pointer file — all three of the latter folded into
`coordinator_core.doe_root_pointer.read_doe_root_pointer`'s own ladder) —
without importing `schema_drift_watch` itself, which is owned by a live
sibling plan. An absent or unreadable clone (no candidate directory
containing a `coordinator/` subdir) resolves to `None`, which every caller
here folds into INDETERMINATE, never MATCH and never FRESH.

Negative-spec:
    - Does NOT add a calendar-age threshold or any second conjunct (AC2).
    - Does NOT re-implement `_git_root` or the since-range comparison; both
      are imported from `staleness_git`, never hand-copied a third time.
    - Does NOT read `Pair.generator`'s own path as the staleness pathspec.
    - Does NOT regenerate any artifact — a STALE verdict is a tell that
      regeneration is owed, not a regeneration.
    - Does NOT modify any file or run any destructive git operation, and
      never writes into the resolved peer clone — this leg only reads it.
    - Does NOT import `coordinator_core.frontmatter.schema_drift_watch`;
      `resolve_peer_repo_path` imitates its resolution order independently.
    - Does NOT run on the commit hot path or join the fast tier.

Exit codes (`main`): `0` no STALE pairs found; `1` (`EXIT_STALE`) at least
one pair verdicts STALE. Never raises — every failure path (missing repo
root, missing artifact, unreadable stamp, git failure, malformed `sources`,
unresolved peer clone) folds into a returned verdict (AC5/AC7); `main`
reports, it never gates.

Spec backlink: docs/plans/2026-08-13-generator-output-staleness-detector.md § C3, C6
"""
from __future__ import annotations

import datetime as _datetime
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.ops.generator_provenance import GeneratorRecord, Pair, discover_generators
from coordinator_core.ops.staleness_git import (
    SinceRange,
    Verdict,
    commits_touching_since,
    git_root,
    verdict_from_range,
)

EXIT_STALE = 1

PEER_REPO_SENTINEL = "coordinator/hooks/hooks.json"


def _extract_json_stamp(text: str, stamp_key: str) -> tuple[Optional[str], str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"artifact is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "artifact JSON top level is not an object"
    value = data.get(stamp_key)
    if not isinstance(value, str) or not value:
        return None, f"artifact JSON has no readable string {stamp_key!r}"
    return value, ""


def _split_frontmatter(text: str) -> Optional[str]:
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[1:idx])
    return None


def _extract_frontmatter_stamp(text: str, stamp_key: str) -> tuple[Optional[str], str]:
    fm_text = _split_frontmatter(text)
    if fm_text is None:
        return None, "artifact has no --- delimited frontmatter block"
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return None, f"artifact frontmatter is not valid YAML: {exc}"
    if not isinstance(fm, dict):
        return None, "artifact frontmatter top level is not a mapping"
    value = fm.get(stamp_key)
    if isinstance(value, (_datetime.datetime, _datetime.date)):
        # YAML auto-parses an unquoted ISO-8601 scalar into a date/datetime
        # object rather than a string; re-serialize it back to ISO-8601
        # rather than rejecting a plainly well-formed frontmatter stamp.
        value = value.isoformat()
    if not isinstance(value, str) or not value:
        return None, f"artifact frontmatter has no readable string {stamp_key!r}"
    return value, ""


def extract_stamp(artifact_full_path: Path, stamp_key: str) -> tuple[Optional[str], str]:
    """Read `<stamp_key>` from *artifact_full_path*'s own content.

    `.json` artifacts are read as a top-level key; anything else is read as
    a `---`-delimited frontmatter block at the top of the file. Never
    raises: any failure (missing file, unreadable encoding, malformed
    JSON/YAML, absent key) returns `(None, detail)`.
    """
    if not artifact_full_path.is_file():
        return None, f"artifact not found: {artifact_full_path}"

    try:
        text = artifact_full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"artifact unreadable: {exc}"

    if artifact_full_path.suffix.lower() == ".json":
        return _extract_json_stamp(text, stamp_key)
    return _extract_frontmatter_stamp(text, stamp_key)


def compute_pair_staleness(repo_root: Path, pair: Pair) -> dict[str, Any]:
    """Verdict for a single declared `Pair`.

    STALE iff `commits_touching_since(pair.sources, stamp)` (C0) is a
    non-empty range, where `stamp` is `pair.stamp_key` read from the
    artifact's own content (never its git history). UNSTAMPED when the
    artifact is missing or the stamp can't be read. INDETERMINATE on a git
    failure or malformed `sources`/`since_point`. Never raises.
    """
    repo_root = Path(repo_root)
    artifact_full_path = repo_root / pair.artifact

    stamp, detail = extract_stamp(artifact_full_path, pair.stamp_key)
    if stamp is None:
        return {"artifact": pair.artifact, "verdict": Verdict.UNSTAMPED, "detail": detail}

    if not pair.sources:
        return {
            "artifact": pair.artifact,
            "verdict": Verdict.INDETERMINATE,
            "detail": "pair declares no sources",
        }

    rng: SinceRange = commits_touching_since(
        repo_root,
        list(pair.sources),
        stamp,
        artifact_path=pair.artifact,
    )
    verdict = verdict_from_range(rng)
    return {
        "artifact": pair.artifact,
        "verdict": verdict,
        "detail": rng.detail or f"stamp={stamp}",
    }


def compute_repo_staleness(repo_root: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Verdict per declared artifact across every discovered generator.

    Keyed by artifact path for a declared pair, or by generator path for a
    generator C1 reports as UNDECLARED, WRITE_TARGET_UNRESOLVED, or
    MUTATES_DECLARED (none of the three has an artifact to key by --
    MUTATES_DECLARED's output set is data-dependent by design, so staleness
    is undefined for it, never computed).
    Never raises: an unresolvable *repo_root* yields a single INDETERMINATE
    entry rather than an empty dict silently reading as "nothing stale".
    """
    root = Path(repo_root) if repo_root is not None else git_root()
    if root is None:
        return {
            "<repo root unresolved>": {
                "artifact": None,
                "verdict": Verdict.INDETERMINATE,
                "detail": "could not resolve a git repo root",
            }
        }

    records: list[GeneratorRecord] = discover_generators(root)

    results: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.verdict in (Verdict.UNDECLARED, Verdict.WRITE_TARGET_UNRESOLVED, Verdict.MUTATES_DECLARED):
            results[record.generator] = {
                "artifact": None,
                "verdict": record.verdict,
                "detail": record.detail,
            }
            continue
        for pair in record.pairs:
            results[pair.artifact] = compute_pair_staleness(root, pair)

    return results


@dataclass(frozen=True)
class VendoredPair:
    """A peer(DoE-claude)-owned artifact claude-klabauter only reads, never writes."""

    artifact: str
    sources: tuple[str, ...]
    stamp_block: str


VENDORED_PAIRS: tuple[VendoredPair, ...] = (
    VendoredPair(
        artifact="coordinator/hooks/hooks.json",
        sources=("coordinator/hooks/scripts",),
        stamp_block="x-effective-delivery",
    ),
)


def resolve_peer_repo_path() -> Optional[Path]:
    """Best-effort resolution of the DoE-claude sibling clone root.

    Imitates `coordinator_core.frontmatter.schema_drift_watch
    .resolve_doe_repo_path`'s ladder (REPO_DOE_CLAUDE env var, then
    `read_doe_root_pointer()`'s registry/durable-file/legacy-file rungs)
    without importing that module. Returns None when no candidate directory
    contains `PEER_REPO_SENTINEL` (the artifact this leg actually consumes,
    `coordinator/hooks/hooks.json`) — a bare `coordinator/` subdir is not
    enough to call a candidate a real DoE-claude clone, and gating on the
    consumed artifact fails early and honestly when a clone is present but
    lacks it. Review: code-reviewer da34f46b — bare-dir sentinel accepted a
    stale/half-cloned checkout as a valid peer root. Never raises.
    """
    candidates: list[Path] = []

    env_root = os.environ.get("REPO_DOE_CLAUDE", "").strip()
    if env_root:
        candidates.append(Path(env_root))

    try:
        pointer_root = read_doe_root_pointer().strip()
    except Exception as exc:
        print(
            f"resolve_peer_repo_path: read_doe_root_pointer() raised unexpectedly "
            f"(never-raises contract violated): {exc}",
            file=sys.stderr,
        )
        pointer_root = ""
    if pointer_root:
        candidates.append(Path(pointer_root))

    for candidate in candidates:
        try:
            if (candidate / PEER_REPO_SENTINEL).is_file():
                return candidate
        except OSError:
            continue
    return None


def _extract_vendored_stamp(artifact_full_path: Path, stamp_block: str) -> tuple[Optional[dict], str]:
    """Read the `{stamp_block}` block's `generated_from_sha` /
    `generated_from_dirty_tree` from *artifact_full_path*'s own JSON content.

    Never raises: any failure (missing file, unreadable encoding, malformed
    JSON, absent/malformed block or fields) returns `(None, detail)`.
    """
    if not artifact_full_path.is_file():
        return None, f"vendored artifact not found: {artifact_full_path}"

    try:
        text = artifact_full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"vendored artifact unreadable: {exc}"

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"vendored artifact is not valid JSON: {exc}"

    if not isinstance(data, dict):
        return None, "vendored artifact JSON top level is not an object"

    block = data.get(stamp_block)
    if not isinstance(block, dict):
        return None, f"vendored artifact has no {stamp_block!r} block"

    sha = block.get("generated_from_sha")
    if not isinstance(sha, str) or not sha:
        return None, f"{stamp_block!r} block has no readable string generated_from_sha"

    dirty = block.get("generated_from_dirty_tree")
    if not isinstance(dirty, bool):
        return None, f"{stamp_block!r} block has no readable bool generated_from_dirty_tree"

    return {"sha": sha, "dirty": dirty}, ""


def compute_vendored_pair_staleness(peer_repo_root: Path, pair: VendoredPair) -> dict[str, Any]:
    """Verdict for a single `VendoredPair`, comparing WITHIN the peer clone.

    Extracts `generated_from_sha` / `generated_from_dirty_tree` from the
    artifact's own content (never the artifact file's git history in EITHER
    repo). `generated_from_dirty_tree=True` forces INDETERMINATE before any
    git comparison runs — the SHA is a lower bound on the emitting state,
    not an identification of it. Otherwise defers to C0's
    `commits_touching_since(peer_repo_root, sources, sha, artifact_path=...)`
    — the SAME comparison C3's local leg uses (AC9). Never raises.
    """
    peer_repo_root = Path(peer_repo_root)
    artifact_full_path = peer_repo_root / pair.artifact

    stamp, detail = _extract_vendored_stamp(artifact_full_path, pair.stamp_block)
    if stamp is None:
        return {"artifact": pair.artifact, "verdict": Verdict.UNSTAMPED, "detail": detail}

    sha = stamp["sha"]
    cite = f"DoE-claude@{sha}"

    if stamp["dirty"]:
        return {
            "artifact": pair.artifact,
            "verdict": Verdict.INDETERMINATE,
            "detail": f"{cite} generated_from_dirty_tree=true — SHA is a lower bound, not an identification",
        }

    if not pair.sources:
        return {
            "artifact": pair.artifact,
            "verdict": Verdict.INDETERMINATE,
            "detail": f"{cite} pair declares no sources",
        }

    rng: SinceRange = commits_touching_since(
        peer_repo_root,
        list(pair.sources),
        sha,
        artifact_path=pair.artifact,
    )
    verdict = verdict_from_range(rng)
    return {
        "artifact": pair.artifact,
        "verdict": verdict,
        "detail": rng.detail or cite,
    }


def compute_vendored_staleness() -> dict[str, dict[str, Any]]:
    """Verdict per declared `VENDORED_PAIRS` entry, keyed `DoE-claude:<artifact>`.

    Takes no *repo_root* — the vendored leg always resolves its OWN root via
    `resolve_peer_repo_path()`, never the local repo root. An earlier
    revision accepted and discarded a `repo_root` parameter "for signature
    symmetry" with `compute_repo_staleness`; that made the local repo root
    look load-bearing here when it structurally could not be, so the
    parameter is dropped rather than kept-and-ignored. `compute_all_staleness`
    still threads its own `repo_root` into `compute_repo_staleness` only.
    An unresolvable peer clone yields a single INDETERMINATE entry rather
    than an empty dict silently reading as "nothing vendored is stale".
    Never raises.
    """
    peer_root = resolve_peer_repo_path()
    if peer_root is None:
        return {
            "<DoE-claude clone unresolved>": {
                "artifact": None,
                "verdict": Verdict.INDETERMINATE,
                "detail": "could not resolve the DoE-claude sibling clone",
            }
        }

    results: dict[str, dict[str, Any]] = {}
    for pair in VENDORED_PAIRS:
        results[f"DoE-claude:{pair.artifact}"] = compute_vendored_pair_staleness(peer_root, pair)
    return results


def compute_all_staleness(repo_root: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Local leg (C3) plus vendored leg (C6), merged into one verdict dict.

    Local-leg keys are bare artifact paths; vendored-leg keys are prefixed
    `DoE-claude:` (see `compute_vendored_staleness`) so the two never
    collide. Never raises — each leg folds its own failures into
    INDETERMINATE entries rather than raising past this join.
    """
    results: dict[str, dict[str, Any]] = {}
    results.update(compute_repo_staleness(repo_root))
    results.update(compute_vendored_staleness())
    return results


def main(argv) -> int:
    results = compute_all_staleness()

    any_stale = False
    for artifact, result in results.items():
        verdict = result["verdict"]
        verdict_str = verdict.value if isinstance(verdict, Verdict) else str(verdict)
        print(f"{verdict_str} {artifact}")
        if verdict == Verdict.STALE:
            any_stale = True

    return EXIT_STALE if any_stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
