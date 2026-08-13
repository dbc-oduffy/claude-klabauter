"""
coordinator_core.ops.session.reap — session.reap op (Class B cadence-gated reaper).

Purpose: Reap stale/orphaned session substrate from .git/coordinator-sessions/.
Three sub-reaps run; (i)/(ii) share a 12h .last-reap cadence gate, (iii) runs on
EVERY invocation (decoupled from the gate — Decision D1 shape (a), PM-ratified
2026-07-14 claim-lock-liveness plan § C4, see Cadence decoupling below):
  (i)  Stale sessions (last_activity > 24h inactive) → .archive/<sid>-<YYYY-MM-DD>/
  (ii) Stale agent dirs (touched.txt mtime > 24h) → .archive/_agents-<aid>-<YYYYMMDD>/
  (iii) Orphaned claim dirs (liveness-checked, TOCTOU re-read) → rm -rf

Class-B safety contract (NOT DR-211 git-archival — untracked .git/ substrate):
  - Per-record idempotency: if archive dest already exists, skip (no error).
  - Liveness: ONLY via resolve_live_session_ids / cs_claim_holder_live — NEVER raw pid.
  - TOCTOU: double liveness check immediately before rm on claim dirs
    (mirrors the bash oracle's cs_reap_stale_claims).
  - Fail-closed-to-keep: on any liveness-check error or meta-read failure,
    DEFER (keep the record) — untracked-substrate removal is NOT git-reversible;
    mirrors the bash oracle's "bias to life" posture.
  - .last-reap cadence gate: 12h minimum, implemented via file-mtime persistence.
    NOT flock — .last-reap is a long-lived persistence marker, not a short-held
    write-critical-section (session-state-contract.md § Negative-Spec).

Cadence decoupling (Decision D1, shape (a) — PM-ratified 2026-07-14
claim-lock-liveness plan § C4, docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md):
  sub-reap (iii) (orphaned claim dirs) is NOT gated by the shared 12h .last-reap
  marker — it runs at EVERY session-init (boot cadence), since dead claim-lock
  cleanup is the true residual of the archival-gate-liveness unification.  Only
  sub-reaps (i)/(ii) (stale sessions/agents) remain behind the 12h gate.  On the
  fast (iii)-only path, .last-reap is NOT touched — touching it there would
  silently reset the (i)/(ii) cadence window on every boot and starve stale
  session/agent cleanup.  session.reap is already invoked every session-init
  (boot_sweep.py:790); this decoupling is purely internal cadence-gate wiring,
  not a new caller — session.boot_sweep does NOT invoke session.reap directly
  (shape (b), folding claim-reap into boot_sweep, was REJECTED: it would mix
  Class-B untracked rm-rf into boot_sweep's DR-211 tracked-archival safety
  identity).

Self-registration: importing this module calls
register_op("session.reap", _handler) as a side-effect, and (C5)
register_op("session.reap_claims_for_repos", _handler_reap_claims_for_repos) — the
per-repo claim-reap primitive (target-root-parameterized, op scope "none"; see
"Per-repo claim-reap PRIMITIVE" section below).
Add this module to coordinator_core/ops/__init__.py (C5) to trigger registration.

Spec backlinks:
  - Plan: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C4
  - Port of: coordinator-session.sh (coordinator-claude e34f2484, 2026-07-22)
    cs_reap_stale, cs_reap_agents, cs_reap_stale_claims
  - session-state-contract.md § Negative-Spec: mkdir/flock decision (long-lived vs. ms-scope)

Negative-spec:
  - Does NOT use _common.archive_and_commit — substrate is untracked .git/coordinator-sessions/
    and no git commit is performed.
  - Does NOT commit to git — mutation is directory mv/rm only; DR-211 git bounds do NOT apply.
  - Does NOT use raw pid (kill -0 / ps -p / psutil.pid_exists) for liveness checks.
    The cs_reap_stale shell uses _cs_pid_alive as a belt-and-braces guard over 24h
    inactivity; that check is NOT ported because in-harness meta.json pid is always a
    dead hook $$ (plan anti-scope), and resolve_live_session_ids is the canonical key.
  - Does NOT use flock for .last-reap cadence gate — file-mtime persistence is correct
    for a long-lived 12h marker; flock is for short-held (ms) write-critical-section on
    POSIX (session-state-contract.md § Negative-Spec, PM-ratified 2026-07-06).
  - Does NOT treat params.repo_root as the path source — D3 check only.
  - Does NOT expose a dry_run/candidate_ids two-phase contract — this is not a fleet op;
    it has no cockpit-facing reviewer loop.
"""

from __future__ import annotations
import sys

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.liveness import cs_claim_holder_live, resolve_live_session_ids
from coordinator_core.ops.fleet._common import (
    _CLAIM_SUBDIRS,
    _sessions_dir,
    build_setup_error_result,
    check_repo_root,
)
from coordinator_core.session.claims import reconcile_dead_handoff_claim_frontmatter

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold constants (mirrors shell cs_reap_stale / cs_reap_agents)
# ---------------------------------------------------------------------------

_SESSION_STALE_SECONDS: int = 24 * 3600   # 24h inactivity → stale session
_AGENT_STALE_SECONDS: int = 24 * 3600     # 24h mtime → stale agent dir
_CADENCE_SECONDS: int = 12 * 3600         # 12h minimum between reap runs

# _CLAIM_SUBDIRS / _sessions_dir: imported above from ops.fleet._common (single
# shared source of truth with archive_handoffs.py's Check 4 — code-reviewer F1,
# 2026-07-14 claim-lock-liveness slice1 review). Re-exported via this module's
# namespace for existing test imports (coordinator_core.ops.session.reap.{_CLAIM_SUBDIRS,_sessions_dir}).


def _last_reap_path(sessions_dir: Path) -> Path:
    """Return the .last-reap cadence marker path: <sessions_dir>/.last-reap."""
    return sessions_dir / ".last-reap"


# ---------------------------------------------------------------------------
# Cadence gate — file-mtime persistence (NOT flock)
# ---------------------------------------------------------------------------

def _cadence_elapsed(sessions_dir: Path) -> bool:
    """Return True if >= 12h have elapsed since the last reap, or if marker is absent.

    Uses mtime of <sessions_dir>/.last-reap for persistence — NOT flock.
    Flock is for short-held (ms) write-critical-section locks; this 12h gate is a
    long-lived session-scoped persistence marker where mtime is the correct primitive
    (session-state-contract.md § Negative-Spec, PM-ratified 2026-07-06).

    Returns True (proceed with reap) on any OSError reading the marker.
    """
    marker = _last_reap_path(sessions_dir)
    if not marker.exists():
        return True  # never reaped — proceed
    try:
        mtime = marker.stat().st_mtime
        elapsed = datetime.now(tz=timezone.utc).timestamp() - mtime
        return elapsed >= _CADENCE_SECONDS
    except OSError:
        return True  # can't read marker — assume elapsed; proceed with reap


def _touch_last_reap(sessions_dir: Path) -> None:
    """Update .last-reap mtime to now (or create if absent)."""
    marker = _last_reap_path(sessions_dir)
    try:
        marker.touch()
    except OSError as exc:
        _LOG.warning("session.reap: could not update .last-reap marker at %s: %s", marker, exc)


# ---------------------------------------------------------------------------
# Timestamp / metadata helpers
# ---------------------------------------------------------------------------

def _now_utc_epoch() -> float:
    """Return current UTC time as a Unix timestamp float."""
    return datetime.now(tz=timezone.utc).timestamp()


def _today_str() -> str:
    """Return today as YYYY-MM-DD (UTC)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _today_compact() -> str:
    """Return today as YYYYMMDD (UTC) — used in .archive/_agents-<aid>-<YYYYMMDD>/."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d")


def _read_meta_json(session_dir: Path) -> Optional[dict]:
    """Read meta.json from a session directory; return None on any error.

    Returns None (not raises) on missing file, invalid JSON, or any OSError so
    callers can apply fail-closed-to-keep semantics on None.
    """
    meta_path = session_dir / "meta.json"
    try:
        text = meta_path.read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError, ValueError):
        print(f"skip: _read_meta_json: text = meta_path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def _parse_last_activity(meta: dict) -> float:
    """Return last_activity as UTC epoch float, or 0.0 on any parse failure.

    0.0 is the sentinel for "unknown timestamp" — callers must treat 0.0 as
    fail-closed-to-keep (mirrors shell cs_reap_stale: epoch=0 → skip reap).

    Normalises trailing 'Z' → '+00:00' for fromisoformat() compatibility on
    Python < 3.11, where datetime.fromisoformat does not accept 'Z'.
    """
    raw = (meta.get("last_activity") or "").strip()
    if not raw:
        return 0.0
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt.timestamp()
    except (ValueError, TypeError):
        print(f"skip: _parse_last_activity: if raw.endswith(\"Z\"): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0.0


# ---------------------------------------------------------------------------
# Sub-reaper (i): stale sessions
# ---------------------------------------------------------------------------

def _reap_stale_sessions(
    sessions_dir: Path,
    live_sids: frozenset,
    liveness_error: Optional[str],
) -> Tuple[List[str], List[dict], List[dict]]:
    """Sync: move stale session dirs (last_activity > 24h) to .archive/.

    Returns (reaped_sessions, deferred, failed).

    liveness_error: when non-None, resolve_live_session_ids failed before this call —
    all sessions are deferred (fail-closed-to-keep), no reaping occurs.

    Stale criterion: last_activity > 24h AND sid NOT in live_sids.
    Unknown timestamp (epoch=0) → defer (fail-closed-to-keep).
    Live session (in live_sids) → keep.
    Archive dest already exists → skip (per-record idempotency).
    """
    reaped: List[str] = []
    deferred: List[dict] = []
    failed: List[dict] = []

    if not sessions_dir.is_dir():
        return reaped, deferred, failed

    if liveness_error is not None:
        # Fail-closed: liveness resolution failed — defer ALL sessions.
        for sdir in sorted(sessions_dir.iterdir()):
            if sdir.is_dir() and not sdir.name.startswith("."):
                deferred.append({
                    "id": sdir.name,
                    "reason": f"resolve_live_session_ids failed — deferred (fail-closed): {liveness_error}",
                })
        return reaped, deferred, failed

    now_epoch = _now_utc_epoch()
    archive_root = sessions_dir / ".archive"

    for sdir in sorted(sessions_dir.iterdir()):
        if not sdir.is_dir():
            continue
        sid = sdir.name
        if sid.startswith("."):
            continue  # skip .archive, .agents, .last-reap sentinel

        # Liveness check: if the session registry says this sid is live, keep it.
        if sid in live_sids:
            continue  # live session — keep

        # Read meta.json to get last_activity.
        meta = _read_meta_json(sdir)
        if meta is None:
            # Can't read meta — ambiguous state; defer (fail-closed-to-keep).
            deferred.append({
                "id": sid,
                "reason": "meta.json unreadable — deferred (fail-closed-to-keep)",
            })
            continue

        last_activity_epoch = _parse_last_activity(meta)
        if last_activity_epoch == 0.0:
            # Unknown timestamp — defer (fail-closed-to-keep).
            # Mirrors shell cs_reap_stale: "epoch=0 → skip reap".
            deferred.append({
                "id": sid,
                "reason": "last_activity absent/unparseable — deferred (fail-closed-to-keep)",
            })
            continue

        inactive_for = now_epoch - last_activity_epoch
        # Clock-skew clamp (mirrors shell cs_reap_stale NTP/VM-resume guard).
        if inactive_for < 0:
            inactive_for = 0.0

        if inactive_for <= _SESSION_STALE_SECONDS:
            continue  # active within threshold — keep

        # Review: code-reviewer F1 — removed dead re-check. live_sids is a frozenset
        # captured once via resolve_live_session_ids; it cannot be updated by a
        # concurrent session-init. The 24h _SESSION_STALE_SECONDS threshold is the
        # actual freshness guard: a brand-new session has a recent last_activity and
        # is kept by the inactivity check above, not by a liveness re-read here.

        # Build archive destination: <sessions_dir>/.archive/<sid>-<YYYY-MM-DD>
        archive_dest = archive_root / f"{sid}-{_today_str()}"

        # Per-record idempotency: if dest already exists, skip (mirrors cs_archive).
        if archive_dest.exists():
            _LOG.debug(
                "session.reap: stale session %s already archived at %s — skip (idempotent)",
                sid, archive_dest,
            )
            continue

        # Move (mirrors cs_archive shell mv).
        try:
            archive_root.mkdir(parents=True, exist_ok=True)
            sdir.rename(archive_dest)
            _LOG.info("session.reap: reaped stale session %s → %s", sid, archive_dest)
            reaped.append(sid)
        except OSError as exc:
            if archive_dest.exists():
                # Concurrent reaper already moved it — idempotent (mirrors shell mv/ENOENT note).
                _LOG.debug(
                    "session.reap: concurrent reap of session %s (dest now exists) — skip",
                    sid,
                )
            else:
                failed.append({"id": sid, "reason": f"mv failed: {exc}"})

    return reaped, deferred, failed


# ---------------------------------------------------------------------------
# Sub-reaper (ii): stale agent dirs
# ---------------------------------------------------------------------------

def _reap_stale_agents(
    sessions_dir: Path,
) -> Tuple[List[str], List[dict], List[dict]]:
    """Sync: move stale agent dirs (touched.txt mtime > 24h) to .archive/.

    Returns (reaped_agents, deferred, failed).

    Stale criterion: touched.txt mtime > 24h.
    touched.txt absent → rmdir the empty agent dir (mirrors shell cs_reap_agents).
    Unreadable mtime → defer (fail-closed-to-keep).
    Archive dest already exists → skip (per-record idempotency).
    """
    reaped: List[str] = []
    deferred: List[dict] = []
    failed: List[dict] = []

    agents_base = sessions_dir / ".agents"
    if not agents_base.is_dir():
        return reaped, deferred, failed

    now_epoch = _now_utc_epoch()
    archive_root = sessions_dir / ".archive"

    for adir in sorted(agents_base.iterdir()):
        if not adir.is_dir():
            continue
        aid = adir.name
        if aid.startswith("."):
            continue

        touched = adir / "touched.txt"
        if not touched.exists():
            # Empty agent dir — rmdir best-effort (mirrors shell cs_reap_agents).
            try:
                adir.rmdir()
                _LOG.debug("session.reap: removed empty agent dir %s", aid)
            except OSError:
                pass  # not empty or concurrent — best-effort
            continue

        # Read touched.txt mtime for staleness check.
        try:
            mtime = touched.stat().st_mtime
        except OSError as exc:
            print(f"skip: _reap_stale_agents: mtime = touched.stat().st_mtime failed: {exc}", file=sys.stderr)
            deferred.append({
                "id": aid,
                "reason": f"touched.txt stat failed — deferred (fail-closed-to-keep): {exc}",
            })
            continue

        elapsed = now_epoch - mtime
        if elapsed < 0:
            elapsed = 0.0  # clock-skew clamp

        if elapsed <= _AGENT_STALE_SECONDS:
            continue  # agent still active

        # Build archive destination: <sessions_dir>/.archive/_agents-<aid>-<YYYYMMDD>
        archive_dest = archive_root / f"_agents-{aid}-{_today_compact()}"

        if archive_dest.exists():
            _LOG.debug(
                "session.reap: stale agent %s already archived at %s — skip (idempotent)",
                aid, archive_dest,
            )
            continue

        # Move (mirrors shell: mv "$adir" "$target" || rm -rf "$adir").
        try:
            archive_root.mkdir(parents=True, exist_ok=True)
            adir.rename(archive_dest)
            _LOG.info("session.reap: reaped stale agent %s → %s", aid, archive_dest)
            reaped.append(aid)
        except OSError as exc:
            if archive_dest.exists():
                # Concurrent reaper moved it — idempotent.
                _LOG.debug(
                    "session.reap: concurrent reap of agent %s (dest now exists) — skip", aid
                )
            else:
                # Move failed and dest absent — fallback rm (mirrors shell || rm -rf "$adir").
                try:
                    shutil.rmtree(str(adir))
                    _LOG.info("session.reap: reaped stale agent %s (fallback rm) after mv err: %s", aid, exc)
                    reaped.append(aid)
                except OSError as rm_exc:
                    failed.append({"id": aid, "reason": f"mv failed ({exc}); rm fallback failed: {rm_exc}"})

    return reaped, deferred, failed


# ---------------------------------------------------------------------------
# Sub-reaper (iii): orphaned claim dirs
# ---------------------------------------------------------------------------

def _reap_orphaned_claims(
    sessions_dir: Path,
) -> Tuple[List[str], List[dict], List[dict]]:
    """Sync: rm -rf orphaned claim dirs after double liveness check (TOCTOU guard).

    Returns (reaped_claims, deferred, failed).

    Claim dirs: <sessions_dir>/{handoff,memo,plan}-claims/<basename>/
    Liveness: cs_claim_holder_live (NEVER raw pid).
    TOCTOU: two sequential cs_claim_holder_live calls immediately before rm — if either
    call returns live (concurrent pickup took over), skip (mirrors shell :855/:861).
    Fail-closed-to-keep: any exception from cs_claim_holder_live → defer; never reap.
    rm idempotency: if claim_dir already absent when rm fires, treat as reaped (concurrent
    reaper won the race; mirrors shell rm -rf idempotency on BSD/macOS note).
    """
    reaped: List[str] = []
    deferred: List[dict] = []
    failed: List[dict] = []

    for subdir_name in _CLAIM_SUBDIRS:
        claims_dir = sessions_dir / subdir_name
        if not claims_dir.is_dir():
            continue

        for claim_dir in sorted(claims_dir.iterdir()):
            if not claim_dir.is_dir():
                continue
            claim_name = claim_dir.name
            claim_id = f"{subdir_name}/{claim_name}"

            # First liveness check — fail-closed-to-keep on any exception.
            try:
                live1 = cs_claim_holder_live(str(claim_dir))
            except Exception as exc:
                deferred.append({
                    "id": claim_id,
                    "reason": f"cs_claim_holder_live (read-1) raised — deferred (fail-closed): {exc}",
                })
                continue

            if live1:
                continue  # holder is live — keep

            # TOCTOU re-read: a concurrent pickup may have taken over between the two calls.
            # If the new holder is live, the second check returns True → skip (keep).
            try:
                live2 = cs_claim_holder_live(str(claim_dir))
            except Exception as exc:
                deferred.append({
                    "id": claim_id,
                    "reason": f"cs_claim_holder_live (TOCTOU re-read) raised — deferred (fail-closed): {exc}",
                })
                continue

            if live2:
                continue  # concurrent takeover confirmed live — keep

            # Reap/ship bug fix 1 (docs/plans/2026-07-24-g4-execute-pipeline-
            # two-repo-rebuild.md § M3a, the Director of Engineering Finding 5): this automated
            # reaper shares the identical lock-only-clear frontmatter drift
            # clear_claim_if_dead had — reconcile the handoff frontmatter
            # BEFORE the rmtree below (see reconcile_dead_handoff_claim_
            # frontmatter's docstring for the unconsume-first crash-
            # disposition rationale). Never raises; skip-and-log on any
            # precondition miss.
            if subdir_name == "handoff-claims":
                reconcile_dead_handoff_claim_frontmatter(claim_name, sessions_dir)

            # Both liveness checks agree: holder not live → reap.
            # shutil.rmtree handles non-empty claim dirs safely.
            try:
                shutil.rmtree(str(claim_dir))
                _LOG.info("session.reap: reaped orphaned claim %s", claim_id)
                reaped.append(claim_id)
            except OSError as exc:
                if not claim_dir.exists():
                    # Concurrent reaper removed it first — idempotent; still count as reaped.
                    _LOG.debug(
                        "session.reap: concurrent reap of claim %s (already absent) — ok",
                        claim_id,
                    )
                    reaped.append(claim_id)
                else:
                    failed.append({"id": claim_id, "reason": f"rm failed: {exc}"})

    return reaped, deferred, failed


# ---------------------------------------------------------------------------
# Per-repo claim-reap PRIMITIVE (C5, shape (b) — target-root-parameterized)
# ---------------------------------------------------------------------------
#
# Spec backlink: pln-claim-lock-liveness-unificatio-c135ac
# § C5 (PM-ratified 2026-07-14; direction resolved by the Staff Engineer review, "adopt split,
# no the Director of Engineering round"). DR-211 does NOT govern this primitive — session.reap is declared
# outside DR-211's git-archival bounds (see module docstring). Governing authority:
# tri-plane ownership boundary + per-repo-self-sweep convention
# (coordinator_roots.py:6-8; initiative-FK-backfill precedent).
#
# Negative-spec (C5):
#   - Does NOT enumerate working-repos.yaml — claude-klabauter has no fleet census here.
#   - Does NOT walk/rm-rf into any sibling repo the caller did not explicitly name
#     via target_root/target_roots. Fleet-level fan-out over this primitive across
#     the full working-repos.yaml census is the META-REPO's job (paired coordinator-claude
#     return-memo commitment), not built in this plan.
#   - Does NOT re-implement the reaper predicate — delegates to
#     _reap_orphaned_claims (same TOCTOU double-check + fail-closed-to-keep
#     liveness safety as the cwd path). A live holder's claim is NEVER reaped.


def _reap_claims_for_target(target_root: Path) -> Tuple[List[str], List[dict], List[dict]]:
    """Sync: per-repo claim-reap PRIMITIVE — reap orphaned claim dirs under an
    explicit *target_root* repo's coordinator-sessions substrate.

    Op scope "none" per claude-klabauter's target-resolution convention (fleet-generic ops
    use scope "none" with an explicit target_root param, NOT common_dir —
    mirrors cartography.churn/cartography.tree precedent): target_root may be
    ANY repo, not just the caller's own dispatching tree. Derives common_dir via
    git_common_dir(target_root) (a subprocess call scoped to target_root, works
    for any git repo) then reuses _reap_orphaned_claims — same TOCTOU double
    liveness check and fail-closed-to-keep guarantee; no parallel reaper
    predicate is (re-)implemented here.

    Returns (reaped_claims, deferred, failed) — same shape as
    _reap_orphaned_claims. A target_root with no coordinator-sessions substrate
    yet (never touched by this session-substrate machinery) returns
    ([], [], []) — not an error.
    """
    try:
        common_dir = git_common_dir(target_root)
    except RuntimeError as exc:
        print(f"skip: _reap_claims_for_target: common_dir = git_common_dir(target_root) failed: {exc}", file=sys.stderr)
        return [], [], [{
            "id": str(target_root),
            "reason": f"git_common_dir failed for target_root: {exc}",
        }]

    sessions_dir = _sessions_dir(common_dir)
    if not sessions_dir.is_dir():
        return [], [], []

    return _reap_orphaned_claims(sessions_dir)


def _reap_session_touched_repos(target_roots: List[Path]) -> dict:
    """Sync: reap orphaned claims across exactly the session-touched baton repos.

    Shape (b) coverage default (C5): every foreign `/pickup` this session
    performed already resolves a BATON_REPO; the caller collects those touched
    repo roots and passes them here as *target_roots*. Reaps ONLY the given
    repos and no others — claude-klabauter does NOT enumerate working-repos.yaml and does
    NOT expand coverage beyond what this session legitimately touched.

    Returns {<target_root str>: {"reaped_claims": [...], "deferred": [...],
    "failed": [...]}}, keyed per repo so a caller can distinguish per-repo
    outcomes.
    """
    per_repo: dict = {}
    for target_root in target_roots:
        reaped, deferred, failed = _reap_claims_for_target(target_root)
        per_repo[str(target_root)] = {
            "reaped_claims": reaped,
            "deferred": deferred,
            "failed": failed,
        }
    return per_repo


# ---------------------------------------------------------------------------
# Result envelope builders
# ---------------------------------------------------------------------------

def _build_result(
    *,
    cadence_gated: bool,
    reaped_sessions: List[str],
    reaped_agents: List[str],
    reaped_claims: List[str],
    deferred: List[dict],
    failed: List[dict],
) -> dict:
    """Build the session.reap result envelope.

    exit_code:
      0 — success (failed[] empty).
      2 — partial; failed[] non-empty; per-item truth enumerated.
      (exit_code:1 reserved for setup errors — use _build_error_result.)
    """
    return {
        "exit_code": 2 if failed else 0,
        "cadence_gated": cadence_gated,
        "reaped_sessions": reaped_sessions,
        "reaped_agents": reaped_agents,
        "reaped_claims": reaped_claims,
        "deferred": deferred,
        "failed": failed,
    }


def _build_error_result(reason: str) -> dict:
    """Build a setup-error result (exit_code:1) — pre-handler failure."""
    _LOG.error("session.reap setup error: %s", reason)
    return {
        "exit_code": 1,
        "cadence_gated": False,
        "reaped_sessions": [],
        "reaped_agents": [],
        "reaped_claims": [],
        "deferred": [],
        "failed": [],
    }


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------

@register_op("session.reap")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.reap — cadence-gated reaper for .git/coordinator-sessions/ substrate.

    Three sub-reaps run behind a single 12h .last-reap cadence gate:
      (i)  Stale sessions (last_activity > 24h) → .archive/<sid>-YYYY-MM-DD/
      (ii) Stale agent dirs (touched.txt mtime > 24h) → .archive/_agents-<aid>-YYYYMMDD/
      (iii) Orphaned claim dirs (two liveness checks, TOCTOU re-read) → rm -rf

    Class-B op: no git commit; substrate is untracked .git/coordinator-sessions/.
    Fail-closed-to-keep: any liveness-check error or ambiguity defers the record.

    params:
      force (bool, optional): skip the 12h cadence gate and reap unconditionally.
                               Default: false.
      repo_root (str, optional): D3 consistency check only — NOT the path source.

    repo_root handler arg: the git common dir supplied by the engine
    (_OP_KEY_SCOPE = "common_dir").  Sessions dir is <common_dir>/coordinator-sessions/
    (inside the git dir, not the worktree — no worktree derivation needed).

    Wire output (session.reap-specific — NOT the frozen fleet envelope):
      exit_code     int   0=ok, 1=setup-error, 2=partial (failed[] non-empty)
      cadence_gated bool  true iff the 12h gate fired and all sub-reaps were skipped
      reaped_sessions  [str]   session IDs successfully moved to .archive/
      reaped_agents    [str]   agent IDs successfully moved to .archive/
      reaped_claims    [str]   claim paths (subdir/name) successfully rm'd
      deferred         [dict]  records kept due to liveness ambiguity / fail-closed
      failed           [dict]  records that could not be moved/rm'd (mv/rm error)

    Cadence decoupling (Decision D1, shape (a) — PM-ratified 2026-07-14
    claim-lock-liveness plan § C4): sub-reap (iii) (orphaned claim dirs) runs on
    EVERY invocation — it is NOT gated behind the 12h .last-reap marker.  Only
    sub-reaps (i)/(ii) (stale sessions/agents) remain behind the cadence gate.
    `cadence_gated` reflects whether (i)/(ii) were skipped for this reason; it
    does NOT mean sub-reap (iii) was skipped too.
    """
    if repo_root is None:
        _LOG.error("session.reap: repo_root handler arg is None")
        return _build_error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root

    # D3 repo_root consistency check (same pattern as fleet ops; check only, not path source).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _build_error_result(mismatch)

    # sessions_dir derives directly from common_dir — no worktree derivation needed.
    # Substrate lives inside .git/coordinator-sessions/ (inside the git dir), not the
    # worktree.  main_worktree_root() is NOT called here: it is a pure path calculation
    # (returns common_dir.parent) that does not validate or raise on any input.
    # Review: code-reviewer F2 — removed discarded main_worktree_root() call whose
    # "validates" comment was incorrect; sessions_dir derives from common_dir directly.
    sessions_dir = _sessions_dir(common_dir)

    if not sessions_dir.is_dir():
        _LOG.debug("session.reap: sessions dir absent at %s — no-op", sessions_dir)
        return _build_result(
            cadence_gated=False,
            reaped_sessions=[],
            reaped_agents=[],
            reaped_claims=[],
            deferred=[],
            failed=[],
        )

    # Cadence gate: (i)/(ii) skip if < 12h since last reap (unless force=true).
    # Sub-reap (iii) is decoupled from this gate (Decision D1 shape (a) — see module
    # docstring "Cadence decoupling"): it runs on the fast path below regardless.
    force = bool(params.get("force", False))
    if not force and not _cadence_elapsed(sessions_dir):
        _LOG.debug(
            "session.reap: cadence gate active (< %dh since last reap) — "
            "skipping sub-reaps (i)/(ii); sub-reap (iii) still runs (boot cadence)",
            _CADENCE_SECONDS // 3600,
        )
        # Fast path: ONLY sub-reap (iii) runs.  .last-reap is deliberately NOT
        # touched here — touching it would reset the (i)/(ii) cadence window on
        # every boot and starve stale session/agent cleanup (see module docstring
        # "Cadence decoupling" FOOTGUN).
        reaped_claims, deferred_claims, failed_claims = await asyncio.to_thread(
            _reap_orphaned_claims, sessions_dir
        )
        return _build_result(
            cadence_gated=True,
            reaped_sessions=[],
            reaped_agents=[],
            reaped_claims=reaped_claims,
            deferred=deferred_claims,
            failed=failed_claims,
        )

    # --- Sub-reap (i): stale sessions ---
    # resolve_live_session_ids is a sync subprocess bridge — wrap in asyncio.to_thread
    # (mirrors archive_handoffs.py DR-211 D4 async mandate for the same call).
    liveness_error: Optional[str] = None
    try:
        live_sids: frozenset = await asyncio.to_thread(resolve_live_session_ids)
    except Exception as exc:
        # Liveness resolution failed entirely — all sessions must be deferred (fail-closed).
        liveness_error = str(exc)
        live_sids = frozenset()
        _LOG.error(
            "session.reap: resolve_live_session_ids failed — sub-reap (i) will defer all: %s",
            exc,
        )

    reaped_sessions, deferred_sessions, failed_sessions = await asyncio.to_thread(
        _reap_stale_sessions, sessions_dir, live_sids, liveness_error
    )

    # --- Sub-reap (ii): stale agent dirs ---
    # No liveness call needed — agent staleness is time-only (touched.txt mtime).
    reaped_agents, deferred_agents, failed_agents = await asyncio.to_thread(
        _reap_stale_agents, sessions_dir
    )

    # --- Sub-reap (iii): orphaned claim dirs ---
    # cs_claim_holder_live calls are sync subprocess bridges — wrapped inside the sync
    # helper itself, which runs in asyncio.to_thread.  The TOCTOU double-check runs
    # sequentially within the same thread to preserve the re-read ordering.
    reaped_claims, deferred_claims, failed_claims = await asyncio.to_thread(
        _reap_orphaned_claims, sessions_dir
    )

    # Update .last-reap cadence marker to now (regardless of partial failures).
    # Only reached on a full run (force OR cadence-elapsed) — the fast (iii)-only
    # path above returns before this point and never touches the marker.
    _touch_last_reap(sessions_dir)

    return _build_result(
        cadence_gated=False,
        reaped_sessions=reaped_sessions,
        reaped_agents=reaped_agents,
        reaped_claims=reaped_claims,
        deferred=deferred_sessions + deferred_agents + deferred_claims,
        failed=failed_sessions + failed_agents + failed_claims,
    )


# ---------------------------------------------------------------------------
# Op handler: session.reap_claims_for_repos (C5 — per-repo claim-reap primitive
# + session-touched-baton-repos coverage default)
# ---------------------------------------------------------------------------

@register_op("session.reap_claims_for_repos")
async def _handler_reap_claims_for_repos(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """session.reap_claims_for_repos — per-repo claim-reap PRIMITIVE (C5).

    Fleet-generic, op scope "none" per claude-klabauter's target-resolution convention
    (explicit target_root param, any repo — mirrors cartography.churn /
    cartography.tree precedent, NOT common_dir). `repo_root` is unused (always
    None for scope-"none" ops); all target roots are caller-supplied via the
    `target_roots` wire param.

    Ships shape (b) of C5 (PM-ratified 2026-07-14 claim-lock-liveness plan,
    the Staff Engineer-review-resolved "adopt split, no the Director of Engineering round"): the PRIMITIVE is
    target-root-parameterized (one call may name any set of repos), and the
    session-touched-baton-repos coverage default is DRIVEN by the caller
    passing exactly the baton repos this session touched (each foreign
    `/pickup` already resolves BATON_REPO) — claude-klabauter does NOT enumerate
    working-repos.yaml and does NOT expand coverage to sibling repos this
    session did not legitimately touch.

    params:
      target_roots (list[str], required): repo roots to reap orphaned claim
                                           dirs under. Each is validated
                                           (non-blank string, resolves to an
                                           existing path via Path.resolve(strict=True))
                                           before use — see Finding-1 note below.

    Wire output (session.reap_claims_for_repos-specific — NOT the frozen fleet
    envelope):
      exit_code  int   0=ok (no per-repo failures), 1=setup-error (bad params),
                       2=partial (a per-repo failed[] entry non-empty)
      results    dict  {<target_root str>: {"reaped_claims": [...],
                        "deferred": [...], "failed": [...]}}, one entry per
                        target_root actually processed (invalid roots are
                        reported in "errors" instead, not silently dropped).
      errors     list[dict]  {"target_root": <raw str>, "reason": <str>} for
                              any target_root that was empty/non-string/blank
                              or failed to resolve to an existing path.

    Review: code-reviewer F1 (2026-07-14 slice2) — this previously called
    cartography._guard.path_guard(raw_target, ".") as a documented "security
    guard", but joining "." onto target_root always collapses back to
    target_root itself, so the containment check could never actually fail
    except on an OSError during resolution — it provided no real traversal
    protection. The REAL gate for this op is _reap_claims_for_target's
    git_common_dir(target_root) call, which fails (reported as a per-repo
    failed[] entry) for any path that isn't a real git repo. Replaced with an
    explicit blank/whitespace rejection + Path.resolve(strict=True) fail-fast
    on a non-existent path (cheaper than waiting for the git subprocess to
    fail) — meaningful validation instead of a vestigial no-op, while
    git_common_dir remains the actual containment/validity gate.

    Same TOCTOU + fail-closed-to-keep + liveness safety as the cwd-scoped
    session.reap sub-reap (iii) — delegates to _reap_orphaned_claims via
    _reap_claims_for_target; no parallel reaper predicate.
    """
    raw_targets = params.get("target_roots")
    if not raw_targets or not isinstance(raw_targets, list):
        _LOG.error(
            "session.reap_claims_for_repos: target_roots must be a non-empty list"
        )
        return {
            "exit_code": 1,
            "results": {},
            "errors": [{
                "target_root": raw_targets,
                "reason": "target_roots must be a non-empty list of repo root strings",
            }],
        }

    guarded_targets: List[Path] = []
    errors: List[dict] = []
    for raw_target in raw_targets:
        if not raw_target or not isinstance(raw_target, str):
            errors.append({
                "target_root": raw_target,
                "reason": "target_root entry must be a non-empty string",
            })
            continue
        if not raw_target.strip():
            errors.append({
                "target_root": raw_target,
                "reason": "target_root entry must not be blank/whitespace-only",
            })
            continue
        try:
            guarded_targets.append(Path(raw_target).resolve(strict=True))
        except OSError as exc:
            errors.append({
                "target_root": raw_target,
                "reason": f"target_root does not resolve to an existing path: {exc}",
            })

    results = await asyncio.to_thread(_reap_session_touched_repos, guarded_targets)

    any_failed = any(entry["failed"] for entry in results.values()) or bool(errors)
    return {
        "exit_code": 2 if any_failed else 0,
        "results": results,
        "errors": errors,
    }
