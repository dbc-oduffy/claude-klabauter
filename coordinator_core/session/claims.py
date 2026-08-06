"""
coordinator_core.session.claims — Python engine port of the CLAIMS module of
the coordinator session hub (Port of: coordinator-session.sh, example-doctrine-repo e34f2484,
2026-07-22).

The claim primitives were deliberately NOT extracted to a ``lib/session/``
sub-module by T0-decompose — they live in the MAIN bash file — so this is the
faithful port of that in-file cluster: the atomic mkdir-based claim/takeover/
release/clear machinery for concurrent ``/pickup`` and plan-execution race
detection, plus the touched.txt self-attribution helpers.

This is a PURE IN-PROCESS LIBRARY, not an IPC op — it self-registers nothing,
touches none of the shared op-registry files, and is imported directly by
sibling modules and (in a LATER chunk) by the native op wrappers. The bash
callers (pickup / execute-plan / workstream-complete SKILL.md, handoff) are a
SEPARATE repoint chunk, NOT rewired here.

TWO DISTINCT DISCIPLINES the claim layer routes through (both in liveness.py):
    - claim TAKEOVER / clear / reap ask the LIVENESS question — is the HOLDER
      still alive? — via ``liveness.claim_holder_live`` (the claim dir's OWN
      metadata, NEVER the caller's pid/sid). One rule, shared with the reaper
      and the memo sweep (single-liveness-key invariant, D5/pcore-03).
    - release / plan re-entrancy ask the IDENTITY question — am I the recorded
      holder? — via ``liveness.claim_held_by_me`` with a PRE-RESOLVED my_sid
      (the two-call TOCTOU re-read keys both reads off ONE identity).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1
Spec backlinks (original claim contracts):
    archive/specs/2026-06/2026-06-17-foreign-cwd-pickup-hardening.md § C1;
    archive/specs/2026-06/2026-06-17-concurrent-pickup-guard-sid-regression.md § C1;
    archive/specs/2026-06/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C1;
    docs/plans/2026-06-26-cs-claim-plan-execution-lock.md § C1;
    docs/plans/2026-06-30-claim-clear-liveness-gate.md § C2;
    docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C3
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § claims.py

Negative-spec:
    - The claim dir is BASENAME-ONLY, NEVER sid-namespaced. All sessions
      sharing a ``<root>`` contend for ONE lock per artifact; that shared-path
      mkdir IS the same-machine concurrent-pickup guard (DR-110). It was
      sid-namespaced until 2026-06-17, which silently defeated the guard once
      Claude Code moved to per-session CLAUDE_CODE_SESSION_ID. Do NOT
      re-namespace.
    - On EEXIST, evaluate liveness against the HOLDER's OWN metadata via
      ``liveness.claim_holder_live`` — NEVER against my pid/sid. Do NOT
      hand-roll a ``held_sid == sid`` string compare — that reintroduces the
      per-session bug and skips the session_id-vs-legacy-pid discrimination +
      internal TOCTOU re-read of ``liveness.claim_held_by_me``.
    - The re-entrant self-claim branch is PLAN-CLASS-ONLY. handoff/memo are
      claimed ONCE per session and their contract is "a same-session re-claim
      is REJECTED" (regression tests T16a/T18c). A global re-entrant branch
      would silently flip that to return-True and break byte-for-byte
      compatibility. Do NOT broaden it.
    - Do NOT re-port ``cs_reap_stale_claims`` (already native ->
      ``ops/session/reap.py``) nor the RETIRED review-claim helpers.
    - Do NOT call ``cs_claim_artifact`` (this ``claim_artifact``) from a hook
      subprocess — the recorded pid is the CALLER's ``os.getpid()``, which
      MUST be a long-lived process (skill/interactive shell); a hook subshell
      exits within seconds and its claim reads immediately dead to the reaper.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import List, Optional, Tuple, Union

from coordinator_core.session import core
from coordinator_core.session import liveness
from coordinator_core.session import scope
from coordinator_core.session import shape


# ---------------------------------------------------------------------------
# Shared DR-084 lifecycle accessor loader (coordinator/bin/lib/handoff_lifecycle.py)
#
# Single exported loader for the frontmatter-level claimed_by/consumed_by
# dual-read accessor — distinct from this module's own mkdir-lock claim
# primitives above (artifact-first: given a path, who holds the mkdir lock).
# This is the session-first counterpart's shared dependency: given a session
# id, which handoff's claimed_by/consumed_by field names it — consumed by
# ``coordinator_core.ops.handoff_author_fork`` and
# ``coordinator_core.ops.session.resolve_chain_terminal_disposition``, both
# of which previously carried independent copies of this importlib-load
# boilerplate. The accessor file lives outside the ``coordinator_core``
# package (under the sibling ``coordinator/bin/lib/`` tree), so it cannot be
# a plain ``import`` — it must be loaded by file path via ``importlib``.
# ---------------------------------------------------------------------------

_HANDOFF_LIFECYCLE_ACCESSOR_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib" / "handoff_lifecycle.py"
)

_handoff_lifecycle_cache: Optional[ModuleType] = None


def handoff_lifecycle() -> ModuleType:
    """Load (once) the shared DR-084 lifecycle accessor module and cache it.

    Fail-loud when the accessor file is absent: a tree without it is a broken
    install, and falling back to a local dual-read would be exactly the
    second raw read site the single-accessor guard exists to forbid.
    """
    global _handoff_lifecycle_cache
    if _handoff_lifecycle_cache is not None:
        return _handoff_lifecycle_cache
    if not _HANDOFF_LIFECYCLE_ACCESSOR_PATH.is_file():
        raise RuntimeError(
            "coordinator_core.session.claims.handoff_lifecycle: shared DR-084 "
            f"accessor missing at {_HANDOFF_LIFECYCLE_ACCESSOR_PATH} — cannot "
            "resolve claim holders without the single shared lifecycle read "
            "site (never inline a dual-read here)"
        )
    spec = importlib.util.spec_from_file_location(
        "_claude_klabauter_handoff_lifecycle_accessor", _HANDOFF_LIFECYCLE_ACCESSOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "coordinator_core.session.claims.handoff_lifecycle: importlib "
            f"could not build a spec for {_HANDOFF_LIFECYCLE_ACCESSOR_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    _handoff_lifecycle_cache = module
    return module


# ---------------------------------------------------------------------------
# touched.txt append primitive
# ---------------------------------------------------------------------------


def atomic_dedup_append(touched: str, entry: str) -> bool:
    """Port of ``cs_atomic_dedup_append <touched-file> <new-entry>`` (786-801),
    made EVENT-AWARE (EM ratification 2026-08-03, plan
    ``2026-08-03-scope-guard-peer-claim-release.md`` § EM ratification item 2,
    chunk C1g) — the SECOND ``touched.txt`` writer (reached from
    ``self_claim`` and, via ``js_bridge_cli``, from
    ``coordinator/lib/coordinator_session.py::claim_path``) now joins
    ``scope.touch()``'s event-log dialect instead of appending a bare,
    unstamped line.

    Append-only write (the fix for the T21 lost-update race: the prior
    mktemp+sort+mv pattern let N concurrent writers each read-then-overwrite,
    so the last mv won and earlier distinct-path merges were silently
    dropped). Still pure append, one short write under PIPE_BUF — NO mktemp,
    NO mv, NO flock; that discipline is the whole reason this function exists
    and is preserved exactly.

    ``entry`` is a bare PATH, never a whole event line — but this is a
    LOWER-LEVEL primitive, and only the ``self_claim`` caller is guaranteed
    to have repo-relativized it first (via ``scope.normalize_touch_path``).
    The other reachable caller (``js_bridge_cli._cmd_claim_path`` ->
    ``coordinator/lib/coordinator_session.py::claim_path``) forwards its
    ``entry`` argument verbatim, with NO normalization step of its own — an
    absolute-path caller there writes an absolute-path entry, which will
    never dedup-match a relative-form entry for the same file written via
    ``self_claim``. Normalizing that caller is a separate, out-of-scope
    change (Review: coordinatorcode-reviewer-7ca5d82a Finding 2) — the
    caller owns normalization at this layer, this function does not
    normalize on the caller's behalf.

    Two steps:
      1. Fast-exit if ``entry`` is already CLAIMED — i.e. the LAST event recorded for
         that path (via ``scope.parse_touch_event``, the single shared
         parser — no second dialect) is already ``T``. This subsumes the old
         whole-line ``grep -qxF`` dedup: a legacy bare-line record for
         ``entry`` parses to ``('T', None, entry)`` (§ ``parse_touch_event``
         fail-safe), so a path whose only record is a bare line is already
         CLAIMED and a ``self_claim`` for it correctly no-ops here too — no
         redundant append, no second dialect for the legacy corpus. Reading
         the file to decide whether to append is fine; rewriting it is not
         (this pass never writes back).
      2. Single-line append of ``scope.format_touch_event("T", entry)``. One
         short write under PIPE_BUF (4096) is atomic on POSIX O_APPEND files,
         and Windows NTFS (Git Bash) serializes concurrent appends without
         corruption.

    Silent-failure contract: ALWAYS returns True (bash ``return 0`` on every
    path), so an advisory hook never blocks tool calls. A missing/unreadable
    file simply falls through to the append; an append that itself fails
    (OSError) is swallowed and still reports success.

    ``touched`` and ``entry`` are REQUIRED (bash ``${1:?}`` / ``${2:?}``) —
    an empty value raises ValueError.
    """
    if not touched:
        raise ValueError("touched-file required")
    if not entry:
        raise ValueError("new-entry required")

    # Fast-exit: is the LAST event recorded for this path already 'T'?
    # Non-atomic read is fine — a false negative just falls through to the
    # append, where a duplicate T event may land (harmless: still CLAIMED,
    # and cleaned at next consumption-time dedup).
    #
    # Review: coordinatorcode-reviewer-7ca5d82a Finding 3 — mirrors
    # scope.touch()'s reversed-scan-and-break: only entry's own last event
    # is ever needed, so scan backward and stop at the first match rather
    # than building a Dict[str, str] over every distinct path in the file.
    already_claimed = False
    try:
        with open(touched, encoding="utf-8") as fh:
            lines = fh.readlines()
        for line in reversed(lines):
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            verb, _ts, path = scope.parse_touch_event(stripped)
            if path == entry:
                already_claimed = verb == "T"
                break
    except OSError:
        pass  # file missing / unreadable -> fall through to append
    if already_claimed:
        return True

    try:
        with open(touched, "a", encoding="utf-8") as fh:
            fh.write(scope.format_touch_event("T", entry) + "\n")
    except OSError:
        return True  # silent-failure contract — never block the caller
    return True


# ---------------------------------------------------------------------------
# THE claim primitive + class wrappers
# ---------------------------------------------------------------------------


def _write_claim_meta(claim_dir: Path, sid: str) -> None:
    """Write the ``pid`` / ``session_id`` / ``claimed_at`` metadata files into a
    freshly-mkdir'd claim dir. ``pid`` is ``os.getpid()`` — the CALLER's pid,
    which MUST be long-lived (see module negative-spec)."""
    (claim_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (claim_dir / "session_id").write_text(f"{sid}\n", encoding="utf-8")
    (claim_dir / "claimed_at").write_text(f"{core.now_iso()}\n", encoding="utf-8")


def _claim_base(class_: str, baton_repo_root: str, cwd: Optional[str]) -> Optional[str]:
    """Resolve the ``<...>/coordinator-sessions`` base for a claim.

    Baton-repo mode (arg present) fails LOUD on an unresolvable root rather
    than silently writing the lock into the wrong (cwd) repo — returns None
    after emitting the diagnostic. Legacy (absent) mode resolves the cwd repo's
    sessions dir; None when not in a git repo.
    """
    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            print(
                f"cs_claim_{class_}: baton repo root <{baton_repo_root}> is not a git repo",
                file=sys.stderr,
            )
            return None
        return str(Path(baton_repo_root) / ".git" / "coordinator-sessions")
    base = core.sessions_dir(cwd)
    if not base:
        return None
    return base


def claim_artifact(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Port of ``cs_claim_artifact <class> <basename> [baton_repo_root]`` (864-962).

    THE atomic mkdir-based claim primitive for concurrent ``/pickup`` race
    detection, shared by the handoff/memo/plan classes (thin wrappers below).
    ``class_`` selects the claims subdir (``<class>-claims``) and the log
    prefix. Returns True on a successful (or re-entrant) claim, False on a
    live-holder collision, an unresolvable session id, a bad baton root, or a
    failed stale-takeover mkdir.

    Claim directory: ``<base>/<class>-claims/<basename>/`` — BASENAME-ONLY,
    NOT sid-namespaced (the shared-path mkdir IS the same-machine concurrent-
    pickup guard, DR-110 — do NOT re-namespace; see module negative-spec).

    ``baton_repo_root`` (optional): the git repo that OWNS the baton being
    picked up. When supplied, the claim lives under that repo's ``.git`` so two
    concurrent ``/pickup`` sessions of the SAME baton contest the same mkdir
    regardless of cwd (foreign-cwd pickup of a ``~/.claude`` baton). A
    SUPPLIED-but-non-git root FAILS LOUD (detect-then-fail-loud, never a silent
    cwd fallback). Only the lock LOCATION follows the baton — session-id
    resolution stays anchored to the running (cwd) session (the sentinel is
    written into the cwd session's ``.git`` by session-init, never the baton).

    On success: atomic ``os.mkdir`` then write pid/session_id/claimed_at inside
    the claim dir.

    On EEXIST — liveness is evaluated against the HOLDER (the claim dir's OWN
    metadata via ``liveness.claim_holder_live``), NEVER the caller:
      - PLAN-CLASS-ONLY re-entrant branch FIRST: if ``class_ == "plan"`` and
        ``liveness.claim_held_by_me(claim_dir, sid)``, return True immediately
        (re-entrant success). A plan is claimed at two seams — execute-plan +
        workstream-complete — which may both run in the same live session;
        without this branch the second seam would fail loud against itself.
        SCOPED to plan deliberately: handoff/memo are claimed once per session
        and their contract is "same-session re-claim REJECTED" (T16a/T18c). Do
        NOT broaden (module negative-spec).
      - Live holder -> failure (concurrent /pickup detected).
      - Dead / >30-min-idle holder -> ``rm -rf`` + re-mkdir takeover (the
        atomic rm+mkdir is itself the race guard: a peer that re-claims between
        them makes our mkdir fail).
      - Legacy pid-only claim dir (no session_id file) -> ``liveness.
        claim_holder_live`` falls back to the ephemeral-pid test.

    ``class_`` / ``basename`` are REQUIRED (bash ``${1:?}`` / ``${2:?}``) —
    empty raises ValueError.
    """
    if not class_:
        raise ValueError("artifact class required")
    if not basename:
        raise ValueError("basename required")

    # Canonical 4-tier resolution; sid is a property of the running (cwd)
    # session — only the lock LOCATION follows the baton. Empty -> FAIL LOUD.
    sid = core.resolve_session_id(cwd)
    if not sid:
        print(
            f"cs_claim_{class_}: session id unresolvable under concurrency — "
            f"run: export CLAUDE_SESSION_ID=<harness-id>",
            file=sys.stderr,
        )
        return False

    base = _claim_base(class_, baton_repo_root, cwd)
    if base is None:
        return False

    claims_dir = Path(base) / f"{class_}-claims"
    claim_dir = claims_dir / basename

    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # mkdir -p ... 2>/dev/null || true

    # Attempt atomic mkdir. ANY failure (EEXIST or otherwise) falls through to
    # the EEXIST-inspection path, exactly as the bash `if mkdir ...; then` /
    # fall-through structure does.
    created = False
    try:
        os.mkdir(claim_dir)
        created = True
    except OSError:
        created = False
    if created:
        _write_claim_meta(claim_dir, sid)
        return True

    # ---- EEXIST — inspect the existing claim (holder's OWN metadata) ----
    held_pid = _read_claim_field(claim_dir, "pid")
    held_sid = _read_claim_field(claim_dir, "session_id")

    # Re-entrant self-claim (PLAN CLASS ONLY) — BEFORE the liveness branch.
    if class_ == "plan" and liveness.claim_held_by_me(str(claim_dir), sid, cwd):
        return True

    if liveness.claim_holder_live(str(claim_dir), cwd):
        live_pid = ""
        if held_sid:
            holder_sdir = core.session_dir(held_sid, cwd)
            if holder_sdir:
                live_pid = core.read_meta_field(holder_sdir, "stable_pid")
        if live_pid:
            pid_clause = f"live PID {live_pid} (session registry stable_pid)"
        elif not held_sid:
            # Legacy pid-only claim dir (no session_id file): the True verdict
            # we're inside this branch FOR came from claim_holder_live's own
            # fallback -- core.pid_alive(held_pid), the SAME pid printed here.
            # Liveness of held_pid IS confirmed (that's why we're here); what's
            # UNCONFIRMED is only the registry stable_pid cross-check, which
            # doesn't apply to a session_id-less dir. "not confirmed live"
            # would flatly contradict the branch we're in -- distinct wording
            # for the two distinct unknowns (19ac2abf fixed this message once
            # already; conflating them again is the same defect class).
            pid_clause = f"live PID {held_pid or '?'} (confirmed via legacy pid-liveness check, no session registry entry)"
        else:
            pid_clause = f"recorded-at-claim-time PID {held_pid or '?'} (not confirmed live)"
        print(
            f"cs_claim_{class_}: {basename} held by session {held_sid or '?'} "
            f"— {pid_clause} — concurrent /pickup detected; reconcile with that "
            f"session, or run clear-claim-if-dead once it exits",
            file=sys.stderr,
        )
        return False

    # Holder is dead or >30-min idle — stale claim; take over.
    print(
        f"cs_claim_{class_}: stale claim on {basename} (session {held_sid or '?'}, "
        f"PID {held_pid or '?'} not live) — taking over",
        file=sys.stderr,
    )
    shutil.rmtree(claim_dir, ignore_errors=True)
    try:
        os.mkdir(claim_dir)
    except OSError:
        print(
            f"cs_claim_{class_}: failed to create claim dir for {basename} "
            f"after stale takeover",
            file=sys.stderr,
        )
        return False
    _write_claim_meta(claim_dir, sid)
    return True


def _read_claim_field(claim_dir: Path, field: str) -> str:
    """Read a single-line claim metadata file (``pid`` / ``session_id``),
    stripped, or ``""`` if absent/unreadable (mirrors ``cat ... || echo ""``)."""
    try:
        return (claim_dir / field).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def claim_handoff(
    basename: str, baton_repo_root: str = "", cwd: Optional[str] = None
) -> bool:
    """Port of ``cs_claim_handoff <basename> [baton_repo_root]`` (970). Thin
    class-bound wrapper — a byte-for-byte 2-arg passthrough to
    ``claim_artifact("handoff", ...)`` preserving the existing call-site
    contract."""
    return claim_artifact("handoff", basename, baton_repo_root, cwd=cwd)


def claim_memo(
    basename: str, baton_repo_root: str = "", cwd: Optional[str] = None
) -> bool:
    """Port of ``cs_claim_memo <basename> [baton_repo_root]`` (971). Thin
    class-bound wrapper over ``claim_artifact("memo", ...)`` — the parity
    addition for memo-pickup (pickup SKILL.md Memo Branch M2.5)."""
    return claim_artifact("memo", basename, baton_repo_root, cwd=cwd)


def _read_scope_mode(plan_file: Path) -> str:
    """Read the ``scope_mode:`` frontmatter value from a plan file (first match),
    stripped of surrounding whitespace and ALL quote chars (mirrors the bash
    ``grep '^scope_mode:' | head -1 | sed ... | tr -d '"' | tr -d "'"``).
    Returns ``""`` on absence/read failure."""
    try:
        text = plan_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("scope_mode:"):
            val = line[len("scope_mode:"):].strip()
            return val.replace('"', "").replace("'", "")
    return ""


def claim_plan(slug: str, cwd: Optional[str] = None) -> bool:
    """Port of ``cs_claim_plan <basename>`` (980-1011).

    ONE-arg wrapper (the ``baton_repo_root`` arg is DELIBERATELY DROPPED —
    plan execution is always in-repo, and passing it through would silently
    absorb a stray extra arg and write the lock into the wrong repo). The
    cwd-default base guarantees an identical claim base across the execute-plan
    and workstream-complete seams.

    On a successful claim, best-effort records ``plan.path`` +
    ``plan.scope_mode`` (the write-time fact from plan frontmatter) into
    ``session-shape.json`` via ``shape.session_shape_set`` (C3 instrumentation).
    NON-FATAL: a shape-write failure must NOT break the plan claim — the True
    return is already committed by the claim success. ``light|full`` sizing is
    a deferred read-time ceremony derivation, NOT stored here (plan F4 fix).

    Returns the claim's success verdict (False if the underlying
    ``claim_artifact`` failed — the shape write is skipped entirely then).

    ``slug`` MUST be a bare basename (e.g. ``2026-07-26-some-plan``), NEVER a
    path. A path-shaped arg (containing ``/`` or ``\\``, or ending in
    ``.md``) is REJECTED here, loud and non-zero, before it ever reaches
    ``claim_artifact``'s ``os.mkdir`` — do NOT let it fall through to mkdir.
    That fall-through is a real hazard, not a theoretical one: ``os.mkdir``
    only requires its OWN leaf component be absent, so a path-shaped basename
    whose intermediate directories HAPPEN to already exist (left behind by an
    earlier failed path-shaped call, a concurrent session, or any other
    coincidence) succeeds SILENTLY — no stderr, ``True`` returned — while
    creating a claim nested under a bogus ``<...>/plan-claims/docs/plans/
    <file>.md`` subtree that protects nothing the real basename-keyed slug
    depends on. Two sessions can then both believe they hold the real plan's
    claim. Rejecting the shape here forecloses the whole class rather than
    hardening ``claim_artifact``'s mkdir fallback, which cannot distinguish
    "intermediate dirs coincidentally pre-exist" from "genuine EEXIST on the
    leaf" after the fact.

    Deliberately REJECT, not normalize-to-basename: this is the module's one
    plan-claiming entry point (also the ``session-claim-cli claim-plan``
    subcommand's target), and a caller passing a full path is itself a bug at
    the call site (see the ``/handoff`` d5-directive emitter fix, same
    workstream) — silently accepting and stripping it would launder that bug
    forward indefinitely instead of surfacing it where it can be fixed once.

    Spec backlink: docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C3
    """
    if "/" in slug or "\\" in slug or slug.endswith(".md"):
        print(
            f"cs_claim_plan: expected a bare plan slug (basename, no path "
            f"separator, no .md suffix) — got {slug!r}; pass the plan's "
            f"slug (e.g. 2026-07-26-some-plan), not its path",
            file=sys.stderr,
        )
        return False

    if not slug or slug.startswith("-"):
        print(
            f"cs_claim_plan: expected a bare plan slug (basename, no path "
            f"separator, no .md suffix) — got {slug!r}; pass the plan's "
            f"slug (e.g. 2026-07-26-some-plan), not its path",
            file=sys.stderr,
        )
        return False

    if not claim_artifact("plan", slug, cwd=cwd):
        return False

    # C3 — best-effort session-shape instrumentation (non-fatal).
    sid = core.resolve_session_id(cwd)
    if sid:
        rel = f"docs/plans/{slug}.md"
        scope = ""
        root = core.git_root(cwd)
        if root and (Path(root) / rel).is_file():
            scope = _read_scope_mode(Path(root) / rel)
        # A dict fragment lets session_shape_set / json.dump handle escaping —
        # no hand-rolled JSON string-escaping needed (bash escaped inline).
        fragment = {"plan": {"path": rel, "scope_mode": scope if scope else None}}
        try:
            shape.session_shape_set(sid, fragment, cwd)
        except Exception as exc:
            # non-fatal — plan claim already succeeded; surface the failure
            # for debugging without blocking the caller.
            print(
                f"cs_claim_plan: session-shape write failed for {slug} "
                f"(non-fatal): {exc}",
                file=sys.stderr,
            )
    return True


# ---------------------------------------------------------------------------
# Release + clear
# ---------------------------------------------------------------------------


def release_artifact(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Port of ``cs_release_artifact <class> <basename> [baton_repo_root]`` (1030-1062).

    Explicit, holder-identity-checked release of a claim. Unlike handoffs
    (dead-PID reaping only), memo-pickup reaches meaningful NON-TERMINAL
    dispositions (Decline, Surface-to-PM) while still alive — those must
    release the claim so a legitimate re-pickup is not blocked until the PID
    dies.

    SAFETY: releases only if THIS session is the recorded holder
    (``liveness.claim_held_by_me`` — keyed on session_id == my id, NEVER $$).
    Not the holder, or the claim already absent, or a bad/absent baton root ->
    NO-OP success. A bare rm without this check would race an inline dead-PID
    takeover and could delete a live peer's claim.

    ``my_sid`` is resolved ONCE and passed to BOTH ``claim_held_by_me`` reads
    (called TWICE — intentional TOCTOU re-read): the second read then varies
    only on the claim-dir CONTENT (the actual race), not on a re-resolution of
    my own id. If an inline takeover (rm + mkdir + new session_id) slipped in
    after the first check, the recheck no longer matches our id and we skip —
    never delete a live peer's claim.

    ORDERING CONTRACT (enforced by the CALLER, not here): the caller MUST
    revert the artifact's frontmatter (status in_progress -> open, clear stamps)
    BEFORE calling this. A crash between the two steps then leaves a recoverable
    "open but claim-held" state; the reverse (claim freed, status still
    in_progress) would re-admit two sessions.

    ALWAYS returns True (bash ``return 0`` on every path — the no-op paths are
    successes, not errors).

    ``class_`` / ``basename`` REQUIRED — empty raises ValueError.

    Spec backlink: docs/plans/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C1
    """
    if not class_:
        raise ValueError("artifact class required")
    if not basename:
        raise ValueError("basename required")

    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            return True  # bad baton root -> no-op success
        base = str(Path(baton_repo_root) / ".git" / "coordinator-sessions")
    else:
        base = core.sessions_dir(cwd)
        if not base:
            return True  # not in a git repo -> no-op success

    claim_dir = Path(base) / f"{class_}-claims" / basename
    if not claim_dir.is_dir():
        return True  # already absent — no-op

    # Resolve my id ONCE; pass it to both TOCTOU reads (F1).
    my_sid = core.resolve_session_id(cwd)
    if not liveness.claim_held_by_me(str(claim_dir), my_sid, cwd):
        return True  # not the holder — no-op
    # TOCTOU re-read before rm (the second call IS the two-read discipline).
    if not liveness.claim_held_by_me(str(claim_dir), my_sid, cwd):
        return True  # takeover slipped in — skip; never delete a live peer's claim

    shutil.rmtree(claim_dir, ignore_errors=True)
    return True


def clear_claim_if_dead(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Port of ``cs_clear_claim_if_dead <class> <basename> [baton_repo_root]`` (1097-1157).

    Single-named-claim sibling of ``cs_reap_stale_claims`` — CLEAR-ONLY (does
    NOT re-claim). The manual EM-driven claim-clear / takeover path: forces the
    caller through a liveness gate before any ``rm -rf``. The claim is removed
    only when the holder is DEAD; a live holder returns False with a stderr
    message naming the holder. Clear and re-claim are SEPARATE operations — do
    NOT bundle them.

    Returns:
      - True on a successful clear OR an idempotent no-op (absent claim /
        absent sessions dir).
      - False on an invalid class, a bad/absent baton root, or a refusal
        because the holder is (or became) live.

    Class validation: restrict to ``{handoff, memo, plan}`` (invalid ->
    False + stderr). Empty ``class_`` / ``basename`` raise ValueError first
    (bash ``${1:?}`` / ``${2:?}``).

    Base resolution: a SUPPLIED-but-bad baton root FAILS LOUD (False + stderr,
    mirroring ``claim_artifact`` — this function is EM-callable and must
    surface errors, NOT the reaper's silent skip). An ABSENT sessions dir (no
    baton) is an idempotent success (no claim can exist).

    LIVENESS: delegates ENTIRELY to ``liveness.claim_holder_live`` — the
    canonical predicate. NEVER ``ps -p`` / ``kill -0`` on a stored pid
    directly (RAW-PID-LIVENESS floor). DOUBLE ``claim_holder_live`` (read 1 +
    TOCTOU read 2) brackets the rm: if a concurrent takeover lands between
    reads (rm + mkdir + new live session_id), read 2 sees the NEW live holder
    and aborts.

    LEGACY pid-only residual: a claim dir with no ``session_id`` file routes
    ``claim_holder_live`` to the ephemeral-pid test ("structurally always dead
    in-harness"), so a legacy pid-only claim whose session is LIVE is
    classified DEAD and cleared. A DISTINCT stderr note is emitted when
    clearing such a dir so the caller is not falsely reassured.

    Spec backlink: docs/plans/2026-06-30-claim-clear-liveness-gate.md § C2
    """
    if not class_:
        raise ValueError("class required")
    if not basename:
        raise ValueError("basename required")

    if class_ not in ("handoff", "memo", "plan"):
        print(
            f"cs_clear_claim_if_dead: invalid class '{class_}' "
            f"(must be handoff, memo, or plan)",
            file=sys.stderr,
        )
        return False

    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            print(
                f"cs_clear_claim_if_dead: baton repo root <{baton_repo_root}> "
                f"is not a git repo",
                file=sys.stderr,
            )
            return False
        base = str(Path(baton_repo_root) / ".git" / "coordinator-sessions")
    else:
        base = core.sessions_dir(cwd)
        if not base:
            return True  # missing sessions dir -> no claim can exist -> idempotent

    claim_dir = Path(base) / f"{class_}-claims" / basename
    if not claim_dir.is_dir():
        return True  # idempotent on absent claim dir

    # Liveness gate — read 1: refuse if the holder is still live.
    if liveness.claim_holder_live(str(claim_dir), cwd):
        holder = _read_holder(claim_dir)
        print(
            f"cs_clear_claim_if_dead: refusing to clear claim '{basename}' — "
            f"holder is live (session: {holder})",
            file=sys.stderr,
        )
        return False

    # TOCTOU re-read — read 2.
    if liveness.claim_holder_live(str(claim_dir), cwd):
        holder = _read_holder(claim_dir)
        print(
            f"cs_clear_claim_if_dead: aborting clear of '{basename}' — "
            f"holder became live after TOCTOU re-read (session: {holder})",
            file=sys.stderr,
        )
        return False

    # Distinct note for a legacy pid-only claim dir (no session_id file).
    if not (claim_dir / "session_id").is_file():
        print(
            "note: cleared a legacy pid-only claim (no session_id) — "
            "liveness could not be session-verified",
            file=sys.stderr,
        )

    # Reap/ship bug fix 1 (docs/plans/2026-07-24-g4-execute-pipeline-two-repo-
    # rebuild.md § M3a): a handoff claim-lock clear must ALSO reconcile the
    # handoff's own frontmatter (status:claimed → open), or list_stale_claim_
    # handoffs keeps reporting it stale forever (it recomputes off claimed_by,
    # never off the lock-dir). unconsume-FIRST-then-rmtree — see
    # reconcile_dead_handoff_claim_frontmatter's docstring for the crash-
    # disposition rationale this ordering buys.
    if class_ == "handoff":
        reconcile_dead_handoff_claim_frontmatter(basename, Path(base))

    shutil.rmtree(claim_dir, ignore_errors=True)
    return True


def _handoff_has_named_successor(handoff_path: Path, worktree: Path) -> Optional[bool]:
    """Return True/False for "some other baton names this one as a predecessor",
    or ``None`` when the corpus could not be enumerated completely.

    Succession edges only: ``predecessor`` + ``additional_predecessors``, which is
    exactly the set ``baton_assemble``'s d6 fires a supersede for (one per
    predecessor, primary and fan-in alike). ``forked_from`` is deliberately
    excluded — d6 does not fire for a fork, so a fork's existence says nothing
    about whether its parent was continued.

    Scans live AND archived handoffs: the successor of a resolved baton is
    routinely already under ``archive/handoffs/``, which is the whole shape this
    predicate exists to detect. Reuses ``handoff_children._collect_handoff_paths``
    + ``dag.referenced_by`` — the same enumeration and the same single-hop
    reverse-edge primitive ``ops/baton_drift_sweep.py`` classifies STRANDED with,
    so the reaper and the diagnostic cannot drift apart on what "has a successor"
    means.

    ``None`` on ANY incompleteness (scan error or unexpected failure) so callers
    can fail closed: an unreadable subtree must never read as "no successor".
    """
    from coordinator_core.dag import referenced_by
    from coordinator_core.ops.handoff_children import _collect_handoff_paths

    try:
        paths, scan_errors = _collect_handoff_paths(worktree)
    except Exception:
        return None
    if scan_errors:
        return None
    try:
        result = referenced_by(
            str(handoff_path),
            paths,
            edge_kinds={"predecessor", "additional_predecessors"},
            handoff_dir=str(worktree / "state" / "handoffs"),
            exclude=[str(handoff_path)],
        )
    except Exception:
        return None
    return bool(result.get("referenced"))


def reconcile_dead_handoff_claim_frontmatter(basename: str, sessions_dir: Path) -> None:
    """Best-effort: flip a dead-claimed handoff's frontmatter back to open.

    Reuses the EXISTING ``unconsume`` verb (``coordinator_core.ops.handoff_
    transition._unclaim``) rather than hand-rolling a second frontmatter
    mutator. Shared by ``clear_claim_if_dead`` (this module, manual EM-driven
    clear) and ``coordinator_core.ops.session.reap._reap_orphaned_claims``
    (automated reaper) — both clear the same ``<sessions_dir>/handoff-claims/
    <basename>`` lock and both carried the identical frontmatter-drift bug
    (the Director of Engineering Finding 4/5).

    ORDERING (the Director of Engineering Finding 4 — "atomic" is an overclaim; rmtree and a
    frontmatter mutation are two distinct filesystem ops, never atomic across
    a crash): callers MUST invoke this BEFORE ``shutil.rmtree``-ing the claim
    dir, never after. Crash disposition: (a) crash after this call succeeds
    but before the caller's rmtree — frontmatter is open, lock dir still
    present; the (now-open) handoff re-enters the shelf and the stale lock
    dir is picked up by the next reap pass (self-healing). (b) crash before
    this call runs — no state change, re-run from scratch. Both degrade to
    "lock still present, re-reaped" — never today's silent drift (lock
    cleared, frontmatter still claimed). rmtree-first-then-unconsume is
    explicitly ruled out: a crash after rmtree reproduces exactly the bug
    this function exists to close.

    PRECONDITION (verified at handoff_transition.py:731,809-813): unconsume
    fails loud (``MutateAbort``) unless ``deployment_state`` is currently
    ``in_flight`` or ``ready_to_fire``. A dead-claimed handoff is normally
    ``in_flight``, but a claim could in principle be sitting on some other
    ``deployment_state``. Rather than let that raised ``MutateAbort``
    propagate uncaught into the caller's clear/reap path, this function
    checks ``deployment_state`` itself FIRST and SKIPS (logs + returns) on
    anything outside ``{in_flight, ready_to_fire}`` — the lock dir still
    gets cleared by the caller; only the frontmatter step is skipped. Also
    skips silently when the handoff file no longer exists (nothing to
    reconcile) — never raises.

    SUCCEEDED-BATON CARVE-OUT (2026-07-30, break-class fix). A handoff that
    some other baton already names as its predecessor is NOT returned to the
    pool, whatever the holder's liveness: the unclaim is SKIPPED and only the
    caller's lock clear proceeds. Two distinct harms made this break-class,
    both observed on disk (three claude-klabauter batons: the DR-084 vocabulary-migration,
    claude-klabauter-driven-ceremony-redesign, and registration-quad chains, reaped at
    ``796ebf5b`` / ``8a90946c`` / ``2892e394``):

      - RESURRECTION. ``_unclaim`` restores ``status: open`` +
        ``deployment_state: ready_to_fire`` on a baton that carries
        ``pickup_ready: true``, so resolved work re-advertises itself to
        ``/pickup``. The dead holder is dead precisely BECAUSE it finished and
        handed off; its successor is the live continuation.
      - EVIDENCE DESTRUCTION, and this one is not self-healing. DR-242's
        ``claimed_or_shipped_at_path`` gate requires independent
        PREDECESSOR-side proof that the parent was once claimed or reached a
        terminal state. Unclaiming erases exactly that proof, so every
        succession writer downstream — ``baton_assemble``'s d6,
        ``handoff.archive_transition`` mode="supersede" — refuses the baton
        FOREVER AFTER. The baton is left permanently open, permanently
        pickup-ready, and permanently unclosable by any automated path; it
        surfaces only as a ``baton-drift-sweep`` STRANDED row.

    The carve-out declines to REOPEN; it concludes nothing about succession and
    stamps nothing, so DR-242 is not in tension with it (DR-242 governs writing
    ``continued``, which no code path here does). The fail-safe directions differ
    in kind: not-unclaiming leaves a stale ``claimed`` a human can resolve with
    an explicit ``drop`` or a supersede, whereas unclaiming is effectively
    irreversible. Indeterminate corpus enumeration therefore also SKIPS —
    ``_handoff_has_named_successor`` returning ``None`` is treated as "may have
    a successor", never as "has none".

    ``sessions_dir``: the ``<common_dir>/coordinator-sessions`` dir (the same
    value ``clear_claim_if_dead``/``_reap_orphaned_claims`` already resolve);
    the handoff worktree is derived from its parent (``common_dir``) via
    ``main_worktree_root`` — the single reviewed home for that derivation
    (see that helper's own negative-spec; do not inline a bare ``.parent``).
    """
    # Local imports: avoids a claims.py <-> handoff_transition.py import-time
    # cycle (handoff_transition registers an IPC op at import time; claims.py
    # is a pure in-process library imported far earlier in the boot chain).
    from coordinator_core.ops.fleet._common import main_worktree_root
    from coordinator_core.ops.handoff_transition import _unclaim
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

    common_dir = sessions_dir.parent
    worktree = main_worktree_root(common_dir)
    handoff_path = worktree / "state" / "handoffs" / basename

    if not handoff_path.is_file():
        return  # claim referenced a handoff that's already gone — nothing to reconcile

    deployment = read_frontmatter_field(str(handoff_path), "deployment_state")
    if deployment not in ("in_flight", "ready_to_fire"):
        print(
            f"note: skipping frontmatter reconcile for dead handoff claim "
            f"'{basename}' — deployment_state '{deployment}' outside "
            f"unconsume's accepted set (in_flight, ready_to_fire); lock still cleared",
            file=sys.stderr,
        )
        return

    has_successor = _handoff_has_named_successor(handoff_path, worktree)
    if has_successor is not False:
        print(
            f"note: skipping frontmatter reconcile for dead handoff claim "
            f"'{basename}' — "
            + (
                "another baton names it as a predecessor"
                if has_successor
                else "the handoff corpus could not be enumerated completely"
            )
            + "; leaving it claimed rather than re-advertising it to /pickup "
            f"(see reconcile_dead_handoff_claim_frontmatter's succeeded-baton "
            f"carve-out); lock still cleared",
            file=sys.stderr,
        )
        return

    # reaped_from (C2): this dead-claim reconcile IS the reap path (the
    # legitimate drop/park path never calls this function), so it always
    # opts in. Resolved from the claim dir's own session_id/pid metadata via
    # _read_holder — the same value cs_clear_claim_if_dead already surfaces
    # in its own diagnostic message — as _unclaim's caller-supplied fallback;
    # _unclaim itself prefers the handoff frontmatter's claimed_by/consumed_by
    # over this when present.
    # Review: coordinator:code-reviewer — _read_holder's fallback rungs return
    # the non-empty sentinel "unknown", or a raw pid string, when the claim
    # dir has no session_id file; neither is a real session id, and passing
    # either through as reaped_from would let _unclaim's resolution chain
    # accept it as if it were one. Reject both shapes here rather than
    # widening _read_holder's own contract (cs_clear_claim_if_dead depends on
    # its current sentinel behaviour for its diagnostic) — pass "" (still
    # opts in to _unclaim's reaped_from resolution, since only `None` fully
    # disables it, but "" never wins the non-empty-string check itself).
    claim_dir = sessions_dir / "handoff-claims" / basename
    holder = _read_holder(claim_dir)
    reaped_from_sid = "" if holder == "unknown" or holder.isdigit() else holder
    result = _unclaim(str(handoff_path), "", worktree, common_dir, reaped_from=reaped_from_sid)
    if result.get("exit_code") != 0:
        print(
            f"note: frontmatter reconcile for dead handoff claim '{basename}' "
            f"failed — {result.get('error', 'unknown error')}; lock still cleared",
            file=sys.stderr,
        )


# Anchored on the FULL sentence ``reconcile_dead_handoff_claim_frontmatter``'s
# reaper caller emits (see that function's docstring) — em-dash included, and
# requiring a full 36-char UUID-shaped capture, never a bare ``\S+``. A
# park_note whose shape has drifted (including the differently-shaped,
# truncated-8-char-sid archived/quoted note class documented below) does NOT
# match and routes to the fail-closed skip in ``backfill_reaped_from_session``.
_CRASH_ORPHAN_PARK_NOTE_RE = re.compile(
    r"^claim released by crash-orphan reaper — holder "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) "
    r"died without resolving"
)


def _collect_live_handoff_paths(worktree: Path) -> "Tuple[List[str], List[str]]":
    """Return (paths, scan_errors) for ``state/handoffs/*.md`` ONLY.

    Deliberately the LIVE HALF of ``handoff_children._collect_handoff_paths``,
    re-implemented here rather than reused, because that function's whole
    purpose is to also enumerate ``archive/handoffs/`` — this call site's one
    requirement is to never see that subtree at all (see
    ``backfill_reaped_from_session``'s negative-spec). Mirrors the live-half
    scan discipline exactly (``iterdir()``, not ``glob()``, so a permission
    error surfaces as a scan_errors entry rather than a silently-empty
    iterator — see that function's own NOTE for why ``glob()`` is unsafe
    here).
    """
    paths: List[str] = []
    scan_errors: List[str] = []

    state_dir = worktree / "state" / "handoffs"
    if state_dir.is_dir():
        try:
            entries = list(state_dir.iterdir())
        except OSError as exc:
            scan_errors.append(f"{state_dir}: {exc}")
        else:
            for p in entries:
                if p.suffix == ".md" and p.is_file():
                    paths.append(str(p.resolve()))

    return paths, scan_errors


def backfill_reaped_from_session(worktree: Path) -> dict:
    """One-shot backfill: parse the dead sid out of ``park_note`` for batons
    reaped BEFORE ``reaped_from_session`` (C2, this same plan) existed, and
    write it as a real frontmatter field.

    Spec backlink: docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § C5

    RE-DERIVES the target set at run time by scanning the LIVE corpus ONLY
    (``state/handoffs/``, via ``_collect_live_handoff_paths``) — deliberately
    does NOT hard-code the six paths named in the plan's Problem section, so a
    repo that has since reaped a seventh live baton the same way is served by
    the same entrypoint.

    Negative-spec: does NOT scan ``archive/handoffs/``. The plan's
    Out-of-scope section asserts no conclusion about the vendored
    ``handoff-archived.schema.json`` twin beyond noting the open question
    exists ("carries the same open question once these batons archive"); a
    write of ``reaped_from_session`` onto an already-archived, terminal
    record would assert exactly the conclusion that section declines to
    reach. Separately, ``reaped_orphan`` (the metric this field feeds) counts
    only NON-terminal batons, so an archived write buys this backfill nothing
    even setting the schema question aside. Do NOT widen this to
    ``handoff_children._collect_handoff_paths`` (its live+archived pair) —
    that reuse is intentional elsewhere in this module (see
    ``_handoff_has_named_successor``) but wrong here.

    A CLASS THIS CAN NEVER RECOVER: ``claims.py``'s own producer
    (``reconcile_dead_handoff_claim_frontmatter``) called ``_unclaim`` with an
    EMPTY note before C2 landed, so a tip reaped through that path before C2
    carries NO ``park_note`` at all — there is no sid to parse out of nothing.
    Such a baton is permanently unrecoverable by this function; only C2 going
    forward prevents new instances. It is counted among ``skipped`` here (no
    park_note to even attempt), never among ``errors``.

    Does NOT route through ``_unclaim``: every target baton is already at
    ``status: open`` + ``deployment_state: ready_to_fire`` — ``_unclaim``'s
    byte-identical idempotent no-op state — so calling it here would silently
    write nothing. This function writes the ONE new field directly, via
    ``locked_rmw`` (the same cross-process discipline every other frontmatter
    mutation in this engine uses — peer sessions are live in this tree).

    IDEMPOTENT: a baton already carrying ``reaped_from_session`` is skipped
    (counted in ``skipped``), and a second full run is byte-identical.

    UNVERIFIED BEYOND ONE RUN: re-run behaviour against a corpus that has
    since grown additional pre-C2 notes has not been exercised — only the
    single observed invocation against the live corpus at C5 time has been
    checked.

    FAIL-CLOSED PER BATON: an unparseable/absent/wrong-shape ``park_note``
    (including the archived, quoted, truncated-8-char-sid note class that an
    unanchored ``crash-orphan.*holder (\\S+)`` would mis-parse, e.g. "claim-
    release REVERTED 2026-07-30 — the crash-orphan reaper returned this baton
    to the pool at 796ebf5b, but holder 517027e6 had already minted its
    successor...") is skipped and named on stderr; the run keeps going for the
    rest of the corpus.

    Returns ``{"written": [...], "skipped": [...], "errors": [...]}`` — each
    a list of repo-relative path strings (``written``/``skipped``) or
    ``(path, message)`` tuples (``errors``, reserved for a locked_rmw failure
    — a parse/shape skip is NOT an error, it is an expected, named, fail-
    closed outcome and goes in ``skipped``).
    """
    from coordinator_core.frontmatter.primitives import (
        insert_fm_field,
        read_fm_field,
        read_fm_field_unquoted,
        rebuild,
        split_frontmatter,
    )
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    written: List[str] = []
    skipped: List[str] = []
    errors: List[Tuple[str, str]] = []

    paths, scan_errors = _collect_live_handoff_paths(worktree)
    for scan_error in scan_errors:
        errors.append(("<scan>", scan_error))

    for path_str in sorted(paths):
        path = Path(path_str)
        try:
            rel = str(path.relative_to(worktree))
        except ValueError:
            rel = path_str

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append((rel, f"unreadable: {exc}"))
            continue

        split = split_frontmatter(text)
        if split is None:
            skipped.append(rel)
            continue
        fm = split.fm_text

        if read_fm_field(fm, "reaped_from_session") is not None:
            skipped.append(rel)  # already backfilled — idempotent no-op
            continue

        park_note = read_fm_field_unquoted(fm, "park_note")
        if not isinstance(park_note, str) or not park_note.strip():
            skipped.append(rel)  # no park_note at all — unrecoverable class
            continue

        match = _CRASH_ORPHAN_PARK_NOTE_RE.match(park_note.strip())
        if match is None:
            print(
                f"backfill_reaped_from_session: park_note shape not "
                f"recognized for {rel} — skipping: {park_note!r}",
                file=sys.stderr,
            )
            skipped.append(rel)
            continue

        sid = match.group(1)

        def mutate(old_text: str, _sid: str = sid) -> str:
            inner = split_frontmatter(old_text)
            if inner is None:
                raise MutateAbort(
                    f"backfill_reaped_from_session: frontmatter vanished "
                    f"under lock for {path}"
                )
            inner_fm = inner.fm_text
            if read_fm_field(inner_fm, "reaped_from_session") is not None:
                return old_text  # raced with a peer backfill — no-op
            inner_fm = insert_fm_field(
                inner_fm, "reaped_from_session", _sid, "deployment_state"
            )
            return rebuild(inner, inner_fm)

        try:
            locked_rmw(path, mutate, repo_root=worktree)
        except MutateAbort as exc:
            errors.append((rel, str(exc)))
            continue
        except OSError as exc:
            errors.append((rel, f"write failed: {exc}"))
            continue

        written.append(rel)

    return {"written": written, "skipped": skipped, "errors": errors}


def list_claims_by_session(sid: str, cwd: Optional[str] = None) -> List[Tuple[str, str]]:
    """Thin wrapper over ``list_claims_by_session_checked`` that discards its
    ``errors`` return arm. Kept byte-identical to its pre-existing contract
    (return type, signature, behaviour) for live callers and tests that
    assert ``== []`` on the no-git-repo path. Callers that need to
    distinguish "genuinely owns nothing" from "the claim store could not be
    resolved" (e.g. an unresolvable ``cwd``) must call the checked variant
    directly and inspect its ``errors`` list."""
    matches, _errors = list_claims_by_session_checked(sid, cwd)
    return matches


def list_claims_by_session_checked(
    sid: str, cwd: Optional[str] = None
) -> "Tuple[List[Tuple[str, str]], List[str]]":
    """Return ``([(class_, basename), ...], errors)`` for every claim dir under this
    repo's ``<sessions_dir>`` whose recorded ``session_id`` == ``sid``, across
    all three claim classes (``_CLAIM_SUBDIRS``). Reads the CLAIM-RECORD STORE
    directly (the ``session_id`` file inside each ``<class>-claims/<basename>/``
    dir) — NEVER the ``claimed_by``/``consumed_by`` frontmatter mirror. An
    unreadable or absent ``session_id`` file is "not a match", not an error —
    this is an ownership-index FILTER, not the tolerant diagnostic label
    ``_read_holder`` provides (that function's pid/``"unknown"`` fallback
    exists for a human-facing message, not for an identity predicate; a claim
    with no readable ``session_id`` cannot be attributed to any queried sid).

    Claim-record LIFECYCLE (which stages release a ``handoff-claims`` entry,
    named because C19's ownership index — and this function's own
    correctness for the AC20 double-count scenario — depends on it):

        - ship (``coordinator_core/ops/ceremony/tail_ops.py:900
          cs_release_artifact(common_dir, "plan", governing_plan_slug)``, called
          from ``wsc_tail.py``'s Step 6 — the native-port successor of the OLD
          ``wsc_commit.py``'s now-deleted ``_native_cs_release_artifact``,
          2026-07-29 kill-list op removal): SURVIVES. Hardcoded to
          ``artifact_class="plan"`` — releases the plan claim only; no
          handoff-class release call exists anywhere in this ship path.
          This wiring is verified by reading the call site, not by
          a test that executes it — invoking it requires the enclosing
          ``_commit_orchestration_sequence`` (PipelineContext + commit-outcome
          plumbing), assessed as disproportionate scaffolding for a unit test
          (Review: code-reviewer slice 2, 2026-07-27, Finding 1).
        - archive (``coordinator_core/ops/ceremony/consumed_handoff_stamp.py
          :381 post_commit_stamp_and_ship`` -> ``handoff_transition._ship``
          at ``coordinator_core/ops/handoff_transition.py:639``): SURVIVES.
          Neither function imports or calls ``release_artifact``/``claims.``
          anywhere. The ``_ship`` mutator itself IS exercised directly by
          ``coordinator_core/session/tests/test_claims.py::
          test_list_claims_by_session_survives_real_ship_call_site``, which
          calls it for real and asserts the handoff claim survives; the
          surrounding async ``post_commit_stamp_and_ship`` orchestration
          (liveness re-check, stamp-before-ship ordering) is not itself
          exercised by that test.
        - release (explicit ``drop``, ``coordinator_core/pickup_assemble/
          apply.py:~1029 release_artifact(class_, basename, cwd=str(root))``
          inside ``drop()`` at ``:967``, for ``class_ in ("handoff", "memo")``):
          RELEASED. This is the put-down/decline path, never a side effect
          of shipping.
        - reap (``coordinator_core/ops/session/reap.py:417
          _reap_orphaned_claims``, iterating ``_CLAIM_SUBDIRS``): released
          ONLY when the holding session is confirmed DEAD (fail-closed-to-keep
          on a live holder or a liveness-check exception). Not applicable at
          gate-read time for a still-running closing session — but is the
          mechanism that eventually reaps a handoff claim once its session
          truly exits.

    Consequence: a handoff claim survives both ship and archive, so an
    archived-mid-ceremony owned baton is still attributable to its session at
    gate-read time — neither closing path silently drops it from this index.
    """
    # Local import: avoids the same claims.py <-> ops.session.reap import-time
    # cycle documented on reconcile_dead_handoff_claim_frontmatter above —
    # ops.fleet._common's package (coordinator_core.ops) eager-imports every
    # op module at import time, including reap.py, which imports back from
    # this module.
    from coordinator_core.ops.fleet._common import _CLAIM_SUBDIRS

    matches: List[Tuple[str, str]] = []
    base = core.sessions_dir(cwd)
    if not base:
        return matches, [
            f"list_claims_by_session_checked: could not resolve sessions_dir "
            f"for cwd={cwd!r}"
        ]
    base_path = Path(base)
    for class_ in _CLAIM_SUBDIRS:
        class_dir = base_path / class_
        if not class_dir.is_dir():
            continue
        for claim_dir in sorted(class_dir.iterdir()):
            if not claim_dir.is_dir():
                continue
            sid_f = claim_dir / "session_id"
            try:
                held_sid = sid_f.read_text(encoding="utf-8").strip()
            except OSError:
                continue  # unreadable/absent session_id — not a match
            if held_sid == sid:
                matches.append((class_, claim_dir.name))
    return matches, []


def _read_holder(claim_dir: Path) -> str:
    """Holder label for a diagnostic message: the ``session_id`` file content
    (even if empty), else the ``pid`` file content, else ``"unknown"`` —
    mirrors the bash ``cat session_id || cat pid || echo unknown`` (where a
    present-but-empty session_id file short-circuits to ``""``, matching cat's
    exit-0 on an empty file)."""
    sid_f = claim_dir / "session_id"
    if sid_f.is_file():
        try:
            return sid_f.read_text(encoding="utf-8").strip()
        except OSError:
            pass  # unreadable session_id file — fall through to the pid rung
    pid_f = claim_dir / "pid"
    if pid_f.is_file():
        try:
            return pid_f.read_text(encoding="utf-8").strip()
        except OSError:
            pass  # unreadable pid file — fall through to "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# Sub-agent touch attribution + self-claim
# ---------------------------------------------------------------------------


def my_agent_touched(
    session_id: str, mode: str = "broadened", cwd: Optional[str] = None
) -> List[str]:
    """Port of ``_cs_my_agent_touched <session_id> [mode]`` (1181-1224).

    Return (as a list, one entry per path — the bash prints one per line) every
    repo-relative path touched by a sub-agent whose
    ``.agents/<aid>/em-session-id.txt`` back-pointer is in the candidate set
    for ``session_id``.

    ``mode`` (default ``"broadened"``):
      - ``broadened`` — candidate set = ``{session_id}`` UNION all live session
        ids (``liveness.live_session_ids``). Recovers the EM's own fan-out
        output on old Claude Code where an executor SessionStart can pollute
        the ``.current-session-id`` sentinel. Safe for the default (do_scoped)
        path because ``compute_scope`` re-subtracts other_sessions downstream,
        so any over-reach is self-correcting.
      - ``exact`` — candidate set = ``{session_id}`` only. Use on the blanket
        path where broadening would scoop a sibling EM's own sub-agent
        back-pointer into "own", causing the blanket to absorb the sibling's
        in-flight files.

    FAIL-OPEN: never raises (except the required-arg guard) — attribution is
    advisory and never blocks the caller. Empty back-pointer / missing
    ``touched.txt`` / unreadable file are all soft-skipped. Returns ``[]`` when
    not in a git repo or the ``.agents`` dir is absent, AND (degraded, still
    ``[]``, but with a logged stderr warning) when ``.agents`` exists but
    cannot be listed (e.g. permission-denied) — the sub-agent enumeration
    uses ``os.scandir()``, not ``Path.glob("*/")``, precisely so an
    unreadable dir raises ``OSError`` instead of silently reading as
    "genuinely zero sub-agent dirs" (the spec here is an AUTHORIZED BLANKET
    CAPTURE; a silently-missed dir is a completeness bug).

    ``session_id`` is REQUIRED — empty raises ValueError.

    RETURNS PATHS, NEVER RAW JOURNAL LINES. Each back-pointed agent dir's
    ``touched.txt`` is a ``T``/``R`` event journal
    (``scope.format_touch_event``), so its lines go through
    ``scope.project_self_scope`` — the same projection ``compute_scope``
    Step 1 and ``_release_from_touched_file`` use — before they leave this
    function. Projection is PER AGENT DIR because
    ``release_committed_claims`` appends each agent's ``R`` events into that
    agent's own file; a path whose last event there is ``R`` is released and
    is excluded. Legacy bare-path lines still parse as ``T`` and survive.

    Negative-spec: do NOT revert to appending raw lines. Every caller treats
    these as paths — ``safe_commit_offer._resolve_agent_touched_candidates``
    feeds them to ``_normalize_agent_touched_entry`` (whose contract is a
    path, and which returns a ``'T <ISO> …'`` line unchanged rather than
    rejecting it) and ``bash_guards.dispatch_checks`` unions them straight
    into ``my_scope``. Raw lines match no file, so the entire sub-agent
    fan-out leg contributes zero usable paths and files a session's own
    dispatched agents wrote get refused as ``unclaimed`` — the defect
    example-market-data-repo-em reported 2026-08-04
    (``cross-repo/inbox/2026-08-04-example-market-data-repo-em-agent-touched-journal-format-unmigrated.md``).

    Spec backlink: docs/plans/2026-06-22-authorized-blanket-orphan-capture-not-sibling-sweep.md § C1a Step 1.
    """
    if not session_id:
        raise ValueError("session_id required")

    base = core.sessions_dir(cwd)
    if not base:
        return []
    agents_dir = Path(base) / ".agents"
    if not agents_dir.is_dir():
        return []

    candidates = {session_id}
    if mode == "broadened":
        candidates |= set(liveness.live_session_ids(cwd))

    out: List[str] = []
    # bash pathname expansion (`base/.agents/*/`) is alphabetically sorted.
    #
    # NOTE: uses os.scandir(), NOT Path.glob("*/") -- Path.glob()'s selector
    # silently swallows PermissionError while walking (empirically re-
    # verified: a chmod-000 dir yields an empty iterator from glob(), no
    # exception), which would make an unreadable .agents/ indistinguishable
    # from "genuinely zero sub-agent dirs" -- the spec here is an
    # AUTHORIZED BLANKET CAPTURE, so a silently-missed dir is a completeness
    # bug, not a benign empty result. os.scandir() raises OSError as
    # expected; the failure is logged (module stays FAIL-OPEN -- attribution
    # is advisory and must never block the caller) rather than raised.
    try:
        agent_entries = sorted(os.scandir(agents_dir), key=lambda e: e.name)
    except OSError as exc:
        print(
            f"coordinator-session: cannot scan {agents_dir} — {exc}; "
            "agent-touch attribution may be INCOMPLETE (not the same as "
            "\"no sub-agent dirs\") — advisory only, not blocking the caller",
            file=sys.stderr,
        )
        return out

    for entry in agent_entries:
        agent_dir = Path(entry.path)
        if not agent_dir.is_dir():
            continue
        backptr = agent_dir / "em-session-id.txt"
        if not backptr.is_file():
            continue
        try:
            first = backptr.read_text(encoding="utf-8").splitlines()
        except OSError:
            first = []
        em_sid = first[0] if first else ""
        if not em_sid:
            continue  # malformed back-pointer; soft-skip
        if em_sid not in candidates:
            continue
        agent_touched = agent_dir / "touched.txt"
        if not agent_touched.is_file():
            continue
        try:
            lines = agent_touched.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue  # unreadable touched.txt — soft-skip per the FAIL-OPEN contract above
        for fpath in sorted(scope.project_self_scope(lines)):
            if fpath:
                out.append(fpath)
    return out


def self_claim(path: str, cwd: Optional[str] = None) -> bool:
    """Port of ``cs_self_claim <path>`` (726-758).

    Record ``path`` in the current session's ``touched.txt`` (best-effort
    attribution for tools that edit files outside the Edit/Write hook path).

    Normalization: ``path`` is run through
    ``coordinator_core.session.scope.normalize_touch_path`` before either
    append attempt below — the SAME normalize-or-skip-if-still-absolute
    contract ``scope.touch()`` (the PreToolUse hot path) already enforces.
    Do NOT re-inline a second normalization dialect here; a caller (e.g.
    ``coordinator_core.text.refresh_queries.process_file``) routinely
    invokes this with an ALWAYS-absolute path, so skipping this step is
    exactly how an absolute entry lands in ``touched.txt`` (example-doctrine-repo
    security-audit 2026-07-31: 240 such entries corroborated on disk). A
    path that is still absolute after normalization is SKIPPED (fail-open,
    advisory-only — this function's return-True-always contract already
    covers a skip; no touched.txt write happens for it).

    Resolution prefers the platform-injected session id directly —
    ``CLAUDE_SESSION_ID`` override, then ``CLAUDE_CODE_SESSION_ID`` (Claude
    Code >= ~2.1.150). That is O(1) and unambiguous. NOTE: this fast path reads
    ONLY those two env vars — it does NOT consult ``COORDINATOR_SESSION_ID`` or
    the cwd sentinel (that is deliberate; it is NOT the full 4-tier
    ``resolve_session_id`` chain). Only when NEITHER env var is set does it fall
    back to ``liveness.live_session_ids`` (O(n) over every session dir) and
    claim ONLY when EXACTLY ONE session is live (otherwise attribution is
    ambiguous — skip).

    ALWAYS returns True (bash ``return 0`` — fail-open: attribution is advisory
    and never blocks the caller). Appends via ``atomic_dedup_append``.

    ``path`` is REQUIRED — empty raises ValueError.
    """
    if not path:
        raise ValueError("path required")

    normalized = scope.normalize_touch_path(path, cwd)
    if normalized is None:
        # STILL absolute after normalization — skip (fail-open, advisory
        # only). Mirrors scope.touch()'s own guard; see this function's
        # docstring for why this cannot be inlined as a second dialect.
        print(
            f"coordinator-session: self-claim path {path!r} is still "
            f"absolute after normalization — skipping (would corrupt the "
            f"relative-path scope set)",
            file=sys.stderr,
        )
        return True
    entry = normalized

    # Fast path: platform tells us our own session id directly (these two env
    # vars ONLY — deliberately not the full resolve_session_id chain).
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
        "CLAUDE_CODE_SESSION_ID"
    ) or ""
    if sid:
        sdir = core.session_dir(sid, cwd)
        if not sdir:
            return True
        if not Path(sdir).is_dir():
            return True  # session dir gone — nothing to claim against
        try:
            atomic_dedup_append(str(Path(sdir) / "touched.txt"), entry)
        except (OSError, ValueError) as exc:
            # fail-open — self-claim attribution is advisory and must never
            # block the caller; surface the failure for debugging.
            print(
                f"coordinator-session: self-claim append failed for {entry} "
                f"(sid {sid}) — advisory only: {exc}",
                file=sys.stderr,
            )
        return True

    # Fallback (no session env var): enumerate live sessions; claim only when
    # EXACTLY ONE is live (otherwise attribution is ambiguous — skip).
    sids = liveness.live_session_ids(cwd)
    count = len(sids)
    if count == 0:
        print(
            f"coordinator-session: no active session found — "
            f"skipping self-claim for {entry}",
            file=sys.stderr,
        )
        return True
    if count > 1:
        print(
            f"coordinator-session: {count} live sessions (ambiguous) — "
            f"skipping self-claim for {entry}",
            file=sys.stderr,
        )
        return True
    sid = next(iter(sids))
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return True
    try:
        atomic_dedup_append(str(Path(sdir) / "touched.txt"), entry)
    except (OSError, ValueError) as exc:
        # fail-open — self-claim attribution is advisory and must never
        # block the caller; surface the failure for debugging.
        print(
            f"coordinator-session: self-claim append failed for {entry} "
            f"(sid {sid}) — advisory only: {exc}",
            file=sys.stderr,
        )
    return True
