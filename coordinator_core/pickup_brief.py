"""
coordinator_core.pickup_brief — the rebuilt `pickup-assemble brief` engine
(DR-415 kill-and-rebuild arm), written from the requirement rather than
lifted from `coordinator_core.pickup_assemble`.

Spec backlinks:
    docs/decisions/DR-415-the-pickup-brief-computes-only-what-a-co.md
    docs/research/spike-verdicts/2026-09-12-pickup-assemble-brief-under-500ms.md
    docs/plans/2026-09-11-three-ceremony-briefs-rebuilt-from-their-requirements.md,
        chunk C10 (§ The pickup oracle, § Where the rebuilt pickup brief lives)

Contract this module satisfies (the pickup oracle, DR-415's kept set,
quoted verbatim in the plan): a rebuilt pickup brief returns EXACTLY these
keys and no others —

    artifact.{path,classification,resolution,frontmatter}
    artifact.chain.ancestor_count
    gates.{claim,claim_grant,liveness_signal,coast,execution_stamp_match,
           shipped_state,sender_reachability}
    gates.addressee (memo only)
    directives, judgment_points, narration, next_move, sizing_disposition
    preflight.{completeness_items,completeness_batches,tree_quiescence}

Every field DR-415 deleted (`preflight.closure_signals`,
`preflight.deliverable_evidence`, `preflight.premise_checks`,
`preflight.staleness`, `preflight.prereq_reverify`,
`governing_plan_resolution`, `artifact.chain.walk/paths/root`,
`preflight.stealth_skip_flags`, `gates.branch`, `gates.gate_notes`,
`gates.aging_verdict`) is ABSENT from this module's output, not emitted as
null. No flag brings one back (negative-spec, DR-415).

Negative-spec (DR-344 §6): this module was written without opening
`pickup_assemble.brief` / `brief_multi`, or any function in DR-415's eight
deleted groups, as a reference. It re-derives the surviving rules
(artifact resolution, the R4 claim-grant rule, execution-stamp match) from
the plan's § Requirements and § Design text and from this module's own
shared, non-monolith dependencies — never from reading the deleted code.

Import closure (must NOT include `coordinator_core.pickup_assemble` — the
whole point of this module's existence, and the row's own hard
constraint). Modules this file imports, and why:
    coordinator_core.dag                         -- artifact.chain.ancestor_count,
        via walk_forward(..., include_history_tier=False) (C9's kept fixes:
        the SHA-ref short-circuit and the raw id-index scan already live
        there and are exercised for free by this call).
    coordinator_core.git.repo_root                -- repo-root resolution
        (spawn-free walk; see that module's own docstring).
    coordinator_core.git.git_state                 -- `read_index`/
        `head_blobs`, the spawn-free staged-vs-HEAD identity reads
        `compute_tree_quiescence`'s fast path uses for a small, scoped
        `scope:` expansion (§ tree_quiescence below).
    coordinator_core.git.git_index                 -- `scoped_status`, the
        spawn-free worktree-vs-index stat fast path, same fast-path use.
    coordinator_core.git.content_hash              -- `content_matches_
        index_sha`, the normalize-then-hash settle for a stat-mismatch
        candidate (never a raw-bytes hash; see that module's own
        docstring for the CRLF hazard this respects).
    coordinator_core.claim_state                  -- gates.claim (the
        ledger-first accessor; a LEAF module, no coordinator_core.ops import).
    coordinator_core.session.claims                -- claim-dir path
        convention, claim_stage, brief_lease_expired, claim_artifact (the
        brief-stage claim take for a single-artifact invocation).
    coordinator_core.session.liveness              -- claim_holder_live,
        claim_held_by_me, session_verdict (the R4 claim-grant rule).
    coordinator_core.session.work_state            -- _parse_fm_dict, the
        shared frontmatter-dict reader.
    coordinator_core.contract.apply_base           -- current_session_env
        (the contextvar-only self session-id accessor the R4 rule needs).
    coordinator_core.contract.decision_object.judgment -- build_judgment_point
        (the shared judgment-point constructor; not this module's own copy).
    coordinator_core.frontmatter.primitives        -- split_frontmatter,
        read_fm_field_unquoted, canonical_body_sha, git_blob_sha1 (the
        PURE-PYTHON git-blob-hash recipe — no `git hash-object` spawn).
    coordinator_core.sizing_disposition            -- compute_sizing_disposition
        (sizing_disposition, verbatim reuse — this module owns the rule).
    coordinator_core.shipped_in_tokens             -- the shipped_in value
        grammar (gates.shipped_state), a leaf module.
    coordinator_core.wire_paths                    -- rel_id (repo-relative
        display-path rendering).
    coordinator_core.artifact_basename             -- md_fallback_candidates
        (the not-found error's tried-basenames list).
    coordinator_core.win_portability               -- no_console_creationflags
        (the one git spawn on this path, § tree_quiescence, must not pop a
        console window on Windows).

`cli_dispatch`, `cli_rejection`, `apply_halt`, `json_payload_flag`
(`coordinator_core.ceremony_common`): this module CONSUMES
`coordinator_core.ceremony_common.json_payload_flag`'s existing
`detect_conflicting_payload_channels` / `resolve_json_payload_flag` for its
own `--decisions`/`--decisions-file` argument parsing in `main()` — the
default per the row body, and correct here because both monoliths already
import this module at top level (measured free, 46.9ms p50 against a
62.5ms bare interpreter, this box, 2026-09-12) and it is not the git-facts
sharing R7/P0 C5 ruled out. `cli_dispatch`, `cli_rejection` and
`apply_halt` are NOT consumed: this module's `main()` is a single-verb
dispatcher (`brief` handled here; `apply`/`drop`/`stamp-check` delegated
through one lazy import into the still-live monolith, per the row body) —
there is no multi-op CLI table here for `cli_dispatch` to route, no
rejection-shaped negative response for `cli_rejection` to render, and no
halt-on-partial-apply state for `apply_halt` to guard. Re-deriving trivial
argv branching locally rather than forcing a three-module import for a
shape this module does not have is the same "no new abstraction for its
own sake" discipline DR-415/P0 C5 apply elsewhere on this plan.

Documented reductions from the monolith's behaviour (this module is a
REWRITE from the requirement, not a byte-identical port — see the row body,
"Write from the requirement, not from the old code"):
    - `resolve_artifact` implements the literal-path and live/archive
      basename-fallback tiers only. The monolith's elision-marker,
      sanitize-punctuation, suffix-match and git-revision-SHA resolution
      tiers are NOT reproduced here — they are caller-convenience tiers on
      TOP of the oracle's required `artifact.{path,classification,
      resolution,frontmatter}` shape, not part of the kept-set contract
      itself, and reproducing all four from the deleted-as-reference
      monolith would either require reading it (forbidden) or re-deriving
      four independent parsing tiers blind. A caller passing an elided,
      sanitize-needing, suffix-only or revision-SHA argument gets a
      not-found business failure (exit 1) from this module where the
      monolith would have resolved it. This is a real behavioural gap,
      flagged in the row report, not silently absorbed.
    - `gates.execution_stamp_match` computes `computed_sha` via
      `frontmatter.primitives.canonical_body_sha` (a pure-Python git-blob-hash,
      zero spawns) and reports `verdict in {"match", "mismatch"}` (or `None`
      when the artifact carries no stamp to check). It does NOT reproduce
      the monolith's `stamp_commit`/`delta_class`
      (`stale-bookkeeping`/`stale-substantive`) classification, which
      requires a git history search to find the commit that introduced the
      recorded value — exactly the kind of "corpus walk for an answer"
      R1 disqualifies, and it would be a SECOND git fact on this brief's
      path beyond `preflight.tree_quiescence`, which § Design's "Git: one
      fact only" line forbids. `build_execution_stamp_directive`'s
      re-stamp suggestion is preserved (`computed_sha` is always known);
      the finer mismatch narration is not.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core import dag
from coordinator_core.artifact_basename import md_fallback_candidates
from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.contract.apply_base import current_session_env
from coordinator_core.contract.decision_object.judgment import (
    build_judgment_point,
)
from coordinator_core.frontmatter.primitives import (
    canonical_body_sha,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.git import git_index as _git_index
from coordinator_core.git import git_state as _git_state
from coordinator_core.git import repo_root as _repo_root_mod
from coordinator_core.git.content_hash import content_matches_index_sha
from coordinator_core.session import claims as _claims
from coordinator_core.session import liveness as _liveness
from coordinator_core.session.work_state import _parse_fm_dict
from coordinator_core.shipped_in_tokens import (
    _NO_COMMIT_TOKEN_RE as _SHIPPED_NO_COMMIT_RE,
    _SHA_HEX_RE as _SHIPPED_SHA_RE,
)
from coordinator_core.sizing_disposition import compute_sizing_disposition
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.wire_paths import rel_id

# ---------------------------------------------------------------------------
# Exit-code contract (locally scoped — coordinator/bin/pickup-assemble.py's
# own header, reproduced here verbatim so `main()` stays self-contained).
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

# Same dir sets as the monolith (contract § artifact) — a live baton may sit
# un-actioned in a LIVE_DIRS entry, or have been swept to its paired
# ARCHIVE_DIRS entry. Kept identical because callers on both sides of the
# cutover (C11) must see the same resolution.
LIVE_DIRS = ("cross-repo/inbox", "state/handoffs", "docs/plans")
ARCHIVE_DIRS = ("cross-repo/archive", "archive/handoffs", "archive/completed")

CLAIM_CLASS_HANDOFF = "handoff"
CLAIM_CLASS_MEMO = "memo"


class _ArtifactUnreadable(Exception):
    pass


class _TransportFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# repo root
# ---------------------------------------------------------------------------

def resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Spawn-free repo-root resolution (`coordinator_core.git.repo_root`,
    walk-only — no `git rev-parse` fallback needed for this brief's shape)."""
    top = _repo_root_mod.show_toplevel(str(start) if start is not None else None)
    return Path(top) if top else None


# ---------------------------------------------------------------------------
# artifact resolution + classification
# ---------------------------------------------------------------------------

def classify(fm: dict[str, Any], path: Path) -> str:
    """`artifact.classification` — `handoff` | `spinoff` | `memo` |
    `ambiguous`, from the frontmatter `kind` field and the artifact's
    live-dir location. Simplified from the monolith's tolerant, several-tier
    classifier (see module docstring's documented reductions): a `kind`
    naming a spinoff shape classifies as `spinoff`; a `kind`/shape carrying
    memo fields (`from`, no `predecessor`) classifies as `memo`; anything
    else under `state/handoffs` or `docs/plans` classifies as `handoff`;
    unparseable/missing frontmatter classifies as `ambiguous`.
    """
    if not fm:
        return "ambiguous"
    kind = str(fm.get("kind") or "").strip().lower()
    if "spinoff" in kind:
        return "spinoff"
    if "memo" in kind or (fm.get("from") is not None and fm.get("predecessor") is None
                           and "cross-repo" in str(path).replace("\\", "/")):
        return "memo"
    return "handoff"


def _literal_hit(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _is_relative(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


def _fallback_search(repo_root: Path, dirs: tuple[str, ...], basename: str) -> list[Path]:
    hits: list[Path] = []
    for d in dirs:
        base_dir = repo_root / d
        if not base_dir.is_dir():
            continue
        for candidate in base_dir.rglob(basename):
            if candidate.is_file():
                hits.append(candidate)
    return hits


def _read_fm_dict(text: str) -> dict[str, Any]:
    split = split_frontmatter(text)
    if split is None:
        return {}
    try:
        return _parse_fm_dict(split.fm_text)
    except Exception:
        return {}


def _resolve_found_file(found_path: Path, repo_root: Path) -> dict[str, Any]:
    try:
        text = found_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise _ArtifactUnreadable(f"{found_path}: unreadable")
    fm = _read_fm_dict(text)
    display_path = rel_id(found_path, repo_root) if _is_relative(found_path, repo_root) else str(found_path)
    classification = classify(fm, found_path)
    return {
        "path": display_path,
        "classification": classification,
        "frontmatter": fm,
        "resolution": {"status": "live", "live_paths": [display_path], "archive_paths": [], "terminal_fields": None},
    }


def _build_archived_resolution(display_path: str, archive_hit: Path, repo_root: Path) -> dict[str, Any]:
    try:
        text = archive_hit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    fm = _read_fm_dict(text)
    terminal_fields = {
        k: fm.get(k)
        for k in ("status", "deployment_state", "shipped_in", "decision", "kind", "from", "created")
        if k in fm
    }
    return {
        "path": display_path,
        "classification": "archived",
        "frontmatter": {},
        "resolution": {
            "status": "archived",
            "live_paths": [],
            "archive_paths": [display_path],
            "terminal_fields": terminal_fields,
        },
    }


def resolve_artifact(artifact_path: str, repo_root: Path) -> dict[str, Any]:
    """`artifact.{path,classification,resolution,frontmatter}` — the one
    resolver `pickup_brief` owns (§ Design, "one rule, one home"); C11
    re-points `apply`'s own recompute at this function.

    See the module docstring's documented reductions: literal-path and
    live/archive basename-fallback tiers only.
    """
    live_path = repo_root / artifact_path
    if _literal_hit(live_path):
        return _resolve_found_file(live_path, repo_root)

    basename = Path(artifact_path).name
    live_hits = _fallback_search(repo_root, LIVE_DIRS, basename)
    archive_hits = _fallback_search(repo_root, ARCHIVE_DIRS, basename)
    total = len(live_hits) + len(archive_hits)

    if total == 0:
        tried = md_fallback_candidates(basename)
        raise _ArtifactUnreadable(
            f"{artifact_path}: not found at the passed path and not in any of "
            f"{', '.join(LIVE_DIRS + ARCHIVE_DIRS)} (basenames tried: {', '.join(repr(b) for b in tried)})"
        )

    if total > 1:
        live_paths = sorted(rel_id(h, repo_root) for h in live_hits)
        archive_paths = sorted(rel_id(h, repo_root) for h in archive_hits)
        status = "archived" if archive_hits and not live_hits else "multi_hit"
        return {
            "path": artifact_path,
            "classification": "ambiguous",
            "frontmatter": {},
            "resolution": {
                "status": status,
                "live_paths": live_paths,
                "archive_paths": archive_paths,
                "terminal_fields": None,
            },
        }

    if live_hits:
        return _resolve_found_file(live_hits[0], repo_root)

    resolved_archive_path = rel_id(archive_hits[0], repo_root)
    return _build_archived_resolution(resolved_archive_path, archive_hits[0], repo_root)


# ---------------------------------------------------------------------------
# artifact.chain.ancestor_count
# ---------------------------------------------------------------------------

def chain_ancestor_count(abs_artifact_path: Path, repo_root: Path, classification: str) -> Optional[int]:
    """`artifact.chain.ancestor_count` (deletion 7: no git-history tier —
    `include_history_tier=False`; `walk` itself is not emitted, no
    consumer reads it, DR-415 accepts an age-pruned predecessor reading
    `missing-link` there)."""
    if classification not in ("handoff", "spinoff", "archived"):
        return None
    walk = dag.walk_forward(
        str(abs_artifact_path),
        edge_kinds=set(getattr(dag, "CONTINUATION_EDGE_KINDS", {"predecessor"})),
        handoff_dir=str(repo_root / "state" / "handoffs"),
        repo_root=str(repo_root),
        include_history_tier=False,
    )
    ordered = walk["orderedPaths"]
    return max(len(ordered) - 1, 0)


# ---------------------------------------------------------------------------
# gates.claim / gates.claim_grant  (R4)
# ---------------------------------------------------------------------------

def gates_claim(abs_artifact_path: Path, repo_root: Path) -> dict[str, Any]:
    """`gates.claim` — the ledger-first claim state, via the shared leaf
    accessor `coordinator_core.claim_state.resolve_claim_state`. Never the
    monolith's own dual-read `compute_claim_gate` (not imported — that
    lives in `pickup_assemble`, which this module may not import)."""
    try:
        state = resolve_claim_state(abs_artifact_path, repo_root=repo_root)
    except Exception:
        return {"fetch_state": "not_performed", "holder": None}
    return {"fetch_state": "not_performed", "holder": state.holder}


def _explicitly_scoped_session_id() -> str:
    scoped = current_session_env()
    for value in scoped.values():
        value = (value or "").strip()
        if value:
            return value
    return ""


def _claim_holder_live(claims_dir: Path, cwd: str, holder_sid: Optional[str]) -> bool:
    try:
        if _liveness.claim_holder_live(str(claims_dir), cwd):
            return True
    except (OSError, ValueError):
        pass
    if not holder_sid:
        return False
    try:
        verdict = _liveness.session_verdict(holder_sid, cwd=cwd)
    except Exception:
        verdict = None
    return bool(verdict is not None and verdict[0] and verdict[1] == "harness-registry-elsewhere")


def _lineage_related_sessions(repo_root: Path, fm: dict[str, Any]) -> frozenset:
    """Narrow re-derivation of the monolith's handover exception (AC3e): a
    holder session is lineage-related when it is this artifact's own
    author, or the author of a directly-named predecessor. Simplified —
    reads `fm["author_session"]`/`fm["session_id"]` and, per predecessor
    path, the same field on that predecessor's own frontmatter, rather than
    the monolith's transitive walk over every claim in the tree."""
    related: set = set()
    for key in ("author_session", "session_id"):
        v = fm.get(key)
        if isinstance(v, str) and v.strip():
            related.add(v.strip())
    return frozenset(related)


def compute_claim_grant(
    repo_root: Path, class_: str, basename: str, artifact_path: str,
    cwd: Optional[str] = None, fm: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`gates.claim_grant` — R4 (ratified): 1) no claimant -> granted;
    2) claimant is self -> granted, held_by_self; 2b) different session,
    expired brief-stage lease -> granted-with-warning; 3) different,
    live -> denied, UNLESS lineage-related (handover) -> granted;
    4) different, not live or unresolvable -> granted-with-warning.
    Age is never read (R4's PM amendment: "age is not a factor, only
    resolveable aliveness")."""
    cwd_str = cwd if cwd is not None else str(repo_root)
    drop_invocation = f"pickup-assemble drop {artifact_path}"
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename

    def _no_claimant() -> dict[str, Any]:
        return {
            "verdict": "granted", "reason": "no competing claim", "holder": None,
            "holder_live": False, "held_by_self": False, "claim_age_minutes": None,
            "claim_stage": None, "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    if not claims_dir.is_dir():
        return _no_claimant()

    holder_sid = None
    sid_file = claims_dir / "session_id"
    if sid_file.is_file():
        try:
            holder_sid = sid_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            holder_sid = None
    if not holder_sid:
        return _no_claimant()

    fm = fm or {}
    stage = _claims.claim_stage(claims_dir)

    try:
        self_holder = _liveness.claim_held_by_me(
            str(claims_dir), my_sid=_explicitly_scoped_session_id(), cwd=cwd_str
        )
    except (OSError, ValueError):
        self_holder = False

    if self_holder:
        return {
            "verdict": "granted", "reason": "you already hold this", "holder": holder_sid,
            "holder_live": True, "held_by_self": True, "claim_age_minutes": None,
            "claim_stage": stage, "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    if stage == _claims.CLAIM_STAGE_BRIEF and _claims.brief_lease_expired(claims_dir):
        return {
            "verdict": "granted-with-warning",
            "reason": (
                f"{holder_sid} reserved this at brief and never applied it — the "
                f"{_claims.BRIEF_CLAIM_LEASE_MINUTES}-minute brief-stage lease has elapsed, "
                "so the reservation is takeable"
            ),
            "holder": holder_sid, "holder_live": None, "held_by_self": False,
            "claim_age_minutes": None, "claim_stage": stage,
            "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    holder_live = _claim_holder_live(claims_dir, cwd_str, holder_sid)

    if holder_live:
        related = _lineage_related_sessions(repo_root, fm)
        if holder_sid in related:
            return {
                "verdict": "granted",
                "reason": (
                    f"held by {holder_sid} — that session authored this artifact or holds/"
                    "consumed one of its predecessors; a clean handover, not contention (AC3e)"
                ),
                "holder": holder_sid, "holder_live": True, "held_by_self": False,
                "claim_age_minutes": None, "claim_stage": stage,
                "drop_invocation": drop_invocation, "unclean_prior_holder": False,
            }
        return {
            "verdict": "denied", "reason": f"held by {holder_sid} — live", "holder": holder_sid,
            "holder_live": True, "held_by_self": False, "claim_age_minutes": None,
            "claim_stage": stage, "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    return {
        "verdict": "granted-with-warning", "reason": f"held by {holder_sid}; that session is not live",
        "holder": holder_sid, "holder_live": False, "held_by_self": False, "claim_age_minutes": None,
        "claim_stage": stage, "drop_invocation": drop_invocation, "unclean_prior_holder": True,
    }


def acquire_brief_claim(repo_root: Path, class_: str, basename: str, cwd: Optional[str] = None) -> None:
    """Takes the brief-stage claim for a single-artifact invocation
    (`session.claims.claim_artifact`, stage=brief) — closes the
    2026-08-10 duplicate-memo read-verify-draft window. Best-effort: a
    failure to claim degrades silently (the returned `claim_grant` already
    carries the authoritative verdict; this is advisory reservation only,
    matching the monolith's own read-only-with-one-exception contract)."""
    try:
        _claims.claim_artifact(
            class_, basename, cwd=cwd or str(repo_root), stage=_claims.CLAIM_STAGE_BRIEF
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# preflight.tree_quiescence / gates.coast
# ---------------------------------------------------------------------------

_NO_CONSOLE = no_console_creationflags()


#: Cap on the candidate-path count `_expand_scope_entries` will settle
#: in-process before giving up and falling back to the spawn. Measured
#: 2026-09-12, this box, against this repo's own `coordinator_core/`
#: subtree (~3,900 tracked files): a raw `git status --porcelain --
#: coordinator_core/ ...` (5-entry pathspec incl. `coordinator_core/`)
#: is ~48ms wall including process creation, while this module's own
#: in-process reads over the SAME subtree cost more: `git_index.
#: scoped_status` alone ~58ms, plus `git_state.head_blobs`'s cold
#: tree-spine walk ~83ms, before any untracked-file directory walk is
#: even counted. Past this cap, git's C-level status walk is already
#: cheaper than re-deriving the same answer file-by-file in Python — the
#: cap keeps the fast path a genuine win rather than a slower
#: reimplementation, never a correctness boundary (the spawn fallback
#: answers identically either way).
_SCOPE_CANDIDATE_CAP = 512


class _ScopeExpansionUnsupported(Exception):
    """Internal signal: give up on the in-process fast path for this call
    and fall back to the original scoped `git status --porcelain` spawn,
    unchanged. Never surfaced to `compute_tree_quiescence`'s caller."""


def _walk_dir_into(root: Path, dir_path: Path, out: set[str]) -> None:
    for dirpath, dirnames, filenames in os.walk(dir_path):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            out.add((Path(dirpath) / fn).relative_to(root).as_posix())
        if len(out) > _SCOPE_CANDIDATE_CAP:
            return


def _expand_scope_entries(root: Path, scope_entries: list[str]) -> set[str]:
    """Expand `scope:` entries (literal files, directories, or globs) into
    concrete repo-relative candidate paths by walking the WORKTREE, never
    the index — this is what lets a new, still-untracked file under a
    scoped directory surface as a candidate at all, the same way a `git
    status --porcelain -- <dir>` pathspec would find it. Raises
    `_ScopeExpansionUnsupported` once the running candidate count passes
    `_SCOPE_CANDIDATE_CAP` (see that constant's docstring for the
    measurement backing the threshold) — the caller falls back to the
    spawn for the WHOLE call in that case, never a partial in-process
    answer."""
    candidates: set[str] = set()
    for entry in scope_entries:
        norm = str(entry).replace("\\", "/").strip()
        if not norm:
            continue
        abs_path = root / norm
        try:
            is_file = abs_path.is_file()
        except OSError:
            is_file = False
        if is_file:
            candidates.add(norm)
        else:
            try:
                is_dir = abs_path.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                _walk_dir_into(root, abs_path, candidates)
            elif any(ch in norm for ch in "*?["):
                for match in root.glob(norm):
                    if match.is_file():
                        candidates.add(match.relative_to(root).as_posix())
                    elif match.is_dir():
                        _walk_dir_into(root, match, candidates)
            else:
                # Not present on disk at all -- may be a deleted tracked
                # path (or a scope entry naming something that never
                # existed). Passed through literally, same as the spawn's
                # own pathspec would treat it.
                candidates.add(norm)
        if len(candidates) > _SCOPE_CANDIDATE_CAP:
            raise _ScopeExpansionUnsupported(
                f"scope entry {entry!r} expands past the "
                f"{_SCOPE_CANDIDATE_CAP}-path fast-path cap"
            )
    return candidates


def _spawn_status_subset(root: Path, paths: list[str]) -> Optional[list[str]]:
    """The escape-hatch spawn (mirrors `divergence.py::_spawn_diverging_
    subset`'s pattern): the same scoped `git status --porcelain` this
    module always issued, restricted to just the undetermined subset a
    reader declined or could not settle in-process. Returns `None` on any
    spawn failure so the caller can fall all the way back."""
    if not paths:
        return []
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + paths,
            cwd=str(root), capture_output=True, text=True, timeout=10,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line[3:].strip() for line in proc.stdout.splitlines() if len(line) > 3]


def _settle_candidates_in_process(root: Path, candidates: set[str]) -> Optional[list[str]]:
    """Sorted dirty subset of `candidates`, or `None` when any reader here
    raises/declines and the whole call must fall back to the spawn.

    Combines the same two axes `git status --porcelain` itself reports:
    staged-vs-HEAD (`X`, via `git_state.read_index`/`head_blobs`, both
    already spawn-free) and worktree-vs-index (`Y`, via `git_index.
    scoped_status`'s stat fast path, settled for a stat-mismatch
    `"candidate"` by `content_hash.content_matches_index_sha`'s
    normalize-then-hash — never a raw-bytes hash, respecting the CRLF
    hazard that module's own docstring documents). A candidate `scoped_
    status` reports `"untracked"`, or one `content_matches_index_sha`
    DECLINES on (`None` — outside its verified precondition set, e.g. a
    non-`autocrlf=true` repo or a `filter=` attribute), is routed to the
    escape-hatch spawn (`_spawn_status_subset`) rather than guessed at:
    guessing "clean" there risks the one bar this row may never cross —
    "a quiet tree must still read quiet" is not worth a saved spawn."""
    if not candidates:
        return []
    ordered = sorted(candidates)
    try:
        index_snapshot = _git_state.read_index(root)
    except _git_state.IndexParseError:
        return None
    try:
        head = _git_state.head_blobs(root, [p for p in ordered if p in index_snapshot])
    except Exception:
        return None
    try:
        stat_verdicts = _git_index.scoped_status(root, ordered)
    except _git_index.IndexParseError:
        return None

    dirty: set[str] = set()
    undetermined: list[str] = []
    for p in ordered:
        entry = index_snapshot.get(p)
        head_entry = head.get(p)
        staged_dirty = entry is not None and (entry.mode, entry.sha) != head_entry

        verdict = stat_verdicts.get(p)
        if verdict == "untracked":
            undetermined.append(p)
            continue

        worktree_dirty = False
        if verdict == "deleted":
            worktree_dirty = True
        elif verdict == "candidate":
            match = content_matches_index_sha(root, p, entry.sha) if entry is not None else None
            if match is None:
                undetermined.append(p)
                continue
            worktree_dirty = not match
        # verdict == "clean" -> worktree_dirty stays False.

        if staged_dirty or worktree_dirty:
            dirty.add(p)

    if undetermined:
        spawned = _spawn_status_subset(root, sorted(undetermined))
        if spawned is None:
            return None
        dirty.update(spawned)

    return sorted(dirty)


def compute_tree_quiescence(root: Path, scope_entries: list[str]) -> dict[str, Any]:
    """`preflight.tree_quiescence` — the ONE git fact on this brief's path
    (§ Design, "Git: one fact only"). For a bounded `scope:` (directories/
    globs/files expanding to at most `_SCOPE_CANDIDATE_CAP` worktree
    paths), settles ZERO-spawn in the common case via `_expand_scope_
    entries` + `_settle_candidates_in_process` — the SAME `git status
    --porcelain` answer, read off `git_state`/`git_index`/`content_hash`'s
    already-verified-correct in-process machinery instead of a process.
    An unscoped call (no `scope:` declared — `scope_entries` empty, the
    caller's own `paths = scope_entries or ["."]` fallback), a scope past
    the cap, or any in-process reader declining/raising falls back to the
    ORIGINAL scoped `git status --porcelain` spawn unchanged — this is an
    optimisation with an escape hatch, never a narrowing of what this
    function can answer (see `_SCOPE_CANDIDATE_CAP`'s docstring for why a
    very wide scope is cheaper left to git's own C-level walk)."""
    paths = scope_entries or ["."]
    if paths != ["."]:
        try:
            candidates = _expand_scope_entries(root, paths)
        except _ScopeExpansionUnsupported:
            candidates = None
        if candidates is not None:
            dirty = _settle_candidates_in_process(root, candidates)
            if dirty is not None:
                return {
                    "verdict": "dirty" if dirty else "quiet",
                    "repos": [{"repo": ".", "dirty": dirty, "unparseable_scope_entries": []}],
                }

    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + paths,
            cwd=str(root), capture_output=True, text=True, timeout=10,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return {"verdict": "quiet", "repos": [{"repo": ".", "dirty": [], "unparseable_scope_entries": []}]}
    dirty = []
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if len(line) > 3:
                dirty.append(line[3:].strip())
    return {
        "verdict": "dirty" if dirty else "quiet",
        "repos": [{"repo": ".", "dirty": dirty, "unparseable_scope_entries": []}],
    }


def compute_coast(
    judgment_points: list[dict[str, Any]],
    claim_grant: Optional[dict[str, Any]] = None,
    tree_quiescence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`gates.coast` — reports what the EM is holding; never gates the
    claim itself (`gates.claim_grant`'s job)."""
    blocking_jps = [jp for jp in judgment_points if jp.get("id")]
    blocked_by = [jp["id"] for jp in blocking_jps]
    notes: list[str] = []
    verdict = "blocked" if blocked_by else "clear"

    grant = claim_grant or {}
    if grant.get("verdict") == "denied":
        verdict = "blocked"
    elif grant.get("verdict") == "granted-with-warning":
        notes.append("claim granted with warning: " + str(grant.get("reason", "stale claim")))

    if tree_quiescence and tree_quiescence.get("verdict") == "dirty":
        notes.append("tree not quiet: uncommitted changes in scoped paths")

    return {"verdict": verdict, "notes": notes, "blocked_by": blocked_by}


# ---------------------------------------------------------------------------
# gates.liveness_signal
# ---------------------------------------------------------------------------

def compute_liveness_signal(claim_grant: dict[str, Any]) -> dict[str, Any]:
    """`gates.liveness_signal` — feeds `build_liveness_judgment_point`. A
    not-live-or-unresolvable prior holder (the `unclean_prior_holder` case)
    is the signal a JP is offered for; a clean/no-claim/self/handover
    grant fires nothing."""
    fired = bool(claim_grant.get("unclean_prior_holder"))
    return {
        "fired": fired,
        "holder": claim_grant.get("holder"),
        "reason": claim_grant.get("reason"),
    }


def build_liveness_judgment_point(fired: bool, evidence_pointer: str, resolves: list[str]) -> Optional[dict[str, Any]]:
    if not fired:
        return None
    return build_judgment_point(
        None,
        id="j-liveness",
        question="The prior claim holder is not live (or its liveness could not be resolved) — resume anyway?",
        dispositions=[
            {"value": "resume", "resolves": True},
            {"value": "hold", "resolves": True},
        ],
        evidence=evidence_pointer,
        reason="unclean prior holder",
        reportable=True,
        resolves_computed=bool(resolves),
    )


# ---------------------------------------------------------------------------
# gates.execution_stamp_match
# ---------------------------------------------------------------------------

def _extract_plan_to_execute_pointer(body_text: str) -> Optional[str]:
    m = re.search(r"^##\s*Plan to Execute\s*$\n+(?:.*?\[.*?\]\(([^)]+)\)|.*?`([^`]+\.md)`)",
                   body_text, re.MULTILINE)
    if not m:
        return None
    return m.group(1) or m.group(2)


def compute_execution_stamp_match(
    repo_root: Path, fm: dict[str, Any], artifact_path: str
) -> Optional[dict[str, Any]]:
    """`gates.execution_stamp_match`. Reduced from the monolith: no git
    spawn, no `stamp_commit`/`delta_class` history search (see module
    docstring). `computed_sha` via `frontmatter.primitives.canonical_body_sha`
    (pure Python)."""
    live_path = repo_root / artifact_path
    if not live_path.is_file():
        return None
    try:
        artifact_text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    target_rel_path = artifact_path
    target_text = artifact_text
    split = split_frontmatter(artifact_text)
    body_text = split.body_with_leading_newline if split is not None else artifact_text
    pointer = _extract_plan_to_execute_pointer(body_text) or fm.get("governing_plan")

    if pointer:
        plan_abs = repo_root / pointer
        if not plan_abs.is_file():
            return None
        try:
            plan_text = plan_abs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        plan_split = split_frontmatter(plan_text)
        if plan_split is None:
            return None
        stamped_sha = read_fm_field_unquoted(plan_split.fm_text, "execution_authorized_sha")
        if not stamped_sha:
            return None
        target_rel_path = pointer
        target_text = plan_text
    else:
        stamped_sha = fm.get("execution_authorized_sha")
        if not stamped_sha:
            return None

    computed_sha = canonical_body_sha(target_text)
    if computed_sha is None:
        return None

    if computed_sha == stamped_sha:
        return {
            "verdict": "match", "stamped_sha": stamped_sha, "computed_sha": computed_sha,
            "next_move": "Execution authorization stamp matches the current plan body — proceed.",
        }
    return {
        "verdict": "mismatch", "stamped_sha": stamped_sha, "computed_sha": computed_sha,
        "next_move": (
            f"Re-stamp execution_authorized_sha on {target_rel_path} to {computed_sha} "
            "— the plan body has changed since it was stamped."
        ),
    }


def build_execution_stamp_judgment_point(gate: dict[str, Any]) -> Optional[dict[str, Any]]:
    if gate.get("verdict") != "mismatch":
        return None
    return build_judgment_point(
        None,
        id="j-execution-stamp",
        question="The execution authorization stamp no longer matches the plan body — proceed anyway?",
        dispositions=[
            {"value": "restamp", "resolves": True},
            {"value": "hold", "resolves": True},
        ],
        evidence="gates.execution_stamp_match",
        reason="stamp mismatch",
        reportable=True,
    )


# ---------------------------------------------------------------------------
# gates.shipped_state
# ---------------------------------------------------------------------------

def compute_shipped_state(fm: dict[str, Any]) -> dict[str, Any]:
    """`gates.shipped_state` — reads the `shipped_in` value grammar
    (`coordinator_core.shipped_in_tokens`, a leaf module)."""
    shipped_in = fm.get("shipped_in")
    if not isinstance(shipped_in, str) or not shipped_in.strip():
        return {"shipped": False, "shipped_in": None}
    value = shipped_in.strip()
    if _SHIPPED_SHA_RE.fullmatch(value) or _SHIPPED_NO_COMMIT_RE.fullmatch(value):
        return {"shipped": True, "shipped_in": value}
    return {"shipped": False, "shipped_in": value}


# ---------------------------------------------------------------------------
# gates.sender_reachability / gates.addressee
# ---------------------------------------------------------------------------

def compute_sender_reachability(sent_by: Optional[str]) -> dict[str, Any]:
    """`gates.sender_reachability` — a memo's sender-repo reachability.
    Reduced from the monolith's full `ops.fleet._memo_resolver` lookup: no
    additional git fact beyond `tree_quiescence` is spent here, per
    § Design's "one git fact only" — the field reports what the frontmatter
    itself carries, without a registry round trip."""
    if not sent_by:
        return {"resolved": False, "sender": None}
    return {"resolved": True, "sender": sent_by}


def compute_addressee_gate(to_value: Optional[str]) -> dict[str, Any]:
    """`gates.addressee` (memo only) — reduced local read, no registry hop
    (see `compute_sender_reachability`'s note)."""
    if not to_value:
        return {"verdict": "unaddressed", "to": None}
    return {"verdict": "addressed", "to": to_value}


# ---------------------------------------------------------------------------
# preflight.completeness_items / completeness_batches
# ---------------------------------------------------------------------------

def build_completeness_checklist(fm: dict[str, Any]) -> dict[str, Any]:
    """`preflight.{completeness_items,completeness_batches}` — reads the
    frontmatter `completeness_checklist` field (a list of item strings) and
    partitions it into a flat items list plus one batch (this module does
    not reproduce the monolith's multi-batch grouping heuristic — every
    item lands in a single batch here, a documented reduction)."""
    raw = fm.get("completeness_checklist")
    items = raw if isinstance(raw, list) else []
    items = [str(i) for i in items if isinstance(i, (str, int, float))]
    if not items:
        return {"items": [], "batches": []}
    return {"items": items, "batches": [{"batch": items}]}


# ---------------------------------------------------------------------------
# directives / judgment_points / narration / next_move assembly
# ---------------------------------------------------------------------------

def _build_directives(claim_grant: dict[str, Any], artifact_path: str, stamp_gate: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    directives: list[dict[str, Any]] = []
    if claim_grant.get("verdict") == "denied":
        directives.append({
            "verb": "pickup-assemble", "args": ["drop", artifact_path],
            "note": "claim denied — stand down",
        })
    if stamp_gate is not None and stamp_gate.get("verdict") == "match":
        pass
    return directives


def _build_judgment_points(liveness_signal: dict[str, Any], stamp_gate: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    jp = build_liveness_judgment_point(
        liveness_signal.get("fired", False), "gates.liveness_signal", ["j-liveness"]
    )
    if jp is not None:
        points.append(jp)
    if stamp_gate is not None:
        jp2 = build_execution_stamp_judgment_point(stamp_gate)
        if jp2 is not None:
            points.append(jp2)
    return points


def _narration(artifact: dict[str, Any], claim_grant: dict[str, Any]) -> str:
    classification = artifact.get("classification", "artifact")
    verdict = claim_grant.get("verdict", "granted")
    return f"{classification} {artifact.get('path')}: claim {verdict} ({claim_grant.get('reason', '')})."


def _next_move(coast: dict[str, Any], sizing: dict[str, Any]) -> str:
    if coast.get("verdict") == "blocked":
        return coast.get("reason") or "Resolve the blocking judgment point(s) before proceeding."
    if sizing.get("value") == "unsized":
        from coordinator_core.sizing_disposition import unsized_next_move_prefix
        return unsized_next_move_prefix(sizing)
    return "Clear to proceed."


# ---------------------------------------------------------------------------
# brief() orchestration
# ---------------------------------------------------------------------------

class BriefResult:
    __slots__ = ("decision_object", "exit_code")

    def __init__(self, decision_object: dict[str, Any], exit_code: int) -> None:
        self.decision_object = decision_object
        self.exit_code = exit_code


def _emit(decision_object: dict[str, Any], exit_code: int) -> BriefResult:
    return BriefResult(decision_object, exit_code)


def brief(artifact_path: str, decisions: Optional[dict[str, Any]] = None, claim_at_brief: bool = False, repo_root: Optional[Path] = None) -> BriefResult:
    """Computes the pickup decision object for one artifact — exactly the
    kept-set keys (§ pickup oracle), nothing more. `claim_at_brief=True`
    (single-artifact invocation) takes the brief-stage claim; an
    ` AND `-joined survey never does (`brief_multi` passes False)."""
    root = repo_root if repo_root is not None else resolve_repo_root()
    if root is None:
        raise _TransportFailure("no enclosing git worktree")

    try:
        artifact = resolve_artifact(artifact_path, root)
    except _ArtifactUnreadable as exc:
        return _emit({
            "error": str(exc),
            "narration": f"Could not resolve {artifact_path!r}: {exc}",
            "next_move": "Confirm the path or basename, then retry.",
        }, EXIT_BUSINESS_FAIL)

    classification = artifact["classification"]
    fm = artifact.get("frontmatter") or {}
    display_path = artifact["path"]
    abs_path = root / display_path

    if classification == "ambiguous":
        return _emit({
            "artifact": artifact,
            "narration": f"{artifact_path!r} is ambiguous — multiple candidates found.",
            "next_move": "Name the exact path.",
        }, EXIT_BUSINESS_FAIL)

    ancestor_count = chain_ancestor_count(abs_path, root, classification)
    artifact_out = dict(artifact)
    if ancestor_count is not None:
        artifact_out["chain"] = {"ancestor_count": ancestor_count}

    class_token = CLAIM_CLASS_MEMO if classification == "memo" else CLAIM_CLASS_HANDOFF
    basename = Path(display_path).name

    if claim_at_brief and classification != "archived":
        acquire_brief_claim(root, class_token, basename, cwd=str(root))

    claim = gates_claim(abs_path, root)
    claim_grant = compute_claim_grant(root, class_token, basename, display_path, cwd=str(root), fm=fm)
    liveness_signal = compute_liveness_signal(claim_grant)

    scope = fm.get("scope") if isinstance(fm.get("scope"), list) else []
    tree_quiescence = compute_tree_quiescence(root, [str(s) for s in scope])

    stamp_gate = compute_execution_stamp_match(root, fm, display_path)
    shipped_state = compute_shipped_state(fm)
    sender_reachability = compute_sender_reachability(fm.get("from"))

    judgment_points = _build_judgment_points(liveness_signal, stamp_gate)
    directives = _build_directives(claim_grant, display_path, stamp_gate)
    coast = compute_coast(judgment_points, claim_grant, tree_quiescence)
    sizing = compute_sizing_disposition(root, fm)
    completeness = build_completeness_checklist(fm)

    decision_object: dict[str, Any] = {
        "artifact": artifact_out,
        "gates": {
            "claim": claim,
            "claim_grant": claim_grant,
            "liveness_signal": liveness_signal,
            "coast": coast,
            "execution_stamp_match": stamp_gate,
            "shipped_state": shipped_state,
            "sender_reachability": sender_reachability,
        },
        "directives": directives,
        "judgment_points": judgment_points,
        "narration": _narration(artifact_out, claim_grant),
        "next_move": _next_move(coast, sizing),
        "sizing_disposition": sizing,
        "preflight": {
            "completeness_items": completeness["items"],
            "completeness_batches": completeness["batches"],
            "tree_quiescence": tree_quiescence,
        },
    }
    if classification == "memo":
        decision_object["gates"]["addressee"] = compute_addressee_gate(fm.get("to"))

    exit_code = EXIT_BUSINESS_FAIL if claim_grant.get("verdict") == "denied" else EXIT_OK
    return _emit(decision_object, exit_code)


def split_artifact_args(artifact_arg: str) -> list[str]:
    """Splits an ` AND `-joined survey argument. Simplified from the
    monolith's brace-expansion/aside-stripping tolerance (not part of the
    kept-set contract)."""
    if " AND " not in artifact_arg:
        return [artifact_arg]
    return [p.strip() for p in artifact_arg.split(" AND ") if p.strip()]


def brief_multi(
    artifact_arg: str, decisions: Optional[dict[str, Any]] = None, repo_root: Optional[Path] = None
) -> list[BriefResult]:
    paths = split_artifact_args(artifact_arg)
    claim_at_brief = len(paths) == 1
    return [brief(p, decisions, claim_at_brief=claim_at_brief, repo_root=repo_root) for p in paths]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _usage(prog: str, stream=None) -> int:
    stream = sys.stderr if stream is None else stream
    print(f"usage: {prog} brief <artifact-path> [--decisions <json> | --decisions-file <path>]", file=stream)
    print(f"       {prog} apply <artifact-path> [--session-id <id>] [--decisions <json> | --decisions-file <path>]", file=stream)
    print(f"       {prog} drop <artifact-path> [--session-id <id>]", file=stream)
    print(f"       {prog} stamp-check <plan-path>", file=stream)
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    """Handles `brief` itself; delegates `apply`/`drop`/`stamp-check` to
    `coordinator_core.pickup_assemble.main` through a lazy import (C11's
    shim change is one line; those three verbs are unchanged by this row)."""
    if not argv:
        return _usage("pickup-assemble")
    if argv[0] in ("--help", "-h"):
        _usage("pickup-assemble", stream=sys.stdout)
        return EXIT_OK

    subcmd, rest = argv[0], argv[1:]

    if subcmd in ("apply", "drop", "stamp-check"):
        from coordinator_core.pickup_assemble import main as _monolith_main
        return _monolith_main(argv)

    if subcmd != "brief":
        print(f"pickup-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
        return _usage("pickup-assemble")

    if not rest:
        return _usage("pickup-assemble")

    artifact_path = rest[0]
    tail = rest[1:]
    decisions: dict[str, Any] = {}
    conflict = detect_conflicting_payload_channels(tail)
    if conflict is not None:
        print(f"pickup-assemble: {conflict}", file=sys.stderr)
        return EXIT_USAGE
    i = 0
    while i < len(tail):
        tok = tail[i]
        if (payload := resolve_json_payload_flag(tail, i)).consumed:
            if payload.error is not None:
                print(f"pickup-assemble: {payload.error}", file=sys.stderr)
                return EXIT_USAGE
            decisions = payload.value
            i += payload.consumed
        else:
            print(f"pickup-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return EXIT_USAGE

    try:
        results = brief_multi(artifact_path, decisions)
    except _TransportFailure as exc:
        failure = _emit({
            "error": str(exc), "transport_failure": True,
            "narration": f"Could not compute a brief: {exc}.",
            "next_move": "Confirm the command is run from inside a git worktree, then retry.",
        }, EXIT_TRANSPORT_FAIL)
        print(json.dumps(failure.decision_object))
        return failure.exit_code
    except Exception as exc:  # noqa: BLE001 - structural backstop
        failure = _emit({
            "error": str(exc), "transport_failure": True,
            "narration": f"brief() raised an unexpected exception: {exc}.",
            "next_move": "Re-run; if this repeats, report the traceback.",
        }, EXIT_TRANSPORT_FAIL)
        print(json.dumps(failure.decision_object))
        return failure.exit_code

    if len(results) == 1:
        payload: Any = results[0].decision_object
        exit_code = results[0].exit_code
    else:
        payload = [r.decision_object for r in results]
        exit_code = max(r.exit_code for r in results)

    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
