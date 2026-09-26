
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
            _git("fetch", "origin", "main")
            local_ahead = _rev_list_count(
                "refs/remotes/origin/main..refs/heads/main"
            )
            if local_ahead > 0:
                return die(
                    f"Local main is {local_ahead} commit(s) ahead of origin/main. "
                    "Investigate before branching."
                )

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
