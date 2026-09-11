"""
coordinator_core.engine_version — the engine self-version surface.

Purpose: resolve which commit of coordinator_core actually RAN, for
receipt-stamping and drift-probe consumers. This is a self-version surface,
not a repo-version surface.

Negative-spec (verbatim intent — do not "fix" this):
  - Resolves the engine's OWN git dir via `Path(__file__)`, NOT the caller's
    `repo_root`. A receipt `engine_sha` identifies the commit the running
    engine's source is CHECKED OUT AT, which for a vendored consumer
    correctly resolves the vendored copy's SHA (Decision A) — never the
    consuming repo's own HEAD. `engine_sha` alone does NOT claim the code
    that ran matches that commit byte-for-byte: on a dirty tree (the norm
    on this box — see `resolve_engine_dirty()`) the working copy can differ
    from what `git show <engine_sha>` reconstructs. `resolve_engine_dirty()`
    is the discriminator a consumer must check alongside `engine_sha` to
    know whether the sha actually identifies the code that ran.
  - Robust to both live (this repo checked out directly) and vendored
    (coordinator_core copied/symlinked into a consumer repo) topology,
    because `Path(__file__)` always points at the copy that is executing.
    Known limitation (Review: code-reviewer, Finding 2): this holds for
    copy-based vendoring; for symlink-based vendoring, `.resolve()` follows
    the symlink back to the source checkout and reports the SOURCE's current
    HEAD, not the frozen SHA the symlink was created to pin. Treat the
    symlink-robustness claim as unverified until a fleet census confirms no
    consumer vendors via symlink.

Public surface (pinned contract):

    def resolve_engine_sha() -> str | None: ...
    def resolve_engine_dirty() -> bool | None: ...
    MIN_KNOWN_GOOD_SHA: str

Spec backlink: pln-claude-klabauter-engine-version-surface--c130a8
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git.run import run_git
from coordinator_core.git_scope import scoped_git_env

# Committed floor-provenance guardrail. This value is version-controlled and
# reviewable in this file's own git history — it is NEVER runtime-fetched
# from a remote/registry. It is bumped as part of the Definition of Done of
# any fix consumers were demonstrably re-hitting (the wsc_resolve/6fdc7b4
# class), so the floor tracks known-bad lines rather than "latest".
#
# This value is the wsc_resolve phantom-handoff fix that motivated the plan.
MIN_KNOWN_GOOD_SHA: str = "6fdc7b4de770dc1c996b3c2a42bf2c7984dd67c9"


def resolve_engine_sha() -> str | None:
    """Resolve the git HEAD SHA of the running coordinator_core copy.

    Returns the stripped 40-char hex HEAD SHA of the git repo containing
    this file, resolved via `Path(__file__)` (see module negative-spec).

    Returns None (the sentinel for "unresolvable") and NEVER raises when:
      - git is not installed / not on PATH (FileNotFoundError/OSError),
      - the engine's own directory is not inside a git repo,
      - the subprocess exits non-zero for any other reason,
      - the subprocess exceeds its bound,
      - the subprocess output cannot be decoded (handled by the shared
        runner's `errors="replace"`, so it can no longer raise).

    Bound by `git.run.LOCAL_PLUMBING_BUDGET_SECS`. This site carried its own
    `timeout=5` until the G7 migration; nothing measured that 5, and
    `git -C <repo> rev-parse HEAD` is 26.9 ms on this box (DR-344 § 4).

    Negative-spec (git scoping): `git -C <engine_dir>` sets only the working
    directory. An inherited `GIT_DIR` — git exports one to every hook it runs,
    commonly as the relative `"."` — still wins over directory-based discovery,
    so an unscoped probe returns the HEAD of whatever repo the hook was running
    in and reports it as the running engine's version; `engine_drift` then
    computes an ancestry verdict from that foreign sha and can call a current
    engine "behind". The repo-scoping environment is therefore stripped via
    `git_scope.scoped_git_env()` so that `-C` genuinely selects the engine's own
    checkout. (The git-dir confinement check `git_scope` also offers is NOT used
    here: it confines to a repo ROOT, and `engine_dir` is deliberately a
    subdirectory of whichever tree the engine ships in.) See
    `coordinator_core/git_scope.py`.
    """
    engine_dir = Path(__file__).resolve().parent
    result = run_git(
        ["-C", str(engine_dir), "rev-parse", "HEAD"],
        env=scoped_git_env(),
    )
    if not result.ok:
        return None

    return result.stdout.strip()


def resolve_engine_dirty() -> bool | None:
    """Report whether the running engine's OWN source differs from HEAD.

    `resolve_engine_sha()` names a commit; it does not, by itself, prove the
    code that ran matches that commit's tree, because this working tree is
    dirty essentially continuously (50-70 concurrent sessions committing
    every couple of minutes). This resolver is the discriminator: a receipt
    stamped with `engine_sha` plus `engine_dirty=False` genuinely identifies
    the code that ran; `engine_dirty=True` (or None, meaning unresolvable)
    means `git show <engine_sha>` reconstructs a DIFFERENT program than the
    one that actually executed.

    Scoped to the engine's own source directory only (`git status --porcelain
    -- .` run with cwd pinned to `engine_dir`), NOT the whole repository — a
    dirty `docs/` or `state/` tree must not flip this to True when the engine
    module tree itself is clean, or every receipt on this box would read
    dirty regardless of whether the executing code actually changed.

    Returns None (the sentinel for "unresolvable") and NEVER raises, mirroring
    `resolve_engine_sha()`'s failure posture exactly:
      - git is not installed / not on PATH (FileNotFoundError/OSError),
      - the engine's own directory is not inside a git repo,
      - the subprocess exits non-zero for any other reason,
      - the subprocess exceeds its bound,
      - the subprocess output cannot be decoded (see `resolve_engine_sha`).

    Bound by `git.run.LOCAL_PLUMBING_BUDGET_SECS`, same migration and same
    retired `timeout=5` as `resolve_engine_sha`.

    Negative-spec (git scoping): same `GIT_DIR`-stripping rationale as
    `resolve_engine_sha()` applies identically here — an inherited `GIT_DIR`
    (git exports one to every hook it runs, commonly as the relative ".")
    still wins over directory-based discovery, so an unscoped probe would
    report the porcelain status of whatever repo the hook was running in,
    not the running engine's own checkout. `git_scope.scoped_git_env()`
    strips it so `-C` genuinely selects the engine's own checkout. See
    `coordinator_core/git_scope.py`.
    """
    engine_dir = Path(__file__).resolve().parent
    result = run_git(
        ["-C", str(engine_dir), "status", "--porcelain", "--", "."],
        env=scoped_git_env(),
    )
    if not result.ok:
        return None

    return bool(result.stdout.strip())


#: Process-wide memo for `engine_build()`. One entry, set on first call and never
#: invalidated: a process that re-resolved would report two different builds for
#: one run, which is exactly the ambiguity this surface exists to remove. The
#: warm server's lifetime is the memo's lifetime, and a source edit under a live
#: server is `warm.skew`'s axis to detect, not this one's to paper over.
_BUILD_MEMO: dict | None = None


def engine_build() -> dict:
    """The build identity a verdict-returning op stamps on its own answer.

    Returns ``{"engine_sha": str | None, "engine_dirty": None}`` — the commit the
    running engine copy is checked out at, read from `.git` DIRECTLY, plus a
    `engine_dirty` that is always `None` here and is explained below. Never
    raises; `engine_sha` is `None` when the running copy is not inside a repo
    at all.

    Purpose, stated as the consumer's question: a verdict op that reports a
    DEFECT without naming its own build makes "your engine predates this leg"
    indistinguishable from "your plan is wrong". Those have opposite repairs —
    one waits for a publish, the other edits the plan — and a consumer that
    cannot tell them apart follows the refusal's prescribed repair into
    fabricating a declaration to satisfy a check its engine simply does not
    carry yet. Measured: `plan.prep_gate` on a mirror predating the
    `created_roots` exemption refused 7 example-game-repo plans, two of which already
    carried the exact declaration the refusal text prescribed
    (example-game-workbench-repo-00, 2026-09-11). A sha the consumer can
    ancestry-check is the whole repair.

    Budget: 0 spawns. 0.32 ms to resolve, 5.5 ms on the very first call in a
    process that has not yet imported the two git readers (their import, not
    the read), and ~0 on every call after — measured, this box.
    DELIBERATELY NOT `resolve_engine_sha()` + `resolve_engine_dirty()`, which
    answer richer questions and cost 23.8 ms + 69.3 ms of `git` (measured, this
    box). Their callers pay that once per receipt; this one is stamped on EVERY
    verdict, and `plan.prep_gate`'s callers gate a plan at a time — 93 ms times
    a corpus, on ops whose own negative-spec is zero spawns and no git. A
    provenance field that breaks the budget of the op it annotates would be
    retired by DR-344 before a consumer ever read it.

    `engine_dirty` is `None` — "not determined" — and is carried rather than
    dropped so a reader is told which half is missing instead of inferring a
    clean tree from a bare sha. Answering it needs `git status`, which is the
    spawn this function exists to avoid; `resolve_engine_dirty()` is that
    answer for a caller that wants it, and `warm.skew` is where a live server's
    source drift is actually detected.

    Negative-spec:
      - Does NOT spawn, and does NOT invoke git. That is the whole design
        constraint, not an incidental property: see Budget.
      - Does NOT re-resolve, and takes no invalidation argument. See `_BUILD_MEMO`.
      - Does NOT report the CONSUMING repo's HEAD. `Path(__file__)`-derived, per
        this module's own negative-spec — a vendored or published copy correctly
        reports the sha of the copy that is executing, which for a klabauter
        consumer is the mirror's own commit and is exactly what it needs to
        ancestry-check.
      - Does NOT claim the sha identifies the code byte-for-byte. It names a
        commit; `engine_dirty` is the discriminator, and here it is unanswered.
    """
    global _BUILD_MEMO
    if _BUILD_MEMO is None:
        _BUILD_MEMO = {"engine_sha": _engine_head_sha(), "engine_dirty": None}
    return dict(_BUILD_MEMO)


def _engine_head_sha() -> str | None:
    """The running engine copy's HEAD sha, read from `.git`, no process.

    Climbs from this file to the enclosing repo (`repo_root._walk_for_repo`,
    the same climb `resolve_git_dir` cannot do for a SUBDIRECTORY) and reads
    the sha with `git_state.head_sha`, which follows HEAD's one ref hop and
    falls back to `packed-refs`. Both are existing spawn-free readers; nothing
    here reimplements a git format.

    Returns `None` — never raises — when the engine copy is not in a repo, when
    HEAD is unreadable, or when the branch is unborn (`head_sha`'s own `None`).
    """
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.repo_root import _walk_for_repo

    try:
        found = _walk_for_repo(Path(__file__).resolve().parent)
        if found is None:
            return None
        return head_sha(found[1])
    except OSError:
        return None
