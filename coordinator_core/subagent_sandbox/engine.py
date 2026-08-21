"""
coordinator_core.subagent_sandbox.engine — resolver + policy-load module for
the surviving subagent-sandbox provision/report path and the bash_guards
resolvers.

DR-058 removed the subagent write-outside-sandbox PreToolUse DENY enforcement
(the two-tier confinement matrix) as friction-over-EM-intent; the hard-deny
splice was excised from ``coordinator_core.write_guards.engine`` in commit
0998c6a6. This module is what survived that cut: it is no longer an
enforcement engine, it is the shared resolver + policy-load layer two live
consumers still depend on:

  - ``coordinator_core.bash_guards._helpers`` re-exports ``resolve_git_root``,
    ``resolve_effective_types``, ``_canonical_agent_id``, and
    ``_read_backpointer_subagent_type`` from here (the "tiny re-export shim"
    that gives the bash_guards package one resolver import site) so its own
    identity/confinement checks (``block_reviewer_bash_outside_allowlist``,
    ``block_subagent_plan_body_bash_write``, ``block_subagent_destructive_action``)
    can determine which agent/session issued a Bash call without re-deriving
    the OR-resolver.
  - ``coordinator_core.subagent_sandbox.provision_report`` imports
    ``load_policy``, ``resolve_effective_types``, and ``resolve_git_root``
    from here to decide, at spawn time, whether an agent/subagent type has
    opted into a ``report_sidecar`` run-report doc, and to resolve the git
    root the doc gets written under.

Spec backlink: pln-claude-klabauter-subagent-sandbox-enforc-62cc03
                (original enforcement engine, now retired)
Removal: DoE DR-058, commit 0998c6a6 (write_guards splice excision)
Contract: coordinator_core/subagent_sandbox/CONTRACT.md

Negative-spec:
  - Does NOT evaluate ALLOW/DENY for any tool call — that decision matrix was
    removed with DR-058. This module resolves identity/policy INPUTS only;
    it makes no enforcement decision itself.
  - Does NOT vendor or copy the policy YAML — reads it fresh from the injected
    path every invocation (single source of truth with DoE's policy file).
  - Does NOT key confined-vs-exempt on model tier — ``subagent_type`` string
    membership ONLY; lookup-miss is simply not-eligible (no report sidecar
    provisioned), never a model-tier fallback (retained for
    ``report_sidecar`` eligibility, the one surviving membership check).
  - Does NOT run a resident daemon — stateless/cold, spawn-per-call (DR-215).
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

#: agent_id format guards (reference hook lines 96-130).
_BARE_HEX_RE = re.compile(r"^[a-f0-9]{12,}$")
_NAMED_TEAMMATE_RE = re.compile(r"^a.+-[a-f0-9]{16}$")

#: em-session-id.txt back-pointer content format guard (reference hook line 186).
_SESSION_ID_FORMAT_RE = re.compile(r"^[a-zA-Z0-9_-]{3,}$")

#: Best-effort default policy path fallback (the Staff Engineer F3 — no _cc_root resolver
#: chain exists in coordinator_core today; this is deliberately weak and
#: fails open at the caller when it doesn't resolve).
_DEFAULT_POLICY_ENV_CANDIDATES = ("CLAUDE_PLUGIN_ROOT",)
_DEFAULT_POLICY_RELATIVE = "subagent-sandbox-policy.yaml"

#: Env var the DoE shim injects the primary policy path through.
POLICY_ENV_VAR = "SUBAGENT_SANDBOX_POLICY"


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

class Policy:
    """A loaded (or empty, fail-open) subagent-sandbox policy.

    ``report_sidecar`` is a ``subagent_type`` string set (exact match, no
    globbing) — the only surviving policy field after DR-058 removed the
    ``confined``/``exempt``/``sanctioned_dirs`` enforcement fields.
    ``provision_report`` consults it to decide spawn-time eligibility for a
    run-report sidecar doc.

    ``bash_policy`` is a DECLARATION-ONLY field: a mapping of exact
    ``subagent_type`` strings to a mapping describing that agent type's
    allowed Bash surface (Bash only — no write/edit leg, ever). This loader
    only reads and exposes the mapping; it makes no ALLOW/DENY decision and
    is not consumed by any enforcement engine (DR-058 excised that splice —
    see module docstring). A later chunk wires the actual bash_guards
    consumer; adding that wiring here would re-create exactly the splice
    DR-058 removed.

    ``report_type_map`` is a mapping of exact ``subagent_type`` string to the
    ``provision_report --type`` template that agent type's sidecar should use.
    It is ADDITIVE on top of ``report_sidecar`` and does not restructure it: an
    agent type must be sidecar-eligible before a template type means anything.
    Consumed by ``coordinator/bin/provision-sidecar.py`` so a vehicle that
    bypasses the Agent-tool hook (a Workflow ``agent()`` call) still provisions
    the right template rather than falling through to the legacy run-report
    shape. A lookup-miss is not an error — see that CLI for the resolution
    order.
    """

    __slots__ = ("report_sidecar", "bash_policy", "report_type_map")

    def __init__(
        self,
        report_sidecar: Optional[Iterable[str]] = None,
        bash_policy: Optional[Dict[str, Any]] = None,
        report_type_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.report_sidecar = set(report_sidecar or ())
        self.bash_policy = dict(bash_policy or {})
        self.report_type_map = dict(report_type_map or {})

    @property
    def is_empty(self) -> bool:
        return not self.report_sidecar


def _empty_policy() -> Policy:
    return Policy()


def _resolve_default_policy_path() -> Optional[Path]:
    """Best-effort ``_cc_root``-style default fallback (the Staff Engineer F3).

    No genuine ``_cc_root`` resolver chain exists in ``coordinator_core`` —
    this only checks ``CLAUDE_PLUGIN_ROOT`` (the same env var the reference
    bash hook falls back to) for a co-located policy file. Any failure to
    resolve is silently absorbed by the caller (``load_policy`` returns an
    empty, fail-open ``Policy``) — this function never raises.
    """
    for env_var in _DEFAULT_POLICY_ENV_CANDIDATES:
        root = os.environ.get(env_var)
        if not root:
            continue
        candidate = Path(root) / _DEFAULT_POLICY_RELATIVE
        if candidate.is_file():
            return candidate
    return None


def load_policy(policy_path: Optional[str] = None) -> Policy:
    """Load the subagent-sandbox policy via ``yaml.safe_load``.

    Resolution order:
      1. ``policy_path`` (injected explicitly — the CLI ``--policy`` arg).
      2. ``SUBAGENT_SANDBOX_POLICY`` env var (the shim's primary injection —
         only DoE knows its plugin root; this is the expected common case).
      3. Best-effort ``_cc_root``-style default (``_resolve_default_policy_path``).

    Any absent/unreadable/malformed file at any of the above, or the complete
    absence of a resolvable path, returns an empty ``Policy`` (fail-open,
    row 5). Never raises. Never vendors/copies the file — reads it fresh from
    disk on every call.
    """
    candidates: List[Optional[str]] = [
        policy_path,
        os.environ.get(POLICY_ENV_VAR),
    ]
    resolved: Optional[Path] = None
    for candidate in candidates:
        if candidate:
            resolved = Path(candidate)
            break
    if resolved is None:
        resolved = _resolve_default_policy_path()
    if resolved is None:
        return _empty_policy()

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError:
        return _empty_policy()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return _empty_policy()

    if not isinstance(data, dict):
        return _empty_policy()

    # Unknown/removed keys (e.g. a YAML that still carries the DR-058-removed
    # confined/exempt/sanctioned_dirs fields, pending DoE's lockstep YAML
    # strip) are silently ignored here -- .get() on a dict never raises for
    # an absent or surplus key, so reading either an old-shape or new-shape
    # policy file is equally harmless.
    report_sidecar = data.get("report_sidecar") or []
    if not isinstance(report_sidecar, list):
        report_sidecar = []

    # bash_policy: mapping of exact subagent_type -> allowed-Bash-surface
    # mapping (declaration only, see Policy docstring). A non-dict top-level
    # value, or a non-dict per-key value, is silently dropped rather than
    # raising or blocking a spawn -- same fail-open posture as
    # report_sidecar's wrong-typed-value handling above.
    bash_policy_raw = data.get("bash_policy") or {}
    if not isinstance(bash_policy_raw, dict):
        bash_policy_raw = {}
    bash_policy = {
        key: value
        for key, value in bash_policy_raw.items()
        if isinstance(value, dict)
    }

    # report_type_map: mapping of exact subagent_type -> template type name.
    # Same fail-open posture as the two keys above, applied at BOTH levels: a
    # non-dict top-level value voids the whole map, and an individual entry
    # whose key or value is not a string is dropped on its own rather than
    # voiding its siblings. Values are NOT validated against TEMPLATE_TYPES
    # here -- this loader does not import provision_report (that module imports
    # THIS one), and an unknown type name already degrades safely at the point
    # of use, where _build_doc_text falls back to the run-report template.
    report_type_map_raw = data.get("report_type_map") or {}
    if not isinstance(report_type_map_raw, dict):
        report_type_map_raw = {}
    report_type_map = {
        key: value
        for key, value in report_type_map_raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }

    return Policy(
        report_sidecar=report_sidecar,
        bash_policy=bash_policy,
        report_type_map=report_type_map,
    )


# ---------------------------------------------------------------------------
# Git root resolution
# ---------------------------------------------------------------------------

class _GitRootResolutionFailed(Exception):
    """Internal-only signal so ``functools.lru_cache`` does NOT memoize a
    failed ``resolve_git_root()`` resolution — ``lru_cache`` never caches a
    call that raises. Mirrors ``session.core._SessionsDirResolutionFailed``:
    a failure (not a git repo, git missing, transient spawn error) must not
    poison the cache for the rest of the process the way a successful
    resolution legitimately can (see ``resolve_git_root`` docstring)."""


@functools.lru_cache(maxsize=64)
def _resolve_git_root_cached(cwd: str) -> str:
    result = _resolve_git_root_uncached(cwd)
    if result is None:
        raise _GitRootResolutionFailed(cwd)
    return result


def reset_resolve_git_root_cache() -> None:
    """Test/diagnostic escape hatch — clears the process-local
    ``resolve_git_root()`` cache. Call this in test teardown/setup for any
    test that resolves a git root at a path that could collide with a cache
    entry left behind by an earlier test in the same process (e.g. reusing a
    fixed path across parametrized runs, or re-initializing a repo at a path
    a prior test already resolved); ``tmp_path`` itself is already unique per
    test so ordinary tests do not need this."""
    _resolve_git_root_cached.cache_clear()


def _resolve_git_root_uncached(cwd: Optional[str] = None) -> Optional[str]:
    """Uncached ``git rev-parse --show-toplevel`` spawn — the sole place this
    module shells out to resolve a git root. Shared by ``resolve_git_root``'s
    ``cwd is None`` branch and by ``_resolve_git_root_cached``; see
    ``resolve_git_root`` docstring for the caching policy layered on top of
    this.

    Mirrors the reference hook's ``git rev-parse --show-toplevel`` fallback
    (lines 141-148) — deliberately fail-open on any resolution failure rather
    than raising, since the engine cannot locate the back-pointer chain or
    normalize paths without a root, and failing closed here would over-block
    every unmarked subagent.

    Review: code-reviewer F10 — the reference hook tries a `_cs_git_root`
    bash-library helper first, falling back to bare `git rev-parse` only if
    that helper isn't loaded. `_cs_git_root` is DoE-bash-specific with no
    Python equivalent in coordinator_core and is deliberately not ported;
    bare `git rev-parse --show-toplevel` is the full contract on this side.

    NOT converted to `coordinator_core.git.repo_root`'s shared seam --
    attempted and reverted. That seam walks for a `.git` entry before
    falling back to a spawn, which is correct for its own callers but
    breaks this module's documented `resolve_git_root` caching contract:
    its three pinning tests
    (`test_resolve_git_root_cache_hits_on_repeat_same_cwd`,
    `test_resolve_git_root_cwd_none_never_cached`,
    `test_resolve_git_root_failed_resolution_is_not_memoized`) count actual
    `git` subprocess spawns and require ALWAYS spawning (never walking), so
    the walk silently satisfies calls with zero spawns and the counts no
    longer match. See `coordinator_core/git/repo_root.py` module docstring
    for the seam's own (different, and for its callers correct) caching
    policy.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            # House value (dispatch_checks._run_git) -- every dispatch calls
            # this on the identity-gated guard path, so an unbounded spawn
            # here (a hung git behind a contended/slow disk, a network
            # filesystem, or a stale index lock) stalls PreToolUse(Bash)
            # forever before the user's own command ever runs. 2026-08-05
            # hardening pass (PM ruling: "spawn sites need to have timeouts,
            # that can save a machine from getting degraded-stuck").
            timeout=2.0,
            # Windows console-popup suppression; no-op on POSIX
            # (CREATE_NO_WINDOW is Windows-only).
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        # Timeout joins the pre-existing OSError leg of this fail-open
        # contract (see docstring above) -- a hung git is indistinguishable
        # from "no git repo here" to every caller of resolve_git_root.
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    if not root:
        return None
    # `git rev-parse --show-toplevel` always emits forward-slash paths (even on
    # Windows) — this is a filesystem path callers pass back into os.path/Path
    # APIs and compare against native cwd-derived strings, not a wire id, so
    # normalize to the host's native separator here rather than leaving a
    # forward-slash form that silently mismatches os.sep-built paths downstream.
    return os.path.normpath(root)


#: Depth bound for `resolve_git_root_cheap`'s walk-up. Same value the
#: bash_guards prefilter has used since it was written -- a repo nested deeper
#: than this reports "not found" rather than walking to the filesystem root.
_CHEAP_ROOT_WALK_MAX_DEPTH = 64


def resolve_git_root_cheap(cwd: Optional[str]) -> Optional[str]:
    """Nearest ancestor of ``cwd`` holding a ``.git`` entry, found with
    ``os.path.exists`` only -- NEVER a ``git rev-parse`` spawn.

    MISS-MODE CALLERS ONLY, and that restriction is the whole contract. This
    is deliberately cheaper and LESS AUTHORITATIVE than ``resolve_git_root``
    below: a root reached through a symlinked ancestor can resolve to a
    different absolute path than ``git rev-parse --show-toplevel`` reports,
    because that realpath-resolves and this does not. A caller whose wrong
    answer produces a wrong VERDICT (``bump_foreign_repo_write``'s same-repo
    comparison is the standing example) must keep using ``resolve_git_root``
    -- there, a divergent root is a false bump or a silent miss. A caller
    whose wrong answer merely means "this lookup missed" may use this.

    Homed here rather than in ``bash_guards/_helpers`` (which is where
    docs/plans/2026-08-21-guards-under-the-brightline.md § Anti-scope
    suggested lifting it) because its first non-guard caller,
    ``guard_advisory_counter``, is a TOP-LEVEL module: importing ``_helpers``
    from there would invert the layering and drag the guard import closure
    into it. Siting it beside ``resolve_git_root`` keeps ONE resolver pair in
    one place, which is what that Anti-scope entry was protecting.

    Returns ``None`` when ``cwd`` is falsy or unresolvable, or when no
    ``.git`` is found within ``_CHEAP_ROOT_WALK_MAX_DEPTH`` levels.
    """
    if not cwd:
        return None
    try:
        current = os.path.abspath(cwd)
    except (OSError, ValueError):
        return None
    for _ in range(_CHEAP_ROOT_WALK_MAX_DEPTH):
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return current
        except OSError:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def resolve_git_root(cwd: Optional[str] = None) -> Optional[str]:
    """Return the git repo toplevel, or ``None`` (fail-open, row 4).

    Caching policy (added to eliminate repeat byte-identical spawns — the
    PreToolUse(Bash) guard chain calls this once per identity-gated guard
    with the SAME explicit ``payload["cwd"]``, spawning a duplicate `git
    rev-parse --show-toplevel` per guard for every Bash dispatch): cached,
    process-local, keyed on ``cwd`` ONLY when ``cwd is not None``. A caller
    passing an explicit ``cwd`` has asked for a FIXED root and can be served
    from cache; ``cwd=None`` means "resolve against whatever the process cwd
    is right now", which can legitimately change mid-process (mirrors
    ``session.core.git_root()``'s documented uncached contract), so that
    branch is never cached and always re-spawns.

    A FAILED resolution (not a git repo, git missing, transient spawn error)
    is deliberately NOT cached even for an explicit ``cwd`` — only a
    successful resolution is memoized, so a transient failure never poisons
    every subsequent call for that ``cwd`` for the rest of the process. See
    ``reset_resolve_git_root_cache()`` for the cache-clear escape hatch.

    See ``_resolve_git_root_uncached`` for the resolution contract itself
    (fail-open, path normalization).
    """
    if cwd is None:
        return _resolve_git_root_uncached(cwd)
    try:
        return _resolve_git_root_cached(cwd)
    except _GitRootResolutionFailed:
        return None


# ---------------------------------------------------------------------------
# Agent-id / agent-type OR-resolver
# ---------------------------------------------------------------------------

def _canonical_agent_id(raw_agent_id: str, session_id: Optional[str]) -> str:
    """Port of ``resolve_subagent_identity`` + the F4 session_id-absent fallback.

    Accepts both bare-hex unnamed-agent ids (``^[a-f0-9]{12,}$``) and named
    teammate ids (``^a.+-[a-f0-9]{16}$``). For the named form, the back-pointer
    dir is normally keyed by a session_id-resolved value; when ``session_id``
    is absent from the payload, the reference hook falls back to keying on the
    RAW ``agent_id`` itself (lines 118-126) — a distinct leg from the
    session_id-present resolution, ported verbatim here (the Staff Engineer F4). Returns
    ``""`` if the raw id matches neither accepted form.
    """
    if _NAMED_TEAMMATE_RE.match(raw_agent_id):
        if session_id:
            return raw_agent_id
        # session_id-absent named-teammate fallback (the Staff Engineer F4): key on the
        # raw agent_id itself rather than a session_id-resolved value.
        return raw_agent_id
    if _BARE_HEX_RE.match(raw_agent_id):
        return raw_agent_id
    return ""


def _read_backpointer_subagent_type(
    git_root: str, agent_id: str, expected_em_session_id: str = ""
) -> str:
    """Back-pointer chain: agent_id -> em_session_id -> dispatched-agents.txt row.

    Ports the reference hook's secondary OR-resolver leg (lines 179-198).
    Any missing/unreadable/malformed link in the chain returns ``""``
    (lookup-fail — the secondary leg is simply absent, primary still applies).

    ``expected_em_session_id`` (OPTIONAL, review finding, 2026-08-14): when
    supplied (non-empty), the resolved ``em_sid`` must equal it, or the
    lookup returns ``""`` exactly as any other lookup-fail. Without this,
    the chain follows whatever ``em-session-id.txt`` names with no check
    that it names the SESSION MAKING THE CALL — a stale, cross-session, or
    fabricated back-pointer could resolve an unrelated session's dispatch
    row for this ``agent_id``, which
    ``bash_guards.block_reviewer_bash_outside_allowlist``'s Divergence 18
    confinement formula (2026-08-14) turns into a confinement-bypass
    oracle: a KNOWN, non-confined resolved ``subagent_type`` clears
    confinement for any caller-chosen ``agent_type``. Left unset (``""``,
    the default), behaviour is byte-identical to before this parameter
    existed — ``resolve_effective_types`` below passes nothing deliberately;
    tightening its posture is a separate decision, not made here.
    """
    backptr = Path(git_root) / ".git" / "coordinator-sessions" / ".agents" / agent_id / "em-session-id.txt"
    try:
        content = backptr.read_text(encoding="utf-8")
    except OSError:
        return ""
    em_sid = content.splitlines()[0].strip() if content else ""
    if not _SESSION_ID_FORMAT_RE.match(em_sid):
        return ""
    if expected_em_session_id and em_sid != expected_em_session_id:
        return ""

    dispatch_file = Path(git_root) / ".git" / "coordinator-sessions" / em_sid / "dispatched-agents.txt"
    try:
        rows = dispatch_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    # Review: coordinator:code-reviewer (2026-08-14, P3, duplicate-row
    # ambiguity) -- a bare "return on first match" resolved a duplicate
    # agent_id (one legacy 2-column row, one full 3+-column row) by file
    # order rather than by recency. Now: rows with fewer than 3 columns are
    # ignored outright, and more than one 3+-column row matching the same
    # agent_id is ambiguous -> fail-closed ("") rather than picking one by
    # position.
    matches = [
        fields[2]
        for row in rows
        for fields in (row.split("\t"),)
        if len(fields) >= 3 and fields[0] == agent_id
    ]
    if len(matches) == 1:
        return matches[0]
    return ""


def resolve_effective_types(
    payload: Dict[str, Any], git_root: Optional[str]
) -> Tuple[str, str, str]:
    """OR-resolver: returns (agent_id, agent_type, subagent_type).

    ``agent_type`` is the leg populated for UNNAMED/foreground dispatch
    (top-level payload field — the ``subagent_type`` in that shape).
    ``subagent_type`` is the leg populated for NAMED/teammate dispatch
    (back-pointer-resolved; covers the case where ``agent_type`` is the
    teammate name, not the subagent_type). Both legs are always computed and
    OR'd with no precedence between them when checking exempt/confined
    membership — "primary"/"secondary" here would describe provenance only,
    not evaluation order. ``agent_id`` is the canonicalized id (empty string
    if unrecognized/unparseable).
    """
    raw_agent_id = payload.get("agent_id") or ""
    session_id = payload.get("session_id") or None
    agent_id = _canonical_agent_id(raw_agent_id, session_id) if raw_agent_id else ""

    agent_type = payload.get("agent_type") or ""

    subagent_type = ""
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)

    return agent_id, agent_type, subagent_type

