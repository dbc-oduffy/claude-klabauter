"""
coordinator_core.ops.sync_main — branch-creation-invariant enforcer
(local main == origin/main).

Purpose: DR-059 bash-to-naked-Python port of the example-doctrine-repo-owned script
`coordinator/bin/sync-main.sh` (~124 lines). Every branch-creation site in the
coordinator pipeline calls this before `git checkout -b` — after this module's
`main()` returns 0, local `main` == `origin/main` regardless of which branch
the working tree is on, so callers can trust `git checkout -b new-branch main`.

Spec backlink: archive/specs/2026-05-01-orphan-branch-prevention.md § 1.1.5
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Port of: sync-main.sh (example-doctrine-repo b5a4192c, 2026-07-20)

Negative-spec: does NOT create branches, does NOT push, does NOT merge
feature/work branches, does NOT touch anything other than the main ref.

Exit codes (parity-critical, preserved from the bash oracle):
    0 — success (main synced, or silently skipped when not in a git repo or
        origin/main unreachable), OR >50-commits-behind non-strict warning
    1 — local main is ahead of origin/main (should never happen — investigate
        before branching), OR fast-forward pull failed, OR --strict mode hit
        the >50-commits-behind threshold, OR unknown CLI argument
    2 — (not used by this port; reserved by the bash oracle's own convention
        for "not a git repo" cases, which this module instead maps to exit 0
        per the oracle's own `exit 0` on that branch — see `_is_git_repo`)

Behavior-preservation notes (read alongside the bash source):
  - On branch `main`: `git fetch origin main` then check local-ahead-of-origin
    (hard error if so), then `git pull --ff-only origin main` (hard error on
    failure).
  - On any other branch: `git fetch origin main:main` (refspec form, updates
    the local `main` ref without checking it out). If that refspec fetch
    fails (typically because local main is ahead of origin/main — a refspec
    fetch cannot fast-forward in that direction), fall back to a bare
    `git fetch origin main` and check local-ahead-of-origin explicitly,
    surfacing the same hard error the oracle does.
  - After sync, if the current (non-main) branch is >50 commits behind the
    now-updated `main`, warn (or hard-error under --strict).

Known faithfully-reproduced oracle quirk (code-reviewer finding, confirmed
present verbatim in the bash oracle at 0bfe3269:coordinator/bin/sync-main.sh):
in the non-main-branch refspec-fetch-failed-but-not-ahead fallback, both the
bash oracle and this port issue a redundant duplicate `git fetch origin main`
(no-op — second call has no different args/effect than the first), and the
subsequent "Local main ref is now at <ref>" message is printed unconditionally
even though `git fetch origin main` (no refspec) only advances the
remote-tracking ref (`origin/main`), never `refs/heads/main` — so on this
fallback path the message can describe the stale, unchanged local `main` ref
as though it were just updated. Not fixed here: fixing the message text would
diverge textual/observable behavior from the bash oracle this module is a
parity port of — a byte-parity-vs-message-honesty tradeoff, not a portability
bug. See faithful-oracle-repro discipline in the review-integration doctrine.
"""

from __future__ import annotations

import subprocess
import sys

from coordinator_core.win_portability import no_console_creationflags


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _is_git_repo() -> bool:
    r = _git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0


def _current_branch() -> str:
    r = _git("branch", "--show-current")
    return r.stdout.strip() if r.returncode == 0 else ""


def _rev_list_count(range_spec: str) -> int:
    r = _git("rev-list", "--count", range_spec)
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        print(f"skip: _rev_list_count: return int(r.stdout.strip()) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _origin_main_reachable() -> bool:
    r = _git("ls-remote", "--exit-code", "origin", "main")
    return r.returncode == 0


def main(argv: list[str]) -> int:
    quiet = False
    strict = False

    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--quiet":
            quiet = True
        elif arg == "--strict":
            strict = True
        elif arg in ("--help", "-h"):
            sys.stdout.write(_HELP_TEXT)
            return 0
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            return 1
        i += 1

    if not _is_git_repo():
        return 0

    def info(msg: str) -> None:
        if not quiet:
            print(f"[sync-main] {msg}", file=sys.stderr)

    def warn(msg: str) -> None:
        print(f"[sync-main] WARNING: {msg}", file=sys.stderr)

    def die(msg: str) -> int:
        print(f"[sync-main] ERROR: {msg}", file=sys.stderr)
        return 1

    if not _origin_main_reachable():
        info("origin/main not reachable — skipping sync (offline or non-standard remote)")
        return 0

    current_branch = _current_branch()

    if current_branch == "main":
        info("On main — fetching and fast-forwarding...")
        _git("fetch", "origin", "main")

        local_ahead = _rev_list_count("origin/main..HEAD")
        if local_ahead > 0:
            return die(
                f"Local main is {local_ahead} commit(s) ahead of origin/main. "
                "This should never happen — investigate before branching. "
                "(Did a previous operation commit directly to main?)"
            )

        pull = _git("pull", "--ff-only", "origin", "main")
        if pull.returncode != 0:
            return die(
                "Fast-forward pull failed. Local main has diverged from "
                "origin/main in a way sync-main.sh cannot resolve automatically."
            )

        head_short = _git("rev-parse", "--short", "HEAD")
        info(f"main is now at {head_short.stdout.strip()}")

    else:
        info(f"On branch '{current_branch}' — updating local main ref from origin...")
        refspec_fetch = _git("fetch", "origin", "main:main")
        if refspec_fetch.returncode != 0:
            # fetch with refspec fails when local main is ahead of origin/main
            _git("fetch", "origin", "main")
            local_ahead = _rev_list_count(
                "refs/remotes/origin/main..refs/heads/main"
            )
            if local_ahead > 0:
                return die(
                    f"Local main is {local_ahead} commit(s) ahead of origin/main. "
                    "Investigate before branching."
                )
            # Review: code-reviewer -- dropped a redundant duplicate
            # `_git("fetch", "origin", "main")` here (byte-identical to the
            # one at the top of this except-block, same args/no different
            # effect). Pure dead-code removal — the resulting git ref state
            # is unchanged, only a wasted network round-trip is avoided.

        main_short = _git("rev-parse", "--short", "main")
        if main_short.returncode == 0:
            ref_text = main_short.stdout.strip()
        else:
            origin_main_short = _git("rev-parse", "--short", "origin/main")
            ref_text = origin_main_short.stdout.strip()
        info(f"Local main ref is now at {ref_text}")

    if current_branch and current_branch != "main":
        behind = _rev_list_count("HEAD..main")
        if behind > 50:
            if strict:
                return die(
                    f"Current branch is {behind} commits behind main. "
                    "Resolve divergence before proceeding (--strict mode)."
                )
            warn(
                f"Current branch '{current_branch}' is {behind} commits behind "
                "main. Consider rebasing or merging main before creating a "
                "new branch from here."
            )

    info("sync-main complete.")
    return 0


_HELP_TEXT = """\
Usage: sync-main.sh [OPTIONS]

Ensures local main == origin/main before any branch creation.

Options:
  --quiet     Suppress normal informational output
  --strict    Treat >50-commits-behind warning as a hard error (exit 1)
  --help      Show this help

Exits 0 on success, non-zero when divergence cannot be resolved silently.
Skips silently when not inside a git repository.
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
