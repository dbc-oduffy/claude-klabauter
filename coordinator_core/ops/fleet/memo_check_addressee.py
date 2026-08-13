"""
coordinator_core.ops.fleet.memo_check_addressee — memo.check_addressee COMPUTE_ONLY UDS op handler.

Purpose: answer "is a memo's `to:` value addressed to THIS (calling) repo?" —
a **path-based** MATCH / MISMATCH / UNRESOLVED verdict, the claude-klabauter port of the
Coordinator-claude CLI `cross-repo-memo --check-addressee` handler that `/pickup`'s M-addr
guard consumes to decide whether a memo is this repo's to action. Verdict is
computed by comparing resolved REPO PATHS, not receiver-id strings — the same
alias can resolve to the same repo path from two different spellings, and
that must still read as MATCH.

Registered as "memo.check_addressee" via @register_op; COMPUTE_ONLY
classification and the `common_dir` `_OP_KEY_SCOPE` entry are wired alongside
memo_list.py's own entries (this op additionally needs the caller's own repo
to compute self_root, hence common_dir-scoping — unlike memo.list's "none").

Spec backlink:
    state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md
    (ratifying spinoff — carries DR-047 addressee-guard corrected behaviour).
    Parity source: coordinator-claude CLI `cross-repo-memo --check-addressee` handler
    (coordinator-claude/coordinator/bin/cross-repo-memo:3249-3289).
    Resolver: coordinator_core/ops/fleet/_memo_resolver.py (resolve_receiver_inbox,
    suggest_nearest_receiver, read_redirect_aliases).

Negative-spec:
  - Does NOT compare receiver-id strings — the verdict is path-based
    (self_root vs to_root), which is the whole reason an alias resolving to
    the same repo path counts as MATCH.
  - Does NOT hardcode any redirect-alias literal — the redirect set is read
    declaratively via `read_redirect_aliases()`. Coordinator-claude promoted
    `identity.redirectAliases` into the manifest 2026-07-21; the redirect
    branch fires whenever a caller's `to` normalizes into that set.
  - Does NOT write any file, create any directory, or run any git command —
    provably side-effect-free, same posture as memo_list.py.
  - Does NOT accept `dry_run: false` — memo.check_addressee has no act mode;
    a caller that passes `dry_run: false` gets a setup-error envelope.
  - Does NOT fall back to a folder scan on registry-read failure — propagates
    `_memo_resolver.RegistryReadError`/`AmbiguousReceiverError` as a fail-loud
    setup-error envelope, identical posture to memo_send.py/memo_list.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    read_central_receiver_ids,
    read_redirect_aliases,
    read_registry_repos,
    resolve_receiver_inbox,
    same_repo_path as _same_path,
    suggest_nearest_receiver,
)

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field (memo.check_addressee is a single-mode
# op — it only ever returns the dry_run envelope, matching memo_list.py's pattern).
_MODE = "check_addressee"


def _validate_check_addressee_params(params: dict):
    """Validate memo.check_addressee params; return (dry_run, to) or a setup-error dict.

    Required: dry_run (bool) — must be True; memo.check_addressee has no act mode.
    Required: to (str, non-empty after strip) — the memo's `to:` value to check.
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.check_addressee: dry_run must be bool, got "
            + repr(type(dry_run).__name__),
        )
    if dry_run is False:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.check_addressee: dry_run must be true — memo.check_addressee is "
            "a pure read op with no act mode (it never writes, regardless of this flag).",
        )

    to = params.get("to")
    if not isinstance(to, str) or not to.strip():
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.check_addressee: to is required and must be a non-empty string "
            "(the memo's `to:` value to check)",
        )

    return dry_run, to


def compute_check_addressee_candidate(self_root: Path, to: str) -> dict:
    """Sync compute core for the addressee verdict — path-based MATCH /
    MISMATCH / UNRESOLVED, exactly the logic the `memo.check_addressee` op
    handler below wraps in `async def`/`register_op`.

    Factored out (2026-07-26, subprocess-elision spinoff — see module
    docstring's spec backlink) so an IN-PROCESS caller (e.g.
    `coordinator_core.pickup_assemble.compute_addressee_gate`, the `/pickup`
    M-addr guard) can call the verdict computation directly, with NO event
    loop and NO subprocess — the op handler has no `await` in its body, so
    spinning up `asyncio.run()` just to reach this computation would be pure
    overhead, not a real async boundary. THE ONE PLACE this verdict logic
    lives; `_memo_check_addressee` and `compute_addressee_gate` both call it
    rather than each carrying their own copy.

    Params:
        self_root: the CALLING repo's root (already resolved to the main
            worktree — callers pass `main_worktree_root(...)`, this function
            does no further resolution).
        to: the memo's `to:` value to check against self.

    Returns the candidate dict (`id`, `receiver`, `verdict`, `self_repo`,
    `to_repo`, `resolved`, `note`) — same shape `build_dry_run_result`'s
    single-candidate list carries today.

    Raises:
        RegistryReadError: propagated from the underlying registry reads on a
            genuine registry-read failure — callers MUST fail loud (or, for a
            display-only consumer, degrade to "not checked"), never silently
            fall back to a folder scan.
        AmbiguousReceiverError: a central receiver id fans in to more than one
            distinct registered `repos.*` key.
    """
    normalized = to.strip().lower()
    # Review: code-reviewer (Finding 3) — tracks which central id the redirect
    # branch actually resolved against, so note-selection below checks the
    # RESOLVED id, not the caller's original `to`/`normalized`.
    redirected_central_id: Optional[str] = None
    # Review: code-reviewer (Finding 4) — cache the manifest read here so the
    # UNRESOLVED branch below reuses it when the redirect branch already
    # read it, instead of re-opening/re-parsing the manifest a second time
    # in the same call. Lazily bound (not read unconditionally at function
    # top) so the common MATCH/MISMATCH path — which needs central_ids in
    # neither branch — pays for zero manifest reads, same as before this
    # fix; this function is the hot-path compute core a 1098ms->2.5ms
    # optimization was built around.
    central_ids: Optional[set[str]] = None
    redirect_aliases = read_redirect_aliases()
    if normalized in redirect_aliases:
        central_ids = read_central_receiver_ids()
        if central_ids:
            # Review: code-reviewer (Finding 1) — manifest-driven, not a
            # hardcoded literal: derive the redirect target from the
            # manifest's own declared central-id set, taking the FIRST id
            # in sorted order to match resolve_receiver_inbox's own
            # `for cid in sorted(central_ids)` iteration.
            redirected_central_id = sorted(central_ids)[0]
            _, to_root, all_repos = resolve_receiver_inbox(redirected_central_id)
        else:
            # No central ids declared in the manifest at all — nothing to
            # redirect to; degrade cleanly to UNRESOLVED rather than crash
            # on an empty sorted()[0].
            to_root = None
            all_repos = read_registry_repos()
    else:
        _, to_root, all_repos = resolve_receiver_inbox(to)

    note = None
    if to_root is None:
        verdict = "UNRESOLVED"
        # Review: code-reviewer (Finding 3) — check the actually-resolved
        # central id when the redirect branch fired, not the original `to`.
        central_check_id = (
            redirected_central_id if redirected_central_id is not None else normalized
        )
        if central_ids is None:
            central_ids = read_central_receiver_ids()
        if central_check_id in central_ids:
            note = (
                f"receiver {to!r} is a central receiver id "
                f"(identity.centralReceiverIds) that resolves to the coordinator-claude "
                f"repo, but none of the manifest's central receiver ids is "
                f"registered in the machine-local registry."
            )
        else:
            suggestion = suggest_nearest_receiver(to, all_repos)
            suggestion_clause = f" Did you mean {suggestion!r}?" if suggestion else ""
            note = (
                f"receiver {to!r} does not resolve to a known repo on this "
                f"machine.{suggestion_clause}"
            )
    elif _same_path(self_root, to_root):
        verdict = "MATCH"
    else:
        verdict = "MISMATCH"

    return {
        "id": to,
        "receiver": to,
        "verdict": verdict,
        "self_repo": str(self_root),
        "to_repo": str(to_root) if to_root is not None else None,
        "resolved": verdict != "UNRESOLVED",
        "note": note,
    }


#: Exit codes mirroring the coordinator-claude CLI's `--check-addressee` branch
#: (`cross-repo-memo:4088-4105`) — `format_addressee_message` returns one of
#: these three; there is no fourth verdict string in `compute_check_addressee_
#: candidate`'s output, so no other exit code is ever produced by this path.
ADDRESSEE_EXIT_MATCH = 0
ADDRESSEE_EXIT_MISMATCH = 3
ADDRESSEE_EXIT_UNRESOLVED = 4


def format_addressee_message(
    self_em: str, self_root: Path, to_val: str, candidate: dict
) -> tuple[str, int]:
    """Render the `self:`/`to:`/`verdict:` three-line text byte-for-byte
    against the coordinator-claude CLI's `--check-addressee` stdout (`cross-repo-memo:
    4088-4105`), plus the matching exit code. THE ONE formatter for this
    prose — every in-process consumer of `compute_check_addressee_candidate`
    calls this rather than re-deriving the verdict lines.

    `to_root` is read from `candidate["to_repo"]` (already a `str` or `None`)
    — never re-derived — so the printed `to:` line and the computed verdict
    can never disagree about what was resolved.
    """
    verdict = candidate.get("verdict")
    to_root = candidate.get("to_repo")
    lines = [
        f"self: {self_em} ({self_root})",
        f"to:   {to_val} ({to_root if to_root is not None else 'UNRESOLVED'})",
    ]
    if verdict == "MATCH":
        lines.append("verdict: MATCH — this memo is addressed to this repo")
        return "\n".join(lines), ADDRESSEE_EXIT_MATCH
    if verdict == "MISMATCH":
        lines.append(
            f"verdict: MISMATCH — this memo is addressed to {to_val}, not this "
            f"repo ({self_em})"
        )
        return "\n".join(lines), ADDRESSEE_EXIT_MISMATCH
    # UNRESOLVED (or any other/unexpected verdict string — treat as
    # unresolved rather than silently falling through as a MATCH).
    lines.append(
        f"verdict: receiver '{to_val}' does not resolve to a known repo on "
        f"this machine"
    )
    return "\n".join(lines), ADDRESSEE_EXIT_UNRESOLVED


@register_op("memo.check_addressee")
def _memo_check_addressee(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'memo.check_addressee' COMPUTE_ONLY UDS op handler.

    Resolve whether a memo's `to:` value is addressed to the CALLING repo —
    a path-based MATCH / MISMATCH / UNRESOLVED verdict. Never writes, commits,
    or reaches the network; provably side-effect-free.

    repo_root is the git common_dir (op is common_dir-scoped — see op_scopes.py)
    — self_root is derived via `main_worktree_root(Path(repo_root))`, never from
    params.repo_root directly (Key Decision 5 precedent, memo_send.py:1100-1103).

    Params:
        dry_run (bool, required): must be True — memo.check_addressee has no act mode.
        to      (str, required):  the memo's `to:` value to check against self.

    Returns:
        The `build_dry_run_result` envelope (`exit_code:0, dry_run:true`) with one
        candidate dict carrying the verdict; or a `build_setup_error_result`
        envelope (`exit_code:1`) on bad params, missing repo_root, or a genuine
        registry-read failure. exit_code is 0 for ANY computed verdict — op
        success is distinct from the addressee verdict itself (the CLI facade
        maps MATCH/MISMATCH/UNRESOLVED to distinct process exit codes at cutover).

    This handler carries no `await` in its body — `compute_check_addressee_
    candidate` is pure sync compute — and is a plain `def` (2026-08-07
    zero-await fix; `dispatch_message`'s sync branch offloads it via
    `asyncio.to_thread`, restoring `wait_for`'s ability to actually bound it).
    An in-process caller that wants the verdict without an event loop calls
    `compute_check_addressee_candidate` + `format_addressee_message` directly
    (see `coordinator_core.pickup_assemble.compute_addressee_gate`).
    """
    validated = _validate_check_addressee_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    dry_run, to = validated

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.check_addressee: cannot resolve self repo — repo_root (git "
            "common_dir) not supplied (op is common_dir-scoped)",
        )
    self_root = main_worktree_root(Path(repo_root))

    try:
        candidate = compute_check_addressee_candidate(self_root, to)
    except RegistryReadError as exc:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.check_addressee: machine-local registry could not be read: "
            f"{exc.reason} (no folder-scan fallback — fix the registry file or "
            f"re-run machine-local setup).",
        )
    except AmbiguousReceiverError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.check_addressee: {exc}")

    return build_dry_run_result(_MODE, [candidate])
