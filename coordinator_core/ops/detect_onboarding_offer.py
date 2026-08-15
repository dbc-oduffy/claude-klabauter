"""
coordinator_core.ops.detect_onboarding_offer — session-preflight onboarding currency detector.

Purpose: detect whether the cwd repo needs /repo-setup — either because it is
unonboarded, or because its coordinator-currency stamp is stale against the current
schema. /repo-setup is idempotent: lazy-creation skips already-present artifacts and
re-stamps currency. Emits an offer line if action is warranted. Consumed by
/workday-start Step 1.10 at session-preflight cadence (NOT a PreToolUse hook — a
per-repo-stable fact, not a per-command hot path).

Decision rules (all silent on no-offer path):
    1. Not a git repo               -> silent (nothing to onboard against)
    2. Distribution repo ((b) class) -> silent (session infra doesn't belong there)
    3. Dismissal sentinel present    -> silent (user already saw offer, dismissed it)
    4. Unonboarded (no archive/ and no state/workstreams/ dir) -> emit OFFER line (unonboarded)
    5. Stale (probe returns drift/unstamped)    -> emit OFFER line (stale)
    6. Current                       -> silent (healthy)

Distribution repo detection (mirrors skills/repo-setup/SKILL.md):
    2+ of these dirs in .gitignore -> treat as distribution repo:
    tasks/, archive/, state/handoffs/

Dismissal sentinel: <repo>/.git/coordinator-onboarding-dismissed
    (inside .git/ -- gitignore-additive, never shows in `git status`, per-repo)

Offer shape (eager-agent-calibration.md): lead with the better alternative, not the
problem. Format is a plain stdout line the Step 1.10 caller surfaces.

Currency probe: reuses coordinator_core.ops.probe_onboarding_currency.
coordinator_currency_probe(repo_root, plugin_root) in-process (no subprocess) --
that function is itself a faithful reimplementation of BOTH the bash oracle's
bin/probe-onboarding-currency.sh AND the shared lib/coordinator-currency.sh
function it inlined (see that module's own docstring), so calling it directly for
both the "primary" and "fallback" branches below is exact-fidelity, not a
behavior change -- the bash oracle's two branches computed the identical
underlying classification via two different bash entry points into the same
logic.

Exit codes (main(), advisory-only, never blocks):
    0 -- always. This is a best-effort/advisory probe (mirrors the bash oracle's
         own "Callers MUST NOT infer meaning from exit code" contract) -- a
         claude-klabauter import/transport failure at the TRAMPOLINE layer degrades to a
         loud stderr message + exit 0 (see the .sh trampoline), never a nonzero
         exit from this module. There is no business-failure state to encode.

Output (stdout): offer line when action warranted; nothing when silent. Callers
MUST NOT infer meaning from exit code -- check stdout content.

Port of: detect-onboarding-offer.sh (DoE 432e3285, 2026-07-22)
Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 3

Negative-spec (retired bash-oracle surface -- deliberately NOT reproduced here):
    - The bash oracle's SOURCE mode (`source detect-onboarding-offer.sh` then call
      `detect_onboarding_offer` as a shell function) is dropped. Grepped every
      real caller of the DoE .sh (commands/workday-start.md, bin/tests/
      test-detect-onboarding-offer.sh) -- both invoke it as `bash <script>`
      (subprocess), never `source`. SOURCE mode was unused dead API surface, not
      load-bearing behavior; this is not a scope-drop regression.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import List, Optional

from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core.win_portability import is_executable, no_console_creationflags
from coordinator_core.ops.probe_onboarding_currency import (
    _strip_one_trailing_slash,
    coordinator_currency_probe,
)

_CREATIONFLAGS = no_console_creationflags()


_SESSION_DIR_PATTERNS = ("tasks", "archive", "state/handoffs")


def _count_ignored_session_dirs(repo_root: str) -> int:
    """Count how many of the three session-infra dirs are gitignored (bare or
    trailing-slash form, anchored full-line match -- mirrors the bash oracle's
    three `grep -qE '^(<dir>/?|/<dir>/?)$'` checks)."""
    gitignore = os.path.join(repo_root, ".gitignore")
    try:
        with open(gitignore, "r", encoding="utf-8", errors="replace") as fh:
            lines = [line.rstrip("\n").rstrip("\r") for line in fh]
    except OSError as exc:
        # Review: code-reviewer (Finding 1) — replaced a stringified fragment
        # of the try-block's own source with a plain human sentence.
        print(f"skip: could not read {gitignore}: {exc}", file=sys.stderr)
        return 0

    count = 0
    for dir_name in _SESSION_DIR_PATTERNS:
        for line in lines:
            if line in (dir_name, f"{dir_name}/", f"/{dir_name}", f"/{dir_name}/"):
                count += 1
                break
    return count


def _is_distribution_repo(repo_root: str) -> bool:
    return _count_ignored_session_dirs(repo_root) >= 2


def _is_onboarded(repo_root: str) -> bool:
    # Review: coordinator:code-reviewer (P1) -- `state/workstreams/` alone is
    # empirically unsound: it is created lazily by queue_append.py on first
    # workstream event and no install/scaffold path provisions it, so 11 of
    # 12 currently-onboarded sibling repos in the fleet have no
    # state/workstreams/ dir and would flip to UNONBOARDED. `archive/` is
    # present on all of them (verified against the fleet) and is already the
    # pre-existing arm of completion_archive_predicate below, so matching it
    # here makes the two predicates genuinely identical instead of merely
    # claimed-equivalent -- also closes the P2 drift gap.
    return os.path.isdir(os.path.join(repo_root, "archive")) or os.path.isdir(
        os.path.join(repo_root, "state", "workstreams")
    )


def detect_onboarding_offer(repo_root: str, plugin_root: str) -> str:
    """Read-only classification. Never raises -- returns an offer line, or "" if silent."""
    if not repo_root:
        return ""
    if not os.path.isdir(os.path.join(repo_root, ".git")):
        return ""

    if _is_distribution_repo(repo_root):
        return ""

    sentinel = os.path.join(repo_root, ".git", "coordinator-onboarding-dismissed")
    if os.path.isfile(sentinel):
        return ""

    dismiss_cmd = f"touch {shlex.quote(os.path.join(repo_root, '.git', 'coordinator-onboarding-dismissed'))}"

    if not _is_onboarded(repo_root):
        return (
            "[onboarding] This repo is not yet onboarded — run /repo-setup to get "
            "coordinator scaffolding and currency tracking. "
            f"(Dismiss: {dismiss_cmd})"
        )

    if not plugin_root:
        # No probe available -- conservative: stay silent (can't assess).
        return ""

    probe_script = os.path.join(plugin_root, "bin", "probe-onboarding-currency.py")
    if os.path.isfile(probe_script) and is_executable(probe_script):
        probe_status = coordinator_currency_probe(repo_root, plugin_root)
        if probe_status in ("current", "source_is_live"):
            return ""
        if probe_status.startswith("drift"):
            return (
                f"[onboarding] This repo was onboarded against an older coordinator "
                f"({probe_status}) — run /repo-setup to bring it current. "
                f"(Dismiss: {dismiss_cmd})"
            )
        if probe_status.startswith("unstamped"):
            return (
                "[onboarding] This repo was onboarded but has no currency stamp — "
                f"run /repo-setup to bring it current. (Dismiss: {dismiss_cmd})"
            )
        # inconclusive* or any unrecognized status -> silent (safe default; do not
        # nag on infrastructure uncertainty per eager-agent-calibration.md).
        return ""

    # Probe script not found or not executable -- fall back to a direct
    # coordinator_currency_probe call, mirroring the bash oracle's fallback
    # branch (which sourced lib/coordinator_currency.py directly). First honour
    # source_is_live: if plugin_root is nested under repo_root/plugins/, the
    # repo being checked IS the coordinator source -- no stamp is expected.
    #
    # NOTE (mirrors bash oracle's C2 comment): this source_is_live check is a
    # DISTINCT concept from a "meta-repo" check -- it asks "is the coordinator
    # plugin co-located with the repo being checked?" (plugin path inside
    # repo/plugins/), not "is the repo's git root the claude-home dir?".
    #
    # coordinator_currency.py lives in claude-klabauter's own coordinator/lib/ (it
    # migrated out of the DoE plugin_root tree during the executable-surface
    # relocation), not under plugin_root -- resolve it off the claude-klabauter root
    # via the canonical resolver, never plugin_root, so this branch stays
    # reachable instead of silently always missing the file and returning ""
    # regardless of actual drift.
    try:
        claude_klabauter_root = coordinator_claude_klabauter_root()
    except RuntimeError as exc:
        print(f"skip: detect_onboarding_offer: coordinator_claude_klabauter_root(): {exc}", file=sys.stderr)
        claude_klabauter_root = ""

    currency_lib = os.path.join(claude_klabauter_root, "coordinator", "lib", "coordinator_currency.py") if claude_klabauter_root else ""
    if currency_lib and os.path.isfile(currency_lib):
        repo_norm = _strip_one_trailing_slash(repo_root).replace(os.sep, "/")
        plugin_norm = _strip_one_trailing_slash(plugin_root).replace(os.sep, "/")
        if plugin_norm.startswith(repo_norm + "/plugins"):
            return ""  # source_is_live -> silent

        probe_result = coordinator_currency_probe(repo_root, plugin_root)
        if probe_result == "current":
            return ""
        if probe_result.startswith("drift") or probe_result.startswith("unstamped"):
            return (
                f"[onboarding] This repo was onboarded against an older coordinator "
                f"({probe_result}) — run /repo-setup to bring it current. "
                f"(Dismiss: {dismiss_cmd})"
            )
        return ""

    # No probe available -- conservative: stay silent (can't assess).
    return ""


def _resolve_repo_root(explicit: str) -> str:
    """Resolve repo_root: explicit override wins; else `git rev-parse --show-toplevel`
    against cwd. Bounded subprocess (timeout + stdin guard) -- never blocks the
    per-repo-stable probe on a hung git process."""
    if explicit:
        return explicit
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Review: code-reviewer (Finding 1) — replaced a stringified fragment
        # of the try-block's own source with a plain human sentence.
        print(f"skip: git rev-parse --show-toplevel failed: {exc}", file=sys.stderr)
        return ""
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def main(argv: List[str], default_plugin_root: Optional[str] = None) -> int:
    """CLI entry.

    Args (env overrides, mirroring the bash oracle's standalone-mode contract):
        DETECT_ONBOARDING_REPO_ROOT   -- repo to check (default: git root of cwd)
        DETECT_ONBOARDING_PLUGIN_ROOT -- coordinator plugin root (default:
                                          `default_plugin_root`, a DoE-side
                                          contract fact only the calling
                                          trampoline can supply -- the location
                                          of the .sh file on disk)
    CLI flags `--repo <path>` / `--plugin-root <path>` take precedence over the
    env vars above (mirrors the bash oracle's standalone-mode arg parsing).

    Always returns 0 -- see module docstring § Exit codes.
    """
    repo_root_arg = ""
    plugin_root_arg = ""
    args = list(argv)
    i = 0
    while i < len(args):
        if args[i] == "--repo" and i + 1 < len(args):
            repo_root_arg = args[i + 1]
            i += 2
        elif args[i] == "--plugin-root" and i + 1 < len(args):
            plugin_root_arg = args[i + 1]
            i += 2
        else:
            i += 1

    repo_root_explicit = repo_root_arg or os.environ.get("DETECT_ONBOARDING_REPO_ROOT", "")
    repo_root = _resolve_repo_root(repo_root_explicit)

    plugin_root = (
        plugin_root_arg
        or os.environ.get("DETECT_ONBOARDING_PLUGIN_ROOT", "")
        or (default_plugin_root or "")
    )

    offer = detect_onboarding_offer(repo_root, plugin_root)
    if offer:
        print(offer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
